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

from django.contrib.contenttypes.models import ContentType
from nautobot.apps.jobs import BooleanVar, IntegerVar, JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import (
    Cable,
    ConsolePort,
    ConsoleServerPort,
    Device,
    DeviceType,
    Interface,
    Location,
    Platform,
    Rack,
)
from nautobot.extras.models import Role, Status
from nautobot.ipam.models import VLAN, IPAddress, IPAddressToInterface, Namespace, Prefix

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

# The office pool Device42 delegates; a person allocates each office a /22 of it, or bigger.
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
# The devices the Palo's DHCP Option 67 bootstraps, from the central ZTP file server (10-21 00:55–01:01).
CALL_HOME_ROLES = ("office-switch", "console-server", "ups", "pdu")
CALL_HOME_SUFFIXES = ("sw-01", "con-01", "ups-01", "pdu-01")
# Where each device's management address sits in the management VLAN.
MGMT_HOSTS = {"fw-01": 4, "fw-02": 5, "sw-01": 10, "con-01": 11, "ups-01": 12, "pdu-01": 13}
FIRST_AP_HOST = 20
# Each role's interfaces: name, type, and whether it's the management interface that holds the primary IP.
INTERFACES = {
    "edge-firewall": (
        ("management", "1000base-t", True),
        ("ethernet1/1", "1000base-t", False),
        ("ethernet1/2", "1000base-t", False),
        ("HA1", "1000base-t", False),
    ),
    "office-switch": (
        ("Vlan100", "virtual", True),
        *((f"GigabitEthernet1/0/{port}", "1000base-t", False) for port in range(1, 5)),
    ),
    "console-server": (("NET1", "1000base-t", True),),
    "ups": (("NMC", "1000base-t", True),),
    "pdu": (("NMC", "1000base-t", True),),
    "wireless-ap": (("wired0", "1000base-t", True),),
}
CONSOLE_PORTS = {"office-switch": ("console",)}
CONSOLE_SERVER_PORTS = {"console-server": ("Port 1",)}
# The port map (HLD §7 Phase 2): from, port, to, port, what it carries. It is the permanent design: only VLAN 1 on the
# switch is temporary (10-27W 00:42).
PORT_MAP = (
    ("sw-01", "GigabitEthernet1/0/1", None, "", "ISP DIA handoff (primary)"),
    ("sw-01", "GigabitEthernet1/0/2", "fw-01", "ethernet1/1", "Palo WAN"),
    ("sw-01", "GigabitEthernet1/0/3", "fw-01", "ethernet1/2", "Palo LAN"),
    ("sw-01", "GigabitEthernet1/0/4", "con-01", "NET1", "OpenGear LAN"),
    ("con-01", "Port 1", "sw-01", "console", "switch console"),
    ("fw-01", "HA1", "fw-02", "HA1", "HA link"),
)
# The MDF rack: each racked device's position, top down; the PDU mounts at the rear, zero-U, with no position.
RACK = "MDF-R1"
RACK_LAYOUT = {"con-01": 42, "sw-01": 40, "fw-01": 36, "fw-02": 34, "ups-01": 2, "pdu-01": None}
POWER_ON_ORDER = ("sw-01", "fw-01", "fw-02", "con-01")
# The central ZTP file server the Option 67 URL points at, reached through the Palo's VPN (Z3). Its host is open.
FILE_SERVER = "https://ztp.corp.example.com/offices/{site}/"


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


