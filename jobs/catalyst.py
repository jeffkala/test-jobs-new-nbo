"""Greenfield office ZTP, the Cisco 9300 lane: the Meraki-mode readiness check, and the CLI path if not Meraki mode.

Which path runs is open decision Z2. In Meraki mode the factory IOS-XE must support the conversion, or the switch is
upgraded first (Z10). On the CLI path Nautobot upgrades IOS-XE and applies the office profile itself.
"""

from nautobot.apps.jobs import ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoJob, timing
from .ztp import OFFICE_DEVICES

name = "NBO demo: ZTP Catalyst"  # pylint: disable=invalid-name

IOSXE_FACTORY, IOSXE_TARGET = next((row[5], row[6]) for row in OFFICE_DEVICES if row[4] == "cisco_ios")
# The lowest IOS-XE that converts to Meraki management: what the purchase order should ask for (Z10).
MERAKI_MODE_MINIMUM = "17.15.1"
# What the factory ships when the demo shows a switch that's too old.
IOSXE_TOO_OLD = "17.9.4a"


def version_tuple(version):
    """Return `17.9.4a` as (17, 9, 4), for comparing versions."""
    return tuple(int("".join(c for c in part if c.isdigit()) or 0) for part in version.split("."))


class CheckMerakiMode(DemoJob):
    """Read the switch's factory IOS-XE and say whether it converts to Meraki mode as it is (Z10)."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(3, 6)
    failure = "iosxe-too-old"
    failure_message = ""  # not an error: the switch is upgraded first

    class Meta:
        """Meta."""

        name = "ZTP Catalyst: check Meraki mode support"
        description = "Read the factory IOS-XE and say whether it must be upgraded before the Meraki conversion."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {factory_version, minimum, needs_upgrade}."""
        self.work(f"Read {device}'s factory IOS-XE", **{**common, "demo_failure": "none"})
        too_old = self.failure_due(common.get("ticket"), common.get("demo_failure"))
        version = IOSXE_TOO_OLD if too_old else IOSXE_FACTORY
        needs_upgrade = version_tuple(version) < version_tuple(MERAKI_MODE_MINIMUM)
        return {"factory_version": version, "minimum": MERAKI_MODE_MINIMUM, "needs_upgrade": needs_upgrade}


class UpgradeIosXe(DemoJob):
    """Upgrade the switch stack's IOS-XE in install mode, with pre- and post-checks."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(15, 30)

    class Meta:
        """Meta."""

        name = "ZTP Catalyst: upgrade IOS-XE"
        description = "Install the approved IOS-XE on every stack member and reload. Changes the network."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {iosxe_to, stack_members}."""
        self.work(f"install add file flash:cat9k_iosxe.{IOSXE_TARGET}.SPA.bin activate commit on {device}", **common)
        return {"iosxe_to": IOSXE_TARGET, "stack_members": 2}


class RollbackIosXe(DemoJob):
    """Roll the stack back to the committed image: the rollback of a failed upgrade."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(8, 15)

    class Meta:
        """Meta."""

        name = "ZTP Catalyst: roll back IOS-XE"
        description = "install rollback to committed: the rollback of a failed IOS-XE upgrade."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {rolled_back}."""
        self.work(f"install rollback to committed on {device}", **common)
        return {"rolled_back": True}


class ApplyOfficeProfile(DemoJob):
    """Apply the normal or EBC profile over CLI: VLANs, trunks, access ports, plus QoS and multicast for EBC."""

    device = ObjectVar(model=Device)
    office_type = StringVar(default="normal")
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "ZTP Catalyst: apply office profile"
        description = "Push the office profile to the switch over CLI. Changes the network."

    def run(self, device, office_type, **common):  # pylint: disable=arguments-differ
        """Return {config_lines, sections}."""
        self.work(f"Apply the {office_type} profile to {device}", **common)
        sections = ["vlan 100,110,120,130", "trunks to Palo and APs", "access ports 3-44", "802.1X and MAB", "AAA"]
        if office_type == "ebc":
            sections += ["vlan 140 AV", "ip igmp snooping querier", "auto qos trust dscp"]
        return {"config_lines": 180 + 40 * (office_type == "ebc"), "sections": sections}


class RestoreBootstrapConfig(DemoJob):
    """Put the bootstrap config back: the rollback of a failed profile push."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Catalyst: restore bootstrap config"
        description = "configure replace to the bootstrap config: the rollback of a failed profile push."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {restored}."""
        self.work(f"configure replace flash:ztp-bootstrap.cfg on {device}", **common)
        return {"restored": "ztp-bootstrap.cfg"}


JOBS = (CheckMerakiMode, UpgradeIosXe, RollbackIosXe, ApplyOfficeProfile, RestoreBootstrapConfig)

register_jobs(*JOBS)
