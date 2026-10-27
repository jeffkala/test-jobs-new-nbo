"""Greenfield office ZTP, the Palo Alto lane: Panorama pre-work, bootstrap, licensing, HA, upgrade and validation.

Panorama is authoritative for the Palo's config once it has bootstrapped (HLD §5.1): these jobs answer as its XML API
would. The PA-3220 pair is `fw-01` (active) and `fw-02` (passive).
"""

from nautobot.apps.jobs import ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device, Location

from .demo import DemoFailure, DemoJob, stable, timing
from .ztp import OFFICE_DEVICES, site_code

name = "NBO demo: ZTP Panorama"  # pylint: disable=invalid-name

PANOS_TARGET = next(row[6] for row in OFFICE_DEVICES if row[4] == "paloalto_panos")
PANOS_FACTORY = next(row[5] for row in OFFICE_DEVICES if row[4] == "paloalto_panos")


class PrepareOffice(DemoJob):
    """Panorama pre-work: the office's device group, template stack, address objects and DC tunnels (10-21 00:10)."""

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    min_seconds, max_seconds = timing(6, 14)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: prepare office"
        description = (
            "Create the office's device group, template stack, address objects and DC tunnels. Safe to repeat."
        )

    def run(self, location, office_type, **common):  # pylint: disable=arguments-differ
        """Return {device_group, template_stack, tunnels}."""
        site = site_code(location).upper()
        self.work(f"Prepare Panorama for {site}", **common)
        rng = stable("tunnels", site)
        return {
            "device_group": f"DG-{site}",
            "template_stack": f"TS-{site}",
            "templates": [f"TPL-OFFICE-{office_type.upper()}", "TPL-BASE-PA3220", f"TPL-{site}-LOCAL"],
            "tunnels": [
                {"tunnel": f"tunnel.{rng.randint(100, 499)}", "peer": dc, "state": "staged"}
                for dc in ("SJC-DC1 vpn-gw", "IAD-DC2 vpn-gw")
            ],
        }


class RegisterZtpSerials(DemoJob):
    """Register the firewalls' serials with the Palo Alto ZTP service, for the cloud bootstrap (Z1). Opens records."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: register serials for cloud ZTP"
        description = "Register the firewall serials in the Palo Alto ZTP service. Opens a record outside NBO."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {registered}."""
        self.work(f"Register {location}'s firewalls with the ZTP service", **common)
        firewalls = Device.objects.filter(location=location, role__name="edge-firewall").order_by("name")
        return {"registered": [{"device": fw.name, "serial": fw.serial, "state": "Registered"} for fw in firewalls]}


class DeviceConnected(DemoJob):
    """A wait's check: done when Panorama shows the firewall connected, with its bootstrap config loaded."""

    device = ObjectVar(model=Device)
    palo_bootstrap = StringVar(default="usb")
    min_seconds, max_seconds = timing(3, 8)
    failure = "usb-not-read"

    class Meta:
        """Meta."""

        name = "ZTP Panorama: device connected (check)"
        description = "A wait's check: done when Panorama shows the firewall connected."

    def run(self, device, palo_bootstrap, **common):  # pylint: disable=arguments-differ
        """Fail when the active firewall booted its factory config: the USB wasn't read."""
        self.failure_message = (
            f"{device} booted its factory config: the bootstrap USB wasn't read. It must be FAT32 with "
            "config/init-cfg.txt at its root. Re-burn it, power-cycle the firewall, then Retry"
        )
        try:
            self.work(
                f"Ask Panorama about {device.serial}",
                **(
                    common
                    if device.name.endswith("fw-01") and palo_bootstrap == "usb"
                    else {**common, "demo_failure": "none"}
                ),
            )
        except DemoFailure as failure:
            return self.check("Panorama connection", "failed", str(failure))
        return self.check(
            "Panorama connection", "done", f"{device} ({device.serial}) is connected, PAN-OS {PANOS_FACTORY}"
        )


class ServeOption67(DemoJob):
    """Turn on the Palo's LAN DHCP with Option 67 pointing at the ZTP file server: "DHCP 100% Palo" (10-21 00:59)."""

    location = ObjectVar(model=Location)
    option67_url = StringVar()
    template_stack = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: serve DHCP with option 67"
        description = "Push the LAN DHCP server with Option 67 to the office's firewalls. Changes the network."

    def run(self, location, option67_url, template_stack, **common):  # pylint: disable=arguments-differ
        """Return {dhcp_config, commit_id}."""
        self.work(f"Push DHCP option 67 = {option67_url} to {template_stack or location}", **common)
        return {
            "dhcp_config": [
                "set network dhcp interface ethernet1/2 server mode enabled",
                f"set network dhcp interface ethernet1/2 server option user-defined ztp-script code 67 ascii {option67_url}",
                "set network dhcp interface ethernet1/2 server option lease timeout 60",
            ],
            "commit_id": f"PUSH-{stable('dhcp', location.name).randint(10000, 99999)}",
        }


class ActivateLicences(DemoJob):
    """Activate the firewalls' licences and register them in the support portal (10-21 00:25; HLD §8.1)."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: activate licences"
        description = "Fetch the licences by auth code and register the firewalls in the support portal."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {licences}."""
        self.work(f"Activate {location}'s firewall licences", **common)
        firewalls = Device.objects.filter(location=location, role__name="edge-firewall").order_by("name")
        return {
            "licences": [
                {"device": fw.name, "licence": lic, "expires": "2029-12-31"}
                for fw in firewalls
                for lic in ("Threat Prevention", "Advanced URL Filtering", "GlobalProtect Gateway", "Support")
            ]
        }


