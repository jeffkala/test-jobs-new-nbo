"""Greenfield office ZTP, the Meraki lane: the Dashboard network, claiming, AP firmware and the wireless checks.

The access points, and the Cisco 9300 if Meraki mode is confirmed (open decision Z2), are managed from the Meraki
Dashboard. These jobs answer as its API would.
"""

from nautobot.apps.jobs import ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device, Location

from .demo import DemoJob, stable, times_asked, timing
from .ztp import ACCESS_POINT, site_code

name = "NBO demo: ZTP Meraki"  # pylint: disable=invalid-name


def dashboard_network_id(location):
    """Return the Dashboard network ID of the office, stable per site."""
    return f"L_{stable('meraki', location.name).randint(10**17, 10**18 - 1)}"


class PrepareNetwork(DemoJob):
    """Create the office's Dashboard network, bound to its template, with its RF profile (10-21 00:26; HLD §8.3)."""

    location = ObjectVar(model=Location)
    office_type = StringVar(default="normal")
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: prepare network"
        description = "Create the office's Dashboard network, bind its template and RF profile. Safe to repeat."

    def run(self, location, office_type, **common):  # pylint: disable=arguments-differ
        """Return {network_id, network_name, template, rf_profile}."""
        self.work(f"Create the Dashboard network for {location}", **common)
        return {
            "network_id": dashboard_network_id(location),
            "network_name": f"{location.name} office",
            "template": f"Office-{office_type.upper()}",
            "rf_profile": "Office high density" if office_type == "ebc" else "Office standard",
        }


class ClaimDevices(DemoJob):
    """Claim the APs, and the switch in Meraki mode, into the office's network by serial (10-21 00:51)."""

    location = ObjectVar(model=Location)
    switch_management = StringVar(default="meraki")
    network_id = StringVar()
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: claim devices"
        description = "Claim the office's Meraki-managed devices into its network by serial. Safe to repeat."

    def run(self, location, switch_management, network_id, **common):  # pylint: disable=arguments-differ
        """Return {claimed, claimed_count}."""
        self.work(f"Claim {location}'s devices into {network_id}", **common)
        roles = ["wireless-ap"] + (["office-switch"] if switch_management == "meraki" else [])
        devices = Device.objects.filter(location=location, role__name__in=roles).order_by("name")
        claimed = [{"device": d.name, "serial": d.serial, "network": network_id} for d in devices]
        return {"claimed": claimed, "claimed_count": len(claimed)}


class DevicesOnline(DemoJob):
    """A wait's check: done when every claimed device is online in the Dashboard with its config downloaded."""

    location = ObjectVar(model=Location)
    network_id = StringVar()
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: devices online (check)"
        description = "A wait's check: done when every claimed device is online in the Dashboard."

    def run(self, location, network_id, **common):  # pylint: disable=arguments-differ
        """The APs fetch their config first: not yet the first time, done after."""
        self.work(f"Read {network_id}'s device statuses", **common)
        if times_asked(f"meraki-online:{location.pk}:{common.get('ticket')}") == 1:
            return self.check("Dashboard", "not-yet", "the access points are downloading their configuration")
        return self.check("Dashboard", "done", f"every device of {location} is online")


class ApplySwitchProfile(DemoJob):
    """Apply the normal or EBC switch profile from the Dashboard, in Meraki mode: VLANs, trunks, access ports, QoS."""

    device = ObjectVar(model=Device)
    office_type = StringVar(default="normal")
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: apply switch profile"
        description = "Bind the switch to the office's switch template in the Dashboard. Changes the network."

    def run(self, device, office_type, **common):  # pylint: disable=arguments-differ
        """Return {profile, ports}."""
        self.work(f"Bind {device} to the {office_type} switch template", **common)
        ports = {
            "1-2": "trunk to Palo (LAN)",
            "3-44": "access, users 110, voice 120",
            "45-48": "trunk to APs, native 100",
        }
        if office_type == "ebc":
            ports["37-44"] = "access, AV 140, IGMP snooping, QoS trust DSCP"
        return {
            "profile": f"MS-Office-{office_type.upper()}",
            "ports": [{"ports": k, "config": v} for k, v in ports.items()],
        }


class UpgradeFirmware(DemoJob):
    """Schedule the office's AP firmware upgrade in the Dashboard, and wait for it (MVP)."""

    location = ObjectVar(model=Location)
    network_id = StringVar()
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: upgrade AP firmware"
        description = "Upgrade the office's access points to the approved MR firmware. Changes the network."

    def run(self, location, network_id, **common):  # pylint: disable=arguments-differ
        """Return {firmware_from, firmware_to, upgraded}."""
        self.work(f"Upgrade {network_id}'s APs to {ACCESS_POINT[5]}", **common)
        upgraded = Device.objects.filter(location=location, role__name="wireless-ap").count()
        return {"firmware_from": ACCESS_POINT[4], "firmware_to": ACCESS_POINT[5], "upgraded": upgraded}


class ValidateWireless(DemoJob):
    """Check the office's wireless: every AP online, the SSIDs broadcast, clients join on the right VLAN."""

    location = ObjectVar(model=Location)
    network_id = StringVar()
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: validate wireless"
        description = "Check every AP is online, the SSIDs broadcast and a test client joins."

    def run(self, location, network_id, **common):  # pylint: disable=arguments-differ
        """Return {wireless_checks}."""
        self.work(f"Validate {network_id}'s wireless", **common)
        aps = Device.objects.filter(location=location, role__name="wireless-ap").count()
        return {
            "wireless_checks": [
                {"check": f"{aps} of {aps} APs online", "result": "pass"},
                {"check": "SSID corp-wifi (802.1X) broadcasting on 5 and 6 GHz", "result": "pass"},
                {"check": "SSID guest-wifi on VLAN 130", "result": "pass"},
                {"check": "Test client joins and gets DHCP from the Palo", "result": "pass"},
            ]
        }


class SyncToNautobot(DemoJob):
    """Sync what the Dashboard knows back to Nautobot: AP MACs, LAN IPs, firmware (the SSoT direction of Z7)."""

    location = ObjectVar(model=Location)
    network_id = StringVar()
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "ZTP Meraki: sync to Nautobot"
        description = "Sync the Dashboard's view of the office's devices back to Nautobot. Safe to repeat."

    def run(self, location, network_id, **common):  # pylint: disable=arguments-differ
        """Return {synced}."""
        self.work(f"Sync {network_id} to Nautobot", **common)
        site = site_code(location)
        synced = [
            {
                "device": d.name,
                "mac": "cc:03:d9:%02x:%02x:%02x" % tuple(stable("mac", d.name).randint(0, 255) for _ in range(3)),
            }
            for d in Device.objects.filter(location=location, name__startswith=f"{site}-ap-").order_by("name")
        ]
        return {"synced": synced}


JOBS = (
    PrepareNetwork,
    ClaimDevices,
    DevicesOnline,
    ApplySwitchProfile,
    UpgradeFirmware,
    ValidateWireless,
    SyncToNautobot,
)

register_jobs(*JOBS)
