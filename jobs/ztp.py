"""Greenfield office ZTP, the Nautobot side: intake, addressing, records, bootstrap artifacts, gates and handover.

These are the jobs NTC builds in Nautobot for the Croissant ZTP MVP: one office with a Palo Alto PA-3220 pair, a
Cisco 9300 stack, an OpenGear console server, an APC UPS and PDU, and Meraki 9166i access points. The platform jobs
(Panorama, Meraki Dashboard, PAM, Datadog) live in their own modules.

They read and write real Nautobot objects: the office's prefixes come out of the office pool the seed job creates,
the devices are created at the location with their serials, and the location's status walks the HLD's lifecycle.
Nothing touches a network; every device answers as the real one would.
"""

import hashlib
import ipaddress

from nautobot.apps.jobs import BooleanVar, IntegerVar, JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device, DeviceType, Location, Platform
from nautobot.extras.models import Role, Status
from nautobot.ipam.models import Namespace, Prefix

from .demo import DemoFailure, DemoJob, ref, set_status, stable, timing

name = "NBO demo: ZTP greenfield office"  # pylint: disable=invalid-name

# The site lifecycle of the HLD (§6), in order. The workflow's lifecycle track names the same states.
SITE_LIFECYCLE = (
    "Planned",
    "Ready for install",
    "Installed",
    "Bootstrapped",
    "Platform connected",
    "Fully configured",
    "Validated",
    "Commissioned",
)
# A device's own lifecycle: created from the plan, through bootstrap and full config, to production.
DEVICE_LIFECYCLE = ("Planned", "Bootstrapped", "Configured", "Active")

# The office pool Device42 delegates to Nautobot; every office gets the next free /22 of it.
OFFICE_POOL = "10.64.0.0/12"
OFFICE_PREFIX_LENGTH = 22
# The office's VLANs per profile: VLAN, name, prefix length. EBC adds the AV and multicast VLAN (10-21 01:09).
VLAN_PROFILES = {
    "normal": ((100, "management", 26), (110, "users", 23), (120, "voice", 25), (130, "wireless", 24)),
    "ebc": (
        (100, "management", 26),
        (110, "users", 23),
        (120, "voice", 25),
        (130, "wireless", 24),
        (140, "av-multicast", 26),
    ),
}

# The devices of one office (MVP): role, name suffix, manufacturer, model, platform, factory version, target.
OFFICE_DEVICES = (
    ("edge-firewall", "fw-01", "Palo Alto Networks", "PA-3220", "paloalto_panos", "10.1.9", "11.1.6-h1"),
    ("edge-firewall", "fw-02", "Palo Alto Networks", "PA-3220", "paloalto_panos", "10.1.9", "11.1.6-h1"),
    ("office-switch", "sw-01", "Cisco", "C9300-48UXM", "cisco_ios", "17.12.3", "17.15.1"),
    ("console-server", "con-01", "Opengear", "OM1208-8E-L", "opengear", "24.07.0", "25.03.1"),
    ("ups", "ups-01", "APC", "SMT3000RMI2UC", "apc_nmc", "2.4.0.1", "2.7.0.6"),
    ("pdu", "pdu-01", "APC", "AP8841", "apc_nmc", "7.0.4", "7.1.4"),
)
ACCESS_POINT = ("wireless-ap", "Cisco Meraki", "CW9166I-MR", "meraki", "MR 30.6", "MR 31.1.5")
# The devices the Palo's DHCP Option 67 bootstraps, from the ZTP file server (10-21 00:55–01:01).
CALL_HOME_ROLES = ("office-switch", "console-server", "ups", "pdu")
# Where each device's management address sits in the management VLAN.
MGMT_HOSTS = {"fw-01": 4, "fw-02": 5, "sw-01": 10, "con-01": 11, "ups-01": 12, "pdu-01": 13}
FIRST_AP_HOST = 20
# The temporary bootstrap port map (HLD §7 Phase 2).
BOOTSTRAP_PORT_MAP = (
    ("sw-01", "GigabitEthernet1/0/1", "ISP", "DIA handoff (primary)"),
    ("sw-01", "GigabitEthernet1/0/2", "fw-01", "ethernet1/1 (WAN)"),
    ("sw-01", "GigabitEthernet1/0/3", "fw-01", "ethernet1/2 (LAN)"),
    ("sw-01", "GigabitEthernet1/0/4", "con-01", "LAN"),
    ("con-01", "Port 1", "sw-01", "console"),
    ("fw-01", "HA1/HA2", "fw-02", "HA1/HA2"),
)
POWER_ON_ORDER = ("sw-01", "fw-01", "fw-02", "con-01")
# Where each file server option serves the bootstrap files from (open decision Z3).
FILE_SERVERS = {
    "central": "https://ztp.corp.example.com/offices/{site}/",
    "nautobot": "https://nautobot.example.com/api/plugins/ztp/files/{site}/?serial=",
    "opengear": "http://{opengear}/ztp/",
    "aws": "https://ztp-{site}.s3.us-west-2.amazonaws.com/",
}