class ConfirmOfficePrefix(DemoJob):
    """Check and record the office prefix a person allocated from the Device42 office pool (10-21 00:32–00:36).

    Network Eng allocates the office's /22 in Device42 and pastes it into this job's form. It must be an IPv4 network
    in CIDR form, such as 10.64.4.0/22, with no host bits set, and at least a /22: the office's VLANs fill one. A value
    that fails any check fails the step with the reason; Retry with changes takes the corrected prefix. The job records
    it in Nautobot as the office's container prefix, which the next step splits per VLAN.
    """

    location = ObjectVar(model=Location)
    office_prefix = StringVar(
        label="Office prefix", description="The prefix you allocated in Device42, in CIDR form, such as 10.64.4.0/22"
    )
    min_seconds, max_seconds = timing(1, 2)

    class Meta:
        """Meta."""

        name = "ZTP: confirm office prefix"
        description = (
            "Check the office prefix a person allocated is an IPv4 CIDR network of at least a /22, and record it as "
            "the office's container prefix. Started by a person, with the form."
        )

    def run(self, location, office_prefix, **common):  # pylint: disable=arguments-differ
        """Return {office_prefix}; fail on a value that isn't a usable office prefix."""
        self.work(f"Check {office_prefix} for {location}", **common)
        network = office_network(office_prefix)
        namespace = Namespace.objects.get(name="Global")
        prefix = Prefix.objects.filter(prefix=str(network), namespace=namespace).first()
        if prefix is not None and prefix.locations.exclude(pk=location.pk).exists():
            raise DemoFailure(
                f"{network} is already recorded for {prefix.locations.exclude(pk=location.pk).first()}: allocate "
                "another prefix, then Retry with changes"
            )
        if prefix is None:
            prefix = Prefix(
                prefix=str(network),
                namespace=namespace,
                status=status_for(Prefix, "Active"),
                type="container",
                description=f"{location} office",
            )
            prefix.validated_save()
        prefix.locations.add(location)
        return {"office_prefix": str(network)}


def office_network(text):
    """Return `text` as an IPv4 network of at least a /22, or raise DemoFailure saying what's wrong with it."""
    text = (text or "").strip()
    again = "Retry with changes with the right prefix"
    try:
        exact = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise DemoFailure(f"'{text}' isn't a prefix in CIDR form, such as 10.64.4.0/22: {again}") from None
    if "/" not in text:
        raise DemoFailure(f"'{text}' has no prefix length, such as /22: {again}")
    if str(exact) != text and ipaddress.ip_interface(text).ip != exact.network_address:
        raise DemoFailure(f"'{text}' has host bits set: did you mean {exact}? {again}")
    if exact.version != 4:
        raise DemoFailure(f"{exact} is IPv6, and the office plan is IPv4: {again}")
    if exact.prefixlen > OFFICE_PREFIX_LENGTH:
        raise DemoFailure(f"{exact} is too small: the office's VLANs need a /{OFFICE_PREFIX_LENGTH} or bigger. {again}")
    return exact


def status_for(model, name):
    """Return the status `name`, creating it or adding `model` to it as Nautobot needs to use it there."""
    status, _ = Status.objects.get_or_create(name=name)
    status.content_types.add(ContentType.objects.get_for_model(model))
    return status


