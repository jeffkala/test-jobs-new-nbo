"""Greenfield office ZTP, the APC lane: NMC firmware and the operations config of the UPS and the PDU (HLD §8.4).

How the NMC firmware is upgraded is open decision Z6: it "used to be flaky", so the demo can show it failing once.
"""

from nautobot.apps.jobs import ObjectVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoJob, timing
from .ztp import OFFICE_DEVICES

name = "NBO demo: ZTP APC"  # pylint: disable=invalid-name

FIRMWARE = {row[3]: (row[5], row[6]) for row in OFFICE_DEVICES if row[4] == "apc_nmc"}


class UpgradeNmc(DemoJob):
    """Upload the NMC firmware over SFTP and wait for the card to restart."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(12, 24)
    failure = "apc-upgrade-flaky"

    class Meta:
        """Meta."""

        name = "ZTP APC: upgrade NMC firmware"
        description = "Upload the approved NMC firmware and wait for the card to restart. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {firmware_from, firmware_to}."""
        factory, target = FIRMWARE[device.device_type.model]
        self.failure_message = (
            f"{device}'s NMC didn't come back 10 minutes after the {target} upload. The UPS still powers its load; "
            "reset the card from the front panel, then Retry"
        )
        self.work(
            f"Upload {target} to {device}",
            **(common if device.role.name == "ups" else {**common, "demo_failure": "none"}),
        )
        return {"firmware_from": factory, "firmware_to": target}


class Configure(DemoJob):
    """Configure the card: operations VLAN address, SNMPv3, syslog and NTP."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP APC: configure"
        description = "Set the NMC's SNMPv3, syslog and NTP. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {settings}."""
        self.work(f"Configure {device}", **common)
        return {"settings": ["snmpv3 user dd-poller (SHA/AES)", "syslog 10.10.0.60", "ntp 10.10.0.10, 10.10.0.11"]}


JOBS = (UpgradeNmc, Configure)

register_jobs(*JOBS)