def site_code(location):
    """Return the site code the devices are named after: the location's name in lower case."""
    return location.name.lower()


def office_devices(location):
    """Return the office's devices in Nautobot, by name."""
    return Device.objects.filter(location=location).select_related("role", "device_type", "status").order_by("name")


def serial_for(site, suffix, model):
    """Return the serial the vendor ships the box with, stable per site and device."""
    rng = stable("serial", site, suffix)
    if model.startswith("PA-"):
        return f"0120010{rng.randint(10000, 99999)}"
    if model.startswith("CW"):
        return f"Q5AB-{rng.randint(1000, 9999)}-{rng.choice('ABCDEFGHJK')}{rng.randint(100, 999)}"
    if model.startswith("C9300"):
        return f"FOC{rng.randint(2700, 2799)}Y{rng.randint(100, 999)}"
    if model.startswith("OM"):
        return f"1208{rng.randint(1000000, 9999999)}"
    return f"ZA{rng.randint(2300, 2399)}{rng.randint(100000, 999999)}"


def sha256(text):
    """Return the hex SHA-256 of `text`: the integrity check of a bootstrap artifact (HLD §10.1)."""
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------------------------------------- Phase 1: plan
class SetSiteLifecycle(DemoJob):
    """Move the office along the HLD's site lifecycle, from Planned to Commissioned."""

    location = ObjectVar(model=Location)
    stage = IntegerVar(min_value=0, max_value=len(SITE_LIFECYCLE) - 1, description="Index into the lifecycle")
    min_seconds, max_seconds = timing(2, 4)

    class Meta:
        """Meta."""

        name = "ZTP: set site lifecycle state"
        description = "Set the office's status to a state of the ZTP site lifecycle."

    def run(self, location, stage, **common):  # pylint: disable=arguments-differ
        """Return {site_state}."""
        self.work(f"Mark {location} {SITE_LIFECYCLE[stage]}", **common)
        return {"site_state": set_status(location, SITE_LIFECYCLE[stage])}


class SetDeviceLifecycle(DemoJob):
    """Move one device along its lifecycle: Planned, Bootstrapped, Configured, Active."""

    device = ObjectVar(model=Device)
    stage = IntegerVar(min_value=0, max_value=len(DEVICE_LIFECYCLE) - 1, description="Index into the lifecycle")
    min_seconds, max_seconds = timing(2, 4)

    class Meta:
        """Meta."""

        name = "ZTP: set device lifecycle state"
        description = "Set the device's status to a state of its ZTP lifecycle."

    def run(self, device, stage, **common):  # pylint: disable=arguments-differ
        """Return {device_state}."""
        self.work(f"Mark {device} {DEVICE_LIFECYCLE[stage]}", **common)
        return {"device_state": set_status(device, DEVICE_LIFECYCLE[stage])}


class ValidateIntake(DemoJob):
    """Check the site intake: the location, its airport code and office type, and the VLAN plan they give."""

    location = ObjectVar(model=Location)
    airport_code = StringVar(description="The IATA code the site is named after, such as SJC")
    office_type = StringVar(default="normal", description="normal or ebc")
    min_seconds, max_seconds = timing(3, 8)

    class Meta:
        """Meta."""

        name = "ZTP: validate site intake"
        description = "Check the intake variables and return the office's VLAN plan for its profile."

    def run(self, location, airport_code, office_type, **common):  # pylint: disable=arguments-differ
        """Return {site_code, profile, vlans, checks}; fail naming every gap."""
        self.work(f"Validate the intake for {location}", **common)
        checks = [
            ("location is Planned", location.status.name == "Planned", location.status.name),
            ("name starts with the airport code", location.name.upper().startswith(airport_code), location.name),
            ("office type has a profile", office_type in VLAN_PROFILES, office_type),
            ("no devices recorded yet", not office_devices(location).exists(), office_devices(location).count()),
        ]
        gaps = [what for what, ok, _ in checks if not ok]
        if gaps:
            raise DemoFailure(f"The intake for {location} fails: {'; '.join(gaps)}. Fix the location, then Retry")
        return {
            "site_code": site_code(location),
            "profile": office_type,
            "vlans": [
                {"vid": vid, "name": vname, "size": f"/{length}"} for vid, vname, length in VLAN_PROFILES[office_type]
            ],
            "checks": [{"check": what, "value": str(value), "result": "ok"} for what, _, value in checks],
        }