class DesignBuildTheNetwork(DemoJob):
    """Design and build the office in Nautobot in one shot, in Phase 1 before anyone goes to site (HLD §7 Phase 1).

    From the office prefix and the office type it creates, or finds when run again:

    - VLANs: the profile's VLANs at the location; EBC adds the AV and multicast VLAN (10-21 01:09).
    - Prefixes: the office prefix split per VLAN, each linked to its VLAN: the "beforehand" split of 10-21 00:32.
    - Devices with their role, model, platform and serials: the PA-3220 pair, the 9300 stack, the OpenGear, the APC
      UPS and PDU, and the Meraki APs, each Planned. Later steps trust these records: the 9300 and AP serials are
      claimed in the Meraki Dashboard before install, and every device's serial, model and role are checked against
      them at bootstrap (HLD §7 Phase 3).
    - Interfaces, console ports and console server ports, and a management interface on each device.
    - IP addresses: one per device in the management VLAN, assigned to its management interface and made its primary
      IPv4 address.
    - The MDF rack, with each device in its position; the PDU mounts at the rear, zero-U.
    - Cables per the port map, the permanent design: the Palo's WAN and LAN and the OpenGear on the 9300, the
      OpenGear's console on the 9300's, the HA link between the Palos; only the switch's VLAN 1 is temporary
      (10-27W 00:42, 00:55). The ISP handoff on port 1 isn't cabled: the circuits aren't modelled in Nautobot here,
      only returned as values.

    It returns the devices grouped for the bootstrap, the inventory with management addresses, the rack layout, the
    port map and the power-on order the field team installs by (HLD §7 Phase 2). Running it again creates nothing
    twice.
    """

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    ap_count = IntegerVar(default=6, min_value=1, max_value=40)
    office_prefix = StringVar(description="The office prefix, as Confirm office prefix recorded it")
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "ZTP: design and build the network"
        description = (
            "Create the office in Nautobot in one shot: VLANs, the office prefix split per VLAN, devices with serials, "
            "interfaces, management IPs as primary IPs, the MDF rack with each device placed, and the cables of the "
            "port map. Safe to repeat."
        )

    def run(self, location, office_type, ap_count, office_prefix, **common):  # pylint: disable=arguments-differ,too-many-arguments,too-many-locals
        """Return the plan, the addresses, the devices by kind as fan-out items, and what was built."""
        self.work(f"Design and build {location} in {office_prefix}", **common)
        site = site_code(location)
        namespace = Namespace.objects.get(name="Global")
        office = Prefix.objects.filter(prefix=office_prefix, namespace=namespace, locations=location).first()
        if office is None:
            raise DemoFailure(f"{office_prefix} isn't recorded for {location}: confirm the office prefix, then Retry")
        plan = self.vlans_and_prefixes(location, office, office_type, namespace)
        mgmt = ipaddress.ip_network(next(row["prefix"] for row in plan if row["name"] == "management"))
        addresses = {suffix: f"{mgmt.network_address + host}/{mgmt.prefixlen}" for suffix, host in MGMT_HOSTS.items()}
        for number in range(1, ap_count + 1):
            addresses[f"ap-{number:02d}"] = f"{mgmt.network_address + FIRST_AP_HOST + number - 1}/{mgmt.prefixlen}"
        rack = self.find_or_create(
            Rack, {"location": location, "name": RACK}, {"status": status_for(Rack, "Active"), "u_height": 42}
        )
        rows = list(OFFICE_DEVICES) + [
            (ACCESS_POINT[0], f"ap-{number:02d}", *ACCESS_POINT[1:]) for number in range(1, ap_count + 1)
        ]
        devices, by_suffix = [], {}
        for role_name, suffix, _, model, platform_name, factory, target in rows:
            device = self.device(location, rack, site, role_name, suffix, model, platform_name)
            self.management_ip(device, role_name, addresses[suffix], namespace)
            devices.append((device, role_name, suffix, model, factory, target))
            by_suffix[suffix] = device
        cables = self.cables(by_suffix)
        by_role = {}
        for device, role_name, *_ in devices:
            by_role.setdefault(role_name, []).append(ref(device))
        rng = stable("circuit", site)
        circuits = [
            {"circuit_id": f"DIA-{site.upper()}-{rng.randint(10000, 99999)}", "provider": provider, "role": role}
            for provider, role in (("Lumen", "primary"), ("Comcast Business", "secondary"))
        ]
        return {
            "office_prefix": office_prefix,
            "vlan_plan": plan,
            "addresses": addresses,
            "dhcp": [
                {"device": suffix, "reservation": address.split("/")[0], "option_67": suffix in CALL_HOME_SUFFIXES}
                for suffix, address in addresses.items()
                if not suffix.startswith("fw-")
            ],
            "firewalls": by_role["edge-firewall"],
            "infrastructure": [item for role in CALL_HOME_ROLES for item in by_role[role]],
            "access_points": [
                {"name": d.name, "serial": d.serial, "address": addresses[s]}
                for d, role, s, *_ in devices
                if role == "wireless-ap"
            ],
            "inventory": [
                {"device": d.name, "role": role, "model": model, "serial": d.serial, "mgmt": addresses[s]}
                for d, role, s, model, *_ in devices
            ],
            "device_count": len(devices),
            "circuits": circuits,
            "circuit_id": circuits[0]["circuit_id"],
            "rack_layout": [
                {"rack": RACK, "u": u or 0, "device": f"{site}-{suffix}" + ("" if u else " (rear, zero-U)")}
                for suffix, u in RACK_LAYOUT.items()
            ],
            "port_map": [
                {
                    "from": f"{site}-{a}",
                    "port": a_port,
                    "to": f"{site}-{b}" if b else "ISP",
                    "far_end": b_port,
                    "purpose": purpose,
                }
                for a, a_port, b, b_port, purpose in PORT_MAP
            ],
            "power_on_order": [f"{site}-{suffix}" for suffix in POWER_ON_ORDER],
            "built": {
                "vlans": len(plan),
                "prefixes": len(plan),
                "devices": len(devices),
                "interfaces": Interface.objects.filter(device__location=location).count(),
                "ip_addresses": len(devices),
                "cables": cables,
                "rack": RACK,
            },
        }

    @staticmethod
    def find_or_create(model, lookup, defaults=None):
        """Return the object `lookup` finds, or create it with `defaults` too: the job is safe to repeat."""
        found = model.objects.filter(**lookup).first()
        if found is None:
            found = model(**lookup, **(defaults or {}))
            found.validated_save()
        return found

    def vlans_and_prefixes(self, location, office, office_type, namespace):
        """Create the profile's VLANs and split the office prefix per VLAN, smallest first; return the VLAN plan."""
        plan, start = [], ipaddress.ip_network(str(office.prefix)).network_address
        active = status_for(VLAN, "Active")
        for vid, vname, length in sorted(VLAN_PROFILES[office_type], key=lambda vlan: vlan[2]):
            subnet = ipaddress.ip_network(f"{start}/{length}")
            start = subnet.broadcast_address + 1
            vlan = VLAN.objects.filter(locations=location, vid=vid).first()
            if vlan is None:
                vlan = VLAN(vid=vid, name=f"{site_code(location)}-{vname}", status=active)
                vlan.validated_save()
                vlan.locations.add(location)
            prefix = self.find_or_create(
                Prefix,
                {"prefix": str(subnet), "namespace": namespace},
                {"status": status_for(Prefix, "Active"), "type": "network", "description": f"{location} {vname}"},
            )
            if prefix.vlan_id != vlan.pk:
                prefix.vlan = vlan
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
        return plan

    def device(self, location, rack, site, role_name, suffix, model, platform_name):  # pylint: disable=too-many-arguments
        """Create the device in its rack position, with its interfaces and console ports; return it."""
        device = Device.objects.filter(name=f"{site}-{suffix}").first()
        if device is None:
            position = RACK_LAYOUT.get(suffix)
            device = Device(
                name=f"{site}-{suffix}",
                location=location,
                role=Role.objects.get(name=role_name),
                device_type=DeviceType.objects.get(model=model),
                platform=Platform.objects.get(name=platform_name),
                serial=serial_for(site, suffix, model),
                status=status_for(Device, "Planned"),
                rack=rack if suffix in RACK_LAYOUT else None,
                position=position,
                face="front" if position else "",
            )
            device.validated_save()
            self.logger.info("Created %s, serial %s.", device, device.serial)
        planned = status_for(Interface, "Planned")
        for interface_name, kind, management in INTERFACES[role_name]:
            self.find_or_create(
                Interface,
                {"device": device, "name": interface_name},
                {"type": kind, "mgmt_only": management, "status": planned},
            )
        for port_name in CONSOLE_PORTS.get(role_name, ()):
            self.find_or_create(ConsolePort, {"device": device, "name": port_name}, {"type": "rj-45"})
        for port_name in CONSOLE_SERVER_PORTS.get(role_name, ()):
            self.find_or_create(ConsoleServerPort, {"device": device, "name": port_name}, {"type": "rj-45"})
        return device

    def management_ip(self, device, role_name, address, namespace):
        """Assign the device's management address to its management interface and make it the primary IPv4."""
        interface = Interface.objects.get(device=device, name=next(n for n, _, m in INTERFACES[role_name] if m))
        ip = IPAddress.objects.filter(address=address, parent__namespace=namespace).first()
        if ip is None:
            ip = IPAddress(address=address, namespace=namespace, status=status_for(IPAddress, "Active"))
            ip.validated_save()
        if not IPAddressToInterface.objects.filter(ip_address=ip, interface=interface).exists():
            interface.add_ip_addresses(ip)
        if device.primary_ip4_id != ip.pk:
            device.primary_ip4 = ip
            device.validated_save()

    @staticmethod
    def cables(by_suffix):
        """Cable the port map between the office's devices; return how many cables the port map has in Nautobot."""
        planned = status_for(Cable, "Planned")
        count = 0
        for a, a_port, b, b_port, purpose in PORT_MAP:
            if b is None:
                continue  # the ISP handoff: its circuit isn't modelled here
            end_a, end_b = port_of(by_suffix[a], a_port), port_of(by_suffix[b], b_port)
            if end_a.cable_id is None and end_b.cable_id is None:
                Cable(termination_a=end_a, termination_b=end_b, status=planned, label=purpose).validated_save()
            count += 1
        return count