class FormHaPair(DemoJob):
    """Join the passive firewall to the active one: base config first, then HA (open decision Z5)."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(12, 24)
    failure = "ha-join-fails"
    failure_message = (
        "HA1 control link down: fw-02's HA1 peer address doesn't answer. Check the HA1/HA2 cables between the pair, "
        "then Retry"
    )

    class Meta:
        """Meta."""

        name = "ZTP Panorama: form HA pair"
        description = "Configure HA active/passive on the pair and wait until it syncs. Changes the network."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {ha_state}."""
        self.work(f"Form the HA pair at {location}", **common)
        site = site_code(location)
        return {
            "ha_state": [
                {"device": f"{site}-fw-01", "state": "active", "priority": 100, "config_sync": "synchronized"},
                {"device": f"{site}-fw-02", "state": "passive", "priority": 110, "config_sync": "synchronized"},
            ]
        }


class BreakHaPair(DemoJob):
    """Take the HA config off both firewalls: the rollback of a failed join."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: break HA pair"
        description = "Remove the HA config from both firewalls: the rollback of a failed HA join."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {removed}."""
        self.work(f"Remove HA from {location}'s firewalls", **common)
        return {"removed": True}


class UpgradePanos(DemoJob):
    """Upgrade one firewall from factory PAN-OS to the approved release, through Panorama (10-21 00:30)."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(15, 30)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: upgrade PAN-OS"
        description = (
            "Download, install and reboot into the approved PAN-OS, with pre- and post-checks. Changes the network."
        )

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {panos_from, panos_to, path}."""
        self.work(f"Upgrade {device} from {PANOS_FACTORY} to {PANOS_TARGET}", **common)
        return {
            "panos_from": PANOS_FACTORY,
            "panos_to": PANOS_TARGET,
            "path": [PANOS_FACTORY, "11.0.0", "11.1.0", PANOS_TARGET],
        }


class RevertPanos(DemoJob):
    """Boot the firewall back into the partition it ran: the rollback of a failed upgrade."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(8, 15)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: revert PAN-OS"
        description = "Boot the previous partition: the rollback of a failed PAN-OS upgrade."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {reverted_to}."""
        self.work(f"Revert {device} to {PANOS_FACTORY}", **common)
        return {"reverted_to": PANOS_FACTORY}


class PushTemplates(DemoJob):
    """Push the office's device group and template stack to one firewall and commit."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: push templates"
        description = "Commit and push the device group and template stack to the firewall. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {commit_id, pushed}."""
        site = device.location.name.upper()
        self.work(f"Commit and push DG-{site} and TS-{site} to {device}", **common)
        return {
            "commit_id": f"JOB-{stable('push', device.name).randint(1000, 9999)}",
            "pushed": [f"DG-{site}", f"TS-{site}"],
        }


class EnableCorporateTunnels(DemoJob):
    """Bring up the IPsec tunnels and BGP to the data centres: connects the office to the corporate network."""

    location = ObjectVar(model=Location)
    change_number = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: enable corporate tunnels"
        description = "Enable the IPsec tunnels and BGP to the data centres, under the change. Changes the network."

    def run(self, location, change_number, **common):  # pylint: disable=arguments-differ
        """Return {tunnels_enabled}."""
        self.work(f"Enable {location}'s DC tunnels under {change_number or 'the change'}", **common)
        return {"tunnels_enabled": ["SJC-DC1 vpn-gw", "IAD-DC2 vpn-gw"]}


class DisableCorporateTunnels(DemoJob):
    """Shut the DC tunnels again: the rollback of a failed enable."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "ZTP Panorama: disable corporate tunnels"
        description = "Disable the office's DC tunnels: the rollback of a failed enable."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {tunnels_disabled}."""
        self.work(f"Disable {location}'s DC tunnels", **common)
        return {"tunnels_disabled": True}


class ValidateFirewalls(DemoJob):
    """A wait's check: IPsec, BGP, routing, NAT, HA failover and Panorama in-sync (HLD §7 Phase 6)."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(6, 12)
    failure = "tunnel-down"
    failure_message = (
        "IPsec to IAD-DC2 is down: phase 2 proposal mismatch (office offers AES-256-GCM, the DC gateway expects "
        "AES-256-CBC). Fix the crypto profile, then Retry"
    )

    class Meta:
        """Meta."""

        name = "ZTP Panorama: validate firewalls (check)"
        description = "A wait's check: tunnels, BGP, NAT, HA failover and Panorama sync are healthy."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Answer done with the evidence, or failed on a tunnel that can't come up by waiting."""
        try:
            self.work(f"Validate {location}'s firewalls", **common)
        except DemoFailure as failure:
            return self.check("Firewall validation", "failed", str(failure))
        evidence = [
            {"check": check, "result": "pass"}
            for check in (
                "IPsec SJC-DC1 up",
                "IPsec IAD-DC2 up",
                "BGP: 2 peers established, 214 prefixes",
                "Default route via ISP primary",
                "Outbound NAT hits",
                "HA failover test: 3 s, sessions kept",
                "Panorama: In Sync",
            )
        ]
        return self.check("Firewall validation", "done", "every check passes", evidence=evidence)


JOBS = (
    PrepareOffice,
    RegisterZtpSerials,
    DeviceConnected,
    ServeOption67,
    ActivateLicences,
    FormHaPair,
    BreakHaPair,
    UpgradePanos,
    RevertPanos,
    PushTemplates,
    EnableCorporateTunnels,
    DisableCorporateTunnels,
    ValidateFirewalls,
)

register_jobs(*JOBS)