class AllocateOfficeAddressing(DemoJob):
    """Take the next /22 from the office pool and split it per VLAN: the "beforehand" split of 10-21 00:32."""

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    ap_count = IntegerVar(default=6, min_value=1, max_value=40)
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "ZTP: allocate office addressing"
        description = "Allocate the office /22 from the Device42-delegated pool, split it per VLAN, plan addresses."

    def run(self, location, office_type, ap_count, **common):  # pylint: disable=arguments-differ
        """Return {office_prefix, vlan_plan, addresses, dhcp}. Running it again returns the same allocation."""
        self.work(f"Allocate a /{OFFICE_PREFIX_LENGTH} for {location} from {OFFICE_POOL}", **common)
        namespace = Namespace.objects.get(name="Global")
        active = Status.objects.get(name="Active")
        office = Prefix.objects.filter(namespace=namespace, locations=location, type="container").first()
        if office is None:
            office = self.next_free(namespace)
            office.status, office.type, office.description = active, "container", f"{location} office"
            office.validated_save()
            office.locations.add(location)
        plan, network = [], ipaddress.ip_network(str(office.prefix))
        start = network.network_address
        for vid, vname, length in sorted(VLAN_PROFILES[office_type], key=lambda vlan: vlan[2]):
            subnet = ipaddress.ip_network(f"{start}/{length}")
            start = subnet.broadcast_address + 1
            prefix = Prefix.objects.filter(prefix=str(subnet), namespace=namespace).first()
            if prefix is None:
                prefix = Prefix(
                    prefix=str(subnet),
                    namespace=namespace,
                    status=active,
                    type="network",
                    description=f"{location} VLAN {vid} {vname}",
                )
                prefix.validated_save()
            prefix.locations.add(location)
            hosts = list(subnet.hosts())
            plan.append(
                {
                    "vid": vid,
                    "name": vname,
                    "prefix": str(subnet),
                    "gateway": f"{hosts[0]} (Palo)",
                    "dhcp": "" if vname == "management" else f"{hosts[min(9, len(hosts) - 1)]}-{hosts[-1]}",
                }
            )
        mgmt = ipaddress.ip_network(next(row["prefix"] for row in plan if row["name"] == "management"))
        addresses = {suffix: f"{mgmt.network_address + host}/{mgmt.prefixlen}" for suffix, host in MGMT_HOSTS.items()}
        for number in range(1, ap_count + 1):
            addresses[f"ap-{number:02d}"] = f"{mgmt.network_address + FIRST_AP_HOST + number - 1}/{mgmt.prefixlen}"
        dhcp = [
            {
                "device": suffix,
                "reservation": address.split("/")[0],
                "option_67": suffix in ("sw-01", "con-01", "ups-01", "pdu-01"),
            }
            for suffix, address in addresses.items()
            if not suffix.startswith("fw-")
        ]
        return {"office_prefix": str(office.prefix), "vlan_plan": plan, "addresses": addresses, "dhcp": dhcp}

    @staticmethod
    def next_free(namespace):
        """Return a new Prefix for the first /22 of the pool no office holds yet."""
        pool = ipaddress.ip_network(OFFICE_POOL)
        taken = {str(p.prefix) for p in Prefix.objects.filter(namespace=namespace, prefix_length=OFFICE_PREFIX_LENGTH)}
        for candidate in pool.subnets(new_prefix=OFFICE_PREFIX_LENGTH):
            if str(candidate) not in taken:
                return Prefix(prefix=str(candidate), namespace=namespace)
        raise DemoFailure(f"The office pool {OFFICE_POOL} is full: ask the IPAM team for another delegation")


class ReleaseOfficeAddressing(DemoJob):
    """Give the office's prefixes back to the pool: the rollback of a failed allocation."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(2, 5)

    class Meta:
        """Meta."""

        name = "ZTP: release office addressing"
        description = "Delete the office's prefixes so the /22 returns to the pool. Safe to repeat."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {released}."""
        self.work(f"Release {location}'s prefixes", **common)
        released = [str(p.prefix) for p in Prefix.objects.filter(locations=location)]
        Prefix.objects.filter(locations=location).delete()
        return {"released": released}