def port_of(device, name):
    """Return the device's interface, console port or console server port called `name`."""
    for model in (Interface, ConsolePort, ConsoleServerPort):
        found = model.objects.filter(device=device, name=name).first()
        if found is not None:
            return found
    raise DemoFailure(f"{device} has no port {name}: the port map names a port the design doesn't create")


class SelectTargets(DemoJob):
    """Select the office's config profile, firmware targets and templates (HLD §7 Phase 1)."""

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "ZTP: select profile, firmware and templates"
        description = "Select the normal or EBC profile, the approved firmware per platform, and the templates."

    def run(self, location, office_type, **common):  # pylint: disable=arguments-differ
        """Return {profile, firmware, templates}."""
        self.work(f"Select {office_type} targets for {location}", **common)
        # The 9300 runs in Meraki mode (Z2), so the Dashboard upgrades it like the APs.
        upgraded_by = {"paloalto_panos": "Panorama", "meraki": "Meraki Dashboard", "cisco_ios": "Meraki Dashboard"}
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
        return {
            "profile": office_type,
            "firmware": firmware,
            "templates": {
                "panorama_template_stack": f"TS-OFFICE-{office_type.upper()}",
                "panorama_device_group": f"DG-{location.name.upper()}",
                "meraki_template": f"Office-{office_type.upper()}",
                "switch_template": f"MS-Office-{office_type.upper()}",
            },
        }


