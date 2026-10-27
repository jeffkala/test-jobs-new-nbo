"""Greenfield office ZTP, the OpenGear lane: firmware, the operations VLAN and the console port map (HLD §8.5)."""

from nautobot.apps.jobs import ObjectVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoJob, timing
from .ztp import OFFICE_DEVICES

name = "NBO demo: ZTP OpenGear"  # pylint: disable=invalid-name

FACTORY, TARGET = next((row[5], row[6]) for row in OFFICE_DEVICES if row[4] == "opengear")


class UpgradeFirmware(DemoJob):
    """Upgrade the console server's firmware over its REST API."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "ZTP OpenGear: upgrade firmware"
        description = "Upload and install the approved OpenGear firmware, then wait for it back. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {firmware_from, firmware_to}."""
        self.work(f"Upgrade {device} from {FACTORY} to {TARGET}", **common)
        return {"firmware_from": FACTORY, "firmware_to": TARGET}


class Configure(DemoJob):
    """Configure the operations VLAN, the console port map and the cellular out-of-band link."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "ZTP OpenGear: configure"
        description = "Set the operations VLAN, the console port map and the cellular OOB link. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {console_ports}."""
        self.work(f"Configure {device}", **common)
        site = device.name.rsplit("-", 2)[0]
        return {
            "console_ports": [
                {"port": port, "target": f"{site}-{target}"}
                for port, target in ((1, "sw-01"), (2, "fw-01"), (3, "fw-02"), (4, "ups-01"))
            ]
        }


JOBS = (UpgradeFirmware, Configure)

register_jobs(*JOBS)