class CreateOfficeRecords(DemoJob):
    """Create the office's devices with their serials, the circuits, the rack layout and the bootstrap port map."""

    location = ObjectVar(model=Location)
    ap_count = IntegerVar(default=6, min_value=1, max_value=40)
    addresses = JSONVar(description="The management address of each device, from the allocation")
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "ZTP: create office records"
        description = "Create the office's devices, circuits, rack layout and port map in Nautobot. Safe to repeat."

    def run(self, location, ap_count, addresses, **common):  # pylint: disable=arguments-differ
        """Return the devices by kind, as fan-out items, with the circuits, rack layout and port map."""
        self.work(f"Create {location}'s devices, circuits and port map", **common)
        site = site_code(location)
        planned = Status.objects.get(name="Planned")
        rows = list(OFFICE_DEVICES) + [
            (ACCESS_POINT[0], f"ap-{number:02d}", *ACCESS_POINT[1:]) for number in range(1, ap_count + 1)
        ]
        devices = []
        for role_name, suffix, _, model, platform_name, factory, target in rows:
            device = Device.objects.filter(name=f"{site}-{suffix}").first()
            if device is None:
                device = Device(
                    name=f"{site}-{suffix}",
                    location=location,
                    role=Role.objects.get(name=role_name),
                    device_type=DeviceType.objects.get(model=model),
                    platform=Platform.objects.get(name=platform_name),
                    serial=serial_for(site, suffix, model),
                    status=planned,
                )
                device.validated_save()
                self.logger.info("Created %s, serial %s.", device, device.serial)
            devices.append((device, role_name, suffix, model, factory, target))
        by_role = {}
        for device, role_name, *_ in devices:
            by_role.setdefault(role_name, []).append(ref(device))
        rng = stable("circuit", site)
        circuits = [
            {"circuit_id": f"DIA-{site.upper()}-{rng.randint(10000, 99999)}", "provider": provider, "role": role}
            for provider, role in (("Lumen", "primary"), ("Comcast Business", "secondary"))
        ]
        return {
            "firewalls": by_role["edge-firewall"],
            "infrastructure": [item for role in CALL_HOME_ROLES for item in by_role[role]],
            "access_points": [
                {"name": d.name, "serial": d.serial, "address": addresses.get(s, "")}
                for d, role, s, *_ in devices
                if role == "wireless-ap"
            ],
            "inventory": [
                {"device": d.name, "role": role, "model": model, "serial": d.serial, "mgmt": addresses.get(s, "")}
                for d, role, s, model, *_ in devices
            ],
            "device_count": len(devices),
            "circuits": circuits,
            "circuit_id": circuits[0]["circuit_id"],
            "rack_layout": [
                {"rack": "MDF-R1", "u": u, "device": f"{site}-{suffix}"}
                for u, suffix in ((42, "con-01"), (40, "sw-01"), (36, "fw-01"), (34, "fw-02"), (2, "ups-01"))
            ]
            + [{"rack": "MDF-R1", "u": 0, "device": f"{site}-pdu-01 (rear, zero-U)"}],
            "port_map": [
                {
                    "from": f"{site}-{a}" if a != "ISP" else a,
                    "port": port,
                    "to": f"{site}-{b}" if b != "ISP" else b,
                    "far_end": far,
                }
                for a, port, b, far in BOOTSTRAP_PORT_MAP
            ],
            "power_on_order": [f"{site}-{suffix}" for suffix in POWER_ON_ORDER],
        }


class SelectTargets(DemoJob):
    """Select the office's config profile, firmware targets and templates (HLD §7 Phase 1)."""

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    switch_management = StringVar(default="meraki", description="meraki or cli: open decision Z2")
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "ZTP: select profile, firmware and templates"
        description = "Select the normal or EBC profile, the approved firmware per platform, and the templates."

    def run(self, location, office_type, switch_management, **common):  # pylint: disable=arguments-differ
        """Return {profile, firmware, templates}."""
        self.work(f"Select {office_type} targets for {location}", **common)
        upgraded_by = {"paloalto_panos": "Panorama", "meraki": "Meraki Dashboard"}
        firmware, seen = [], set()
        for _, _, _, model, platform_name, factory, target in (
            *OFFICE_DEVICES,
            (ACCESS_POINT[0], "", *ACCESS_POINT[1:]),
        ):
            if model not in seen:
                seen.add(model)
                firmware.append(
                    {
                        "model": model,
                        "factory": factory,
                        "target": target,
                        "upgraded_by": upgraded_by.get(platform_name, "Nautobot"),
                    }
                )
        if switch_management == "meraki":
            switch = next(row for row in firmware if row["model"].startswith("C9300"))
            switch.update({"target": f"{switch['target']} (Meraki managed)", "upgraded_by": "Meraki Dashboard"})
        return {
            "profile": office_type,
            "firmware": firmware,
            "templates": {
                "panorama_template_stack": f"TS-OFFICE-{office_type.upper()}",
                "panorama_device_group": f"DG-{location.name.upper()}",
                "meraki_template": f"Office-{office_type.upper()}",
                "switch_profile": f"c9300-{office_type}-{switch_management}",
            },
        }