class GenerateBootstrapArtifacts(DemoJob):
    """Render, version and checksum every bootstrap file: the Palo USB base config, Cisco script, OpenGear and APC.

    The Palo's base config carries its VPN tunnels, so it reaches Panorama internally (Z11). How the tunnels' keys get
    onto the drive without a secret in clear text is open (Z13): here they are BeyondTrust references.
    """

    location = ObjectVar(model=Location)
    addresses = JSONVar()
    tunnels = JSONVar(required=False, description="The office's VPN tunnels, from the Panorama pre-work")
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "ZTP: generate bootstrap artifacts"
        description = "Render each device's bootstrap file, versioned and checksummed, with secrets as PAM references."

    def run(self, location, addresses, tunnels, **common):  # pylint: disable=arguments-differ
        """Return {artifact_version, artifacts, secrets_in_clear}."""
        self.work(f"Render {location}'s bootstrap artifacts", **common)
        site = site_code(location)
        version = f"{site}-v{stable('artifacts', site).randint(100, 999)}"
        files = []
        for suffix in ("fw-01", "fw-02"):
            ip = addresses.get(suffix, "")
            init_cfg = (
                f"type=static\nip-address={ip.split('/')[0]}\nhostname={site}-{suffix}\n"
                f"panorama-server=panorama.corp.example.com\ntplname=TS-{site.upper()}\ndgname=DG-{site.upper()}\n"
                "vm-auth-key=pam://beyondtrust/ztp/panorama-auth-key\n"
            )
            base_config = "".join(
                f"<tunnel name='{tunnel['tunnel']}' peer='{tunnel['peer']}' psk='pam://beyondtrust/ztp/{site}-vpn'/>\n"
                for tunnel in tunnels or []
            )
            files += [
                (f"{site}-{suffix}", "config/init-cfg.txt", init_cfg, "USB"),
                (f"{site}-{suffix}", "config/bootstrap.xml", f"<!-- static WAN, mgmt -->\n{base_config}", "USB"),
            ]
        files += [
            (
                f"{site}-sw-01",
                "ztp/c9300.py",
                f"# {version}: convert to Meraki mode, call home by serial\n",
                "Option 67",
            ),
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
    """Publish the Option 67 files on the central ZTP file server (Z3); the Palo's files go on USB instead."""

    location = ObjectVar(model=Location)
    artifact_version = StringVar()
    min_seconds, max_seconds = timing(3, 8)

    class Meta:
        """Meta."""

        name = "ZTP: publish bootstrap files"
        description = "Publish the bootstrap files on the central ZTP file server and return the Option 67 URL."

    def run(self, location, artifact_version, **common):  # pylint: disable=arguments-differ
        """Return {option67_url, published}."""
        self.work(f"Publish {artifact_version} to the central file server", **common)
        return {"option67_url": FILE_SERVER.format(site=site_code(location)), "published": artifact_version}


class ConfirmSiteReadiness(DemoJob):
    """The human-confirmed checklist: a Network Eng person ticks what Nautobot can't know (10-21 00:19)."""

    location = ObjectVar(model=Location)
    mdf_ready = BooleanVar(default=False, label="MDF ready", description="Room, rack, power and cooling are in")
    patching_done = BooleanVar(default=False, label="Patching done", description="Structured cabling is terminated")
    devices_on_site = BooleanVar(default=False, label="Devices on site", description="Every box of the BOM is there")
    switch_iosxe = BooleanVar(
        default=False,
        label="Switch IOS-XE as ordered",
        description="The 9300s ship with the minimum IOS-XE on the purchase order: Meraki mode needs it (Z10)",
    )
    notes = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(1, 2)

    class Meta:
        """Meta."""

        name = "ZTP: confirm site readiness"
        description = (
            "Confirm the MDF, the patching, the delivered devices and the switch IOS-XE. Started by a person, with the "
            "form."
        )

    def run(self, location, mdf_ready, patching_done, devices_on_site, switch_iosxe, notes, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return the checklist; fail on anything unticked: MDF is a hard prerequisite (HLD §11)."""
        self.work(f"Record {location}'s readiness checklist", **common)
        checklist = {
            "MDF ready": mdf_ready,
            "Patching done": patching_done,
            "Devices on site": devices_on_site,
            "Switch IOS-XE as ordered": switch_iosxe,
        }
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
    """Take the bootstrap path down: Option 67 off, VLAN 1 replaced, the files unpublished (HLD §10.1, §16)."""

    location = ObjectVar(model=Location)
    artifact_version = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP: retire bootstrap"
        description = (
            "Turn Option 67 off on the Palo through Panorama, check the switch profile replaced VLAN 1, unpublish the "
            "files. Changes the network."
        )

    def run(self, location, artifact_version, **common):  # pylint: disable=arguments-differ
        """Return {retired}."""
        self.work(f"Retire {location}'s bootstrap path", **common)
        return {
            "retired": [
                {"what": "DHCP Option 67 on ethernet1/2", "where": "Panorama template", "state": "removed"},
                {
                    "what": "VLAN 1 ISP bridge",
                    "where": f"{site_code(location)}-sw-01, Dashboard switch profile",
                    "state": "replaced",
                },
                {
                    "what": f"Bootstrap files {artifact_version or ''}".strip(),
                    "where": "ZTP file server",
                    "state": "unpublished",
                },
            ]
        }


class RecordMediaCustody(DemoJob):
    """The installer's report: the USB drives are out of the Palos, and who holds them now (HLD §3, §10.1)."""

    location = ObjectVar(model=Location)
    drives_removed = BooleanVar(default=False, label="USB drives removed", description="Out of both firewalls")
    custody = StringVar(default="", label="Held by", description="Who holds the drives now, such as Network Eng SJC")
    min_seconds, max_seconds = timing(1, 2)

    class Meta:
        """Meta."""

        name = "ZTP: record bootstrap media custody"
        description = "Record the USB drives removed from the firewalls and who holds them. Started by the installer."

    def run(self, location, drives_removed, custody, **common):  # pylint: disable=arguments-differ
        """Return {custody}; fail while a drive is in a firewall or nobody holds them."""
        self.work(f"Record the custody of {location}'s bootstrap drives", **common)
        if not (drives_removed and custody):
            raise DemoFailure(
                "The bootstrap drives must be out of the firewalls and held by someone: Retry with changes once done"
            )
        return {
            "custody": [
                {"drive": f"{site_code(location)}-{suffix} bootstrap", "removed": True, "held_by": custody}
                for suffix in ("fw-01", "fw-02")
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
        description = "A wait's check: the office's prefixes, devices and serials are in Nautobot."

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
        yield "Meraki Dashboard: the 9300 stack and every AP online", True


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
        yield "Option 67 off, VLAN 1 replaced by the switch profile", True
        yield "USB drives removed, custody recorded", True
        yield "Datadog polling, backups enrolled", True


JOBS = (
    SetSiteLifecycle,
    SetDeviceLifecycle,
    ValidateIntake,
    ConfirmOfficePrefix,
    DesignBuildTheNetwork,
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
    RecordMediaCustody,
    CommissionSite,
    ReadinessGate,
    PhysicalGate,
    BootstrapGate,
    PlatformGate,
    ConfigurationGate,
    ValidationGate,
)

register_jobs(*JOBS)