class GenerateBootstrapArtifacts(DemoJob):
    """Render, version and checksum every bootstrap file: Palo init-cfg, Cisco script, OpenGear and APC configs."""

    location = ObjectVar(model=Location)
    palo_bootstrap = StringVar(default="usb", description="usb or panorama-ztp: open decision Z1")
    switch_management = StringVar(default="meraki")
    addresses = JSONVar()
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "ZTP: generate bootstrap artifacts"
        description = "Render each device's bootstrap file, versioned and checksummed, with secrets as PAM references."

    def run(self, location, palo_bootstrap, switch_management, addresses, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {artifact_version, artifacts, secrets_in_clear}."""
        self.work(f"Render {location}'s bootstrap artifacts", **common)
        site = site_code(location)
        version = f"{site}-v{stable('artifacts', site, palo_bootstrap, switch_management).randint(100, 999)}"
        files = []
        for suffix in ("fw-01", "fw-02"):
            ip = addresses.get(suffix, "")
            body = (
                f"type=static\nip-address={ip.split('/')[0]}\nhostname={site}-{suffix}\n"
                f"panorama-server=panorama.corp.example.com\ntplname=TS-{site.upper()}\ndgname=DG-{site.upper()}\n"
                "vm-auth-key=pam://delinea/ztp/panorama-auth-key\n"
            )
            files.append(
                (
                    f"{site}-{suffix}",
                    "config/init-cfg.txt",
                    body,
                    "USB" if palo_bootstrap == "usb" else "Palo ZTP service",
                )
            )
        script = "meraki-mode conversion" if switch_management == "meraki" else "day-0 CLI"
        files += [
            (f"{site}-sw-01", "ztp/c9300.py", f"# {version}: {script}, then call home by serial\n", "Option 67"),
            (f"{site}-con-01", "ztp/opengear.sh", f"# {version}: ops VLAN 100, console port map\n", "Option 67"),
            (f"{site}-ups-01", "ztp/apc-ups.ini", f"; {version}: SNMPv3, syslog, NTP\n", "Option 67"),
            (f"{site}-pdu-01", "ztp/apc-pdu.ini", f"; {version}: SNMPv3, syslog, NTP\n", "Option 67"),
        ]
        artifacts = [
            {"device": device, "file": path, "delivered_by": via, "sha256": sha256(body)[:16], "bytes": len(body)}
            for device, path, body, via in files
        ]
        return {"artifact_version": version, "artifacts": artifacts, "secrets_in_clear": 0}


class PublishBootstrapFiles(DemoJob):
    """Publish the artifacts on the ZTP file server the Palo's Option 67 points at (open decision Z3)."""

    location = ObjectVar(model=Location)
    file_server = StringVar(default="central", description="central, nautobot, opengear or aws")
    artifact_version = StringVar()
    addresses = JSONVar()
    min_seconds, max_seconds = timing(3, 8)

    class Meta:
        """Meta."""

        name = "ZTP: publish bootstrap files"
        description = "Publish the bootstrap files on the ZTP file server and return the Option 67 URL."

    def run(self, location, file_server, artifact_version, addresses, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {option67_url, file_server, published}."""
        self.work(f"Publish {artifact_version} to the {file_server} file server", **common)
        url = FILE_SERVERS[file_server].format(
            site=site_code(location), opengear=addresses.get("con-01", "").split("/")[0]
        )
        return {"option67_url": url, "file_server": file_server, "published": artifact_version}


class ConfirmSiteReadiness(DemoJob):
    """The human-confirmed checklist: a Network Eng person ticks what Nautobot can't know (10-21 00:19)."""

    location = ObjectVar(model=Location)
    mdf_ready = BooleanVar(default=False, label="MDF ready", description="Room, rack, power and cooling are in")
    patching_done = BooleanVar(default=False, label="Patching done", description="Structured cabling is terminated")
    devices_on_site = BooleanVar(default=False, label="Devices on site", description="Every box of the BOM is there")
    notes = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(1, 2)

    class Meta:
        """Meta."""

        name = "ZTP: confirm site readiness"
        description = "Confirm the MDF, the patching and the delivered devices. Started by a person, with the form."

    def run(self, location, mdf_ready, patching_done, devices_on_site, notes, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return the checklist; fail on anything unticked: MDF is a hard prerequisite (HLD §11)."""
        self.work(f"Record {location}'s readiness checklist", **common)
        checklist = {"MDF ready": mdf_ready, "Patching done": patching_done, "Devices on site": devices_on_site}
        missing = [item for item, ticked in checklist.items() if not ticked]
        if missing:
            raise DemoFailure(
                f"Not ready for install: {', '.join(missing)} not confirmed. Retry with changes once done"
            )
        return {"checklist": [{"item": item, "confirmed": True} for item in checklist], "notes": notes}


# ---------------------------------------------------------------------------------------- Phase 2–3: install, boot
class RecordFieldTests(DemoJob):
    """The installer's report: circuit test and link states on the bootstrap port map (HLD §7 Phase 2)."""

    location = ObjectVar(model=Location)
    circuit_tested = BooleanVar(default=False, label="Circuit tested", description="The ISP's turn-up test passed")
    links_up = BooleanVar(default=False, label="Bootstrap links up", description="Ports 1 to 4 of the switch show link")
    photos_url = StringVar(required=False, default="", label="Photos", description="Where the rack photos are")
    min_seconds, max_seconds = timing(1, 2)

    class Meta:
        """Meta."""

        name = "ZTP: record field tests"
        description = "Record the circuit test and the link states. Started by the installer, with the form."

    def run(self, location, circuit_tested, links_up, photos_url, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {field_tests}; fail when the circuit or a link isn't up."""
        self.work(f"Record {location}'s field tests", **common)
        if not (circuit_tested and links_up):
            raise DemoFailure(
                "The installer reports the circuit or a bootstrap link down: fix on site, then Retry with changes"
            )
        return {
            "field_tests": [
                {"test": "Circuit turn-up", "result": "pass"},
                *({"test": f"sw-01 port {port} link", "result": "up"} for port in (1, 2, 3, 4)),
            ],
            "photos_url": photos_url,
        }


class VerifyIdentity(DemoJob):
    """Compare what the device reports with the record before any target config is applied (HLD §7 Phase 3)."""

    device = ObjectVar(model=Device)
    artifact_version = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(3, 8)
    failure = "identity-mismatch"

    class Meta:
        """Meta."""

        name = "ZTP: verify device identity"
        description = "Check serial, model and role the device reports against its Nautobot record."

    def run(self, device, artifact_version, **common):  # pylint: disable=arguments-differ
        """Return {identity, bootstrap_record}; fail on a box that isn't the one planned."""
        reported = device.serial
        if device.role.name == "office-switch" and self.failure_due(common.get("ticket"), common.get("demo_failure")):
            reported = (
                f"FOC{stable('swap', device.name).randint(2800, 2899)}Y{stable('swap', device.name).randint(100, 999)}"
            )
        self.work(f"Read {device}'s identity", **{**common, "demo_failure": "none"})
        if reported != device.serial:
            raise DemoFailure(
                f"{device} reports serial {reported}, Nautobot plans {device.serial}: the wrong box is in the rack, or "
                "the record is wrong. Fix the serial in Nautobot if the box is right, then Retry"
            )
        return {
            "identity": {
                "serial": reported,
                "model": device.device_type.model,
                "role": device.role.name,
                "matches": True,
            },
            "bootstrap_record": {"device": device.name, "artifact_version": artifact_version or "-", "result": "ok"},
        }


# -------------------------------------------------------------------------------------------- Phase 5: per device
class PreCheck(DemoJob):
    """Snapshot the device before its full config: version, config hash, neighbours."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "ZTP: pre-check device"
        description = "Snapshot the device's version, config and neighbours before its full config."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {snapshot}."""
        self.work(f"Snapshot {device}", **common)
        factory = next((row[5] for row in OFFICE_DEVICES if device.name.endswith(row[1])), ACCESS_POINT[4])
        return {
            "snapshot": {
                "version": factory,
                "config_lines": stable(device.name).randint(40, 90),
                "profile": "bootstrap",
            }
        }


class PostCheck(DemoJob):
    """Snapshot the device after its full config and compare it with the pre-check."""

    device = ObjectVar(model=Device)
    pre = JSONVar(required=False)
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "ZTP: post-check device"
        description = "Snapshot the device after its full config and compare it with the pre-check."

    def run(self, device, pre, **common):  # pylint: disable=arguments-differ
        """Return {snapshot, changes}."""
        self.work(f"Snapshot {device} and compare", **common)
        target = next((row[6] for row in OFFICE_DEVICES if device.name.endswith(row[1])), ACCESS_POINT[5])
        after = {"version": target, "config_lines": stable(device.name).randint(400, 900), "profile": "office"}
        changes = [{"field": key, "before": (pre or {}).get(key), "after": value} for key, value in after.items()]
        return {"snapshot": after, "changes": changes}


# --------------------------------------------------------------------------------------- Phase 6–7: operate, hand
class UpdateDnsAndIpam(DemoJob):
    """Mark the office's addresses in use and create its A and PTR records."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "ZTP: update DNS and IPAM"
        description = "Mark the office's prefixes Active and create the devices' A and PTR records. Safe to repeat."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {prefixes, dns_records}."""
        self.work(f"Update {location}'s IPAM and DNS", **common)
        prefixes = [str(p.prefix) for p in Prefix.objects.filter(locations=location, type="network")]
        records = [f"{d.name}.mgmt.corp.example.com" for d in office_devices(location)]
        return {"prefixes": prefixes, "dns_records": records}


class ReconcileSite(DemoJob):
    """Compare what the office runs with the model: devices, serials, versions, VLANs, cabling."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(6, 14)
    failure = "deviation"
    failure_message = ""  # a deviation isn't an error: a person decides whether to accept it

    class Meta:
        """Meta."""

        name = "ZTP: reconcile site with the model"
        description = "Compare the office's observed state with Nautobot and list every deviation."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {deviations, report}."""
        self.work(f"Reconcile {location}", **{**common, "demo_failure": "none"})
        deviations = []
        if self.failure_due(common.get("ticket"), common.get("demo_failure")):
            deviations.append(
                {
                    "device": f"{site_code(location)}-ap-03",
                    "found": "mounted in room 2.14",
                    "model": "room 2.12",
                    "severity": "minor",
                }
            )
        report = [
            {"check": check, "result": "match"}
            for check in ("devices and serials", "software versions", "VLANs and prefixes", "cabling (LLDP)")
        ]
        return {"deviations": deviations, "report": report}


class RetireBootstrap(DemoJob):
    """Take the bootstrap path down: Option 67 off, the VLAN 1 bridge removed, the files unpublished (HLD §16)."""

    location = ObjectVar(model=Location)
    artifact_version = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP: retire bootstrap"
        description = (
            "Turn Option 67 off on the Palo, remove the VLAN 1 path, unpublish the files. Changes the network."
        )

    def run(self, location, artifact_version, **common):  # pylint: disable=arguments-differ
        """Return {retired}."""
        self.work(f"Retire {location}'s bootstrap path", **common)
        return {
            "retired": [
                {"what": "DHCP Option 67 on ethernet1/2", "where": "Panorama template", "state": "removed"},
                {"what": "VLAN 1 ISP bridge", "where": f"{site_code(location)}-sw-01", "state": "removed"},
                {
                    "what": f"Bootstrap files {artifact_version or ''}".strip(),
                    "where": "ZTP file server",
                    "state": "unpublished",
                },
            ]
        }


class CommissionSite(DemoJob):
    """Put every device of the office in production and hand the site over."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "ZTP: commission site"
        description = "Set every device of the office Active and the office Commissioned."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {site_state, devices_active}."""
        self.work(f"Commission {location}", **common)
        for device in office_devices(location):
            set_status(device, "Active")
        return {"site_state": set_status(location, "Commissioned"), "devices_active": office_devices(location).count()}


# ----------------------------------------------------------------------------------------------------------- gates
class SiteGate(DemoJob):
    """A wait's check: a lifecycle gate (HLD §11). Done with its evidence when every check passes; not yet otherwise."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(3, 6)
    gate = ""
    # The device state every non-AP device must have reached, or "" when the gate doesn't read devices.
    devices_at = ""

    class Meta:
        """Meta."""

        abstract = True

    def checks(self, location):
        """Return the gate's own checks as (check, passed) pairs."""
        del location
        return []

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Answer done with the evidence, or not yet naming what's missing."""
        self.work(f"Evaluate the {self.gate} gate for {location}", **common)
        checks = list(self.checks(location))
        if self.devices_at:
            for device in office_devices(location).exclude(role__name="wireless-ap"):
                reached = DEVICE_LIFECYCLE.index(device.status.name) if device.status.name in DEVICE_LIFECYCLE else 0
                checks.append((f"{device.name} {self.devices_at}", reached >= DEVICE_LIFECYCLE.index(self.devices_at)))
        evidence = [{"check": check, "result": "pass" if passed else "missing"} for check, passed in checks]
        missing = [check for check, passed in checks if not passed]
        if missing:
            return self.check(f"{self.gate} gate", "not-yet", f"waiting on {', '.join(missing)}", evidence=evidence)
        return self.check(f"{self.gate} gate", "done", f"{len(checks)} checks pass", evidence=evidence)


class ReadinessGate(SiteGate):
    """Gate 1: the site record is complete before anyone goes to site."""

    gate = "Ready for install"

    class Meta:
        """Meta."""

        name = "ZTP gate: ready for install (check)"
        description = "A wait's check: the office's prefixes, devices, serials and port map are in Nautobot."

    def checks(self, location):
        """The records the plan needs."""
        devices = office_devices(location)
        yield "office prefix allocated", Prefix.objects.filter(locations=location, type="container").exists()
        yield "devices recorded", devices.exists()
        yield "every device has a serial", not devices.filter(serial="").exists()


class PhysicalGate(SiteGate):
    """Gate 2: equipment powered and cabled, ISP handoff present."""

    gate = "Installed"

    class Meta:
        """Meta."""

        name = "ZTP gate: installed (check)"
        description = "A wait's check: the switch sees the ISP and the Palo WAN on the bootstrap VLAN."

    def checks(self, location):
        """What the switch's factory VLAN 1 shows: the ISP handoff and the Palo's WAN MAC."""
        del location  # the switch reports it, not Nautobot
        yield "ISP handoff on sw-01 port 1", True
        yield "Palo WAN MAC on VLAN 1", True


class BootstrapGate(SiteGate):
    """Gate 3: Palo, switch and OpenGear reachable; identities match."""

    gate = "Bootstrapped"
    devices_at = "Bootstrapped"

    class Meta:
        """Meta."""

        name = "ZTP gate: bootstrapped (check)"
        description = "A wait's check: every device bootstrapped, with its identity verified."


class PlatformGate(SiteGate):
    """Gate 4: Panorama, Meraki and Nautobot all see their devices."""

    gate = "Platform connected"
    devices_at = "Bootstrapped"

    class Meta:
        """Meta."""

        name = "ZTP gate: platform connected (check)"
        description = "A wait's check: Panorama and the Meraki Dashboard see every device they manage."

    def checks(self, location):
        """What the platforms report."""
        del location  # Panorama and the Dashboard report it, not Nautobot
        yield "Panorama: both firewalls connected, HA formed", True
        yield "Meraki Dashboard: every AP online", True


class ConfigurationGate(SiteGate):
    """Gate 5: the selected profile is applied in every domain."""

    gate = "Fully configured"
    devices_at = "Configured"

    class Meta:
        """Meta."""

        name = "ZTP gate: fully configured (check)"
        description = "A wait's check: every device runs its target firmware and the office profile."


class ValidationGate(SiteGate):
    """Gate 6: network, wireless, security and management checks pass, and the bootstrap path is gone."""

    gate = "Validated"
    devices_at = "Configured"

    class Meta:
        """Meta."""

        name = "ZTP gate: validated (check)"
        description = "A wait's check: the operational checks pass and the bootstrap is retired."

    def checks(self, location):
        """The operational checks."""
        yield "prefixes Active in IPAM", Prefix.objects.filter(locations=location, type="network").exists()
        yield "Option 67 off, VLAN 1 path removed", True
        yield "Datadog polling, backups enrolled", True


JOBS = (
    SetSiteLifecycle,
    SetDeviceLifecycle,
    ValidateIntake,
    AllocateOfficeAddressing,
    ReleaseOfficeAddressing,
    CreateOfficeRecords,
    SelectTargets,
    GenerateBootstrapArtifacts,
    PublishBootstrapFiles,
    ConfirmSiteReadiness,
    RecordFieldTests,
    VerifyIdentity,
    PreCheck,
    PostCheck,
    UpdateDnsAndIpam,
    ReconcileSite,
    RetireBootstrap,
    CommissionSite,
    ReadinessGate,
    PhysicalGate,
    BootstrapGate,
    PlatformGate,
    ConfigurationGate,
    ValidationGate,
)

register_jobs(*JOBS)
