"""Software upgrade: plan a group's upgrade with a canary, then upgrade each device with checks before and after.

A device's version is its Nautobot software version; the last step of a device's upgrade records the new one, so
the device's own page shows the change. Its peers, routes and interfaces are fixed by its name, so the snapshot
before and the snapshot after compare cleanly.
"""

from django.contrib.contenttypes.models import ContentType
from nautobot.apps.jobs import BooleanVar, ChoiceVar, JSONVar, ObjectVar, StringVar, TextVar, register_jobs
from nautobot.dcim.models import Device, SoftwareVersion
from nautobot.extras.models import DynamicGroup, Status

from .demo import DemoFailure, DemoJob, recall, ref, stable, store, times_asked, timing

name = "NBO demo: software upgrade"  # pylint: disable=invalid-name

# The images the demo knows, by version: file name, size and checksum.
IMAGES = {
    "17.9.5": ("cat9k_iosxe.17.09.05.SPA.bin", 1187, "6f0e1d8b2f3c4a59a0d7c3e1b8f9a2c4"),
    "17.12.4": ("cat9k_iosxe.17.12.04.SPA.bin", 1243, "a41c9e07d2b85f3e6c1d0a9b7e4f2c18"),
    "17.15.1": ("cat9k_iosxe.17.15.01.SPA.bin", 1302, "0c7b2e9f4a1d68e3b5f0c2a7d9e1b4f6"),
}
FALLBACK_VERSION = "17.9.4a"


def image(version):
    """Return (file, size in MB, md5) for `version`; an unknown version gets a plausible image."""
    return IMAGES.get(version) or (f"cat9k_iosxe.{version}.SPA.bin", 1250, f"{stable(version).getrandbits(128):032x}")


def running_version(device, ticket):
    """Return the version `device` runs: what this request installed, else its Nautobot software version."""
    installed = recall(f"installed:{device.pk}:{ticket}")
    if installed:
        return installed
    software = getattr(device, "software_version", None)
    return software.version if software else FALLBACK_VERSION


class PlanUpgrade(DemoJob):
    """Decide which members need the target version, and pick the canary: the first to go, alone."""

    dynamic_group = ObjectVar(model=DynamicGroup)
    target_version = StringVar()
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "Demo software: plan upgrade"
        description = "Decide which members need the target version, and pick the canary."

    def run(self, dynamic_group, target_version, **common):  # pylint: disable=arguments-differ
        """Return {plan, canary, fleet, to_upgrade, already_current, image}."""
        devices = sorted((m for m in dynamic_group.members if isinstance(m, Device)), key=lambda device: device.name)
        self.work(f"Plan {target_version} for {len(devices)} devices in {dynamic_group}", **common)
        due = [device for device in devices if running_version(device, common.get("ticket")) != target_version]
        plan = []
        for device in devices:
            current = running_version(device, common.get("ticket"))
            wave = "canary" if due and device == due[0] else ("fleet" if device in due else "-")
            action = "upgrade" if device in due else "already current"
            plan.append(
                {"device": device.name, "current": current, "target": target_version, "action": action, "wave": wave}
            )
        file_name, size, _ = image(target_version)
        return {
            "plan": plan,
            "canary": [ref(device) for device in due[:1]],
            "fleet": [ref(device) for device in due[1:]],
            "to_upgrade": len(due),
            "already_current": len(devices) - len(due),
            "image": f"{file_name} ({size} MB)",
        }


class StageImage(DemoJob):
    """Put the image on the regional file servers the devices copy from."""

    target_version = StringVar()
    min_seconds, max_seconds = timing(15, 28)

    class Meta:
        """Meta."""

        name = "Demo software: stage image"
        description = "Put the image on the regional file servers the devices copy from."

    def run(self, target_version, **common):  # pylint: disable=arguments-differ
        """Return {image_md5, servers}."""
        file_name, size, md5 = image(target_version)
        self.work(f"Stage {file_name} ({size} MB) on the file servers", **common)
        return {"image_md5": md5, "servers": ["files-nyc.example.com", "files-lon.example.com"]}


class Snapshot(DemoJob):
    """Read the device's state: version, uptime, interfaces, routing neighbours and routes."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(8, 18)

    class Meta:
        """Meta."""

        name = "Demo software: snapshot"
        description = "Read the device's version, uptime, interfaces, neighbours and routes."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {snapshot, version}."""
        self.work(f"Collect show version, interfaces, ospf and bgp from {device}", **common)
        rng = stable(device.name)
        version = running_version(device, common.get("ticket"))
        upgraded = recall(f"installed:{device.pk}:{common.get('ticket')}") is not None
        days_up = rng.randint(20, 380)  # drawn either way, so the values after it match before and after
        snapshot = {
            "version": version,
            "uptime": "4 minutes" if upgraded else f"{days_up} days",
            "interfaces_up": rng.randint(18, 46),
            "ospf_neighbours": rng.randint(2, 4),
            "bgp_peers_established": rng.randint(2, 6),
            "routes": rng.randint(1800, 5200),
            "mac_addresses": rng.randint(120, 900),
            "free_flash_mb": rng.randint(1900, 4200) - (image(version)[1] if upgraded else 0),
        }
        return {"snapshot": snapshot, "version": version}


class CheckFlash(DemoJob):
    """Check the device has room for the image."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "Demo software: check flash"
        description = "Check the device has room for the image."

    def run(self, device, target_version, **common):  # pylint: disable=arguments-differ
        """Return {free_mb, needed_mb, fits}; fail when it doesn't fit."""
        self.work(f"Read free space on {device}'s flash", **common)
        free, needed = stable(device.name).randint(1900, 4200), image(target_version)[1]
        if free < needed:
            raise DemoFailure(f"{device} has {free} MB free and the image needs {needed} MB: remove old images")
        return {"free_mb": free, "needed_mb": needed, "fits": True}


class CopyImage(DemoJob):
    """Copy the image to the device's flash from the nearest file server."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    min_seconds, max_seconds = timing(20, 30)

    class Meta:
        """Meta."""

        name = "Demo software: copy image"
        description = "Copy the image to the device's flash. Copying again overwrites it."

    def run(self, device, target_version, **common):  # pylint: disable=arguments-differ
        """Return {copied, seconds}."""
        file_name, size, _ = image(target_version)
        self.work(f"Copy {file_name} ({size} MB) to {device} flash:", **common)
        return {"copied": file_name, "transfer_mbps": stable(device.name, "copy").randint(80, 400)}


class VerifyImage(DemoJob):
    """Verify the copied image's MD5 against the staged one."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    image_md5 = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 15)
    failure = "image-corrupt"
    failure_message = (
        "verify /md5 gave 9b1f0c… but the staged image is a41c9e…: a link flap truncated the copy, "
        "which has since resumed. Retry to verify again"
    )

    class Meta:
        """Meta."""

        name = "Demo software: verify image"
        description = "Verify the copied image's MD5 against the staged one."

    def run(self, device, target_version, image_md5, **common):  # pylint: disable=arguments-differ
        """Return {md5}."""
        self.work(f"verify /md5 flash:{image(target_version)[0]} on {device}", **common)
        return {"md5": image_md5 or image(target_version)[2], "verified": True}


class InstallImage(DemoJob):
    """Install the image and reload: the step a person starts, from the job's own form. Its rollback reverts."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    reload = ChoiceVar(choices=(("now", "Reload now"), ("scheduled", "At the window's start")), default="now")
    console_confirmed = BooleanVar(default=False, description="I have console access to the device")
    notes = TextVar(required=False, default="")
    min_seconds, max_seconds = timing(20, 30)
    failure = "install-fails"
    failure_message = (
        "install add failed: SMU 'CSCwh87343' must be deactivated first. The device still runs its old version; "
        "it has been deactivated now, so Retry installs again"
    )

    class Meta:
        """Meta."""

        name = "Demo software: install and reload"
        description = "Install the image and reload. A person starts it. Changes the network."

    def run(self, device, target_version, reload, console_confirmed, notes, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {installed, reload}; refuse to start without console access."""
        # A person's form values may arrive as text: "false" isn't a confirmation.
        if str(console_confirmed).strip().lower() not in ("true", "1", "yes", "on"):
            raise DemoFailure("console access isn't confirmed: use Retry with changes and set console_confirmed")
        self.work(f"install add activate commit {image(target_version)[0]} on {device}, reload {reload}", **common)
        store(f"installed:{device.pk}:{common.get('ticket')}", target_version)
        return {"installed": target_version, "reload": reload, "notes": notes}


class RollbackImage(DemoJob):
    """Roll the device back to the version it ran: the rollback when the install fails."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "Demo software: roll back image"
        description = "Roll the device back to the version it ran: the rollback of a failed install."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {rolled_back_to}."""
        self.work(f"install rollback to committed on {device}", **common)
        store(f"installed:{device.pk}:{common.get('ticket')}", None)
        return {"rolled_back_to": running_version(device, common.get("ticket"))}


class DeviceBack(DemoJob):
    """A wait's check: not yet while the device reboots, done once it answers on its new version."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "Demo software: device back (check)"
        description = "A wait's check: done once the device answers on its new version."

    def run(self, device, target_version, **common):  # pylint: disable=arguments-differ
        """The first check finds the device rebooting; the next finds it back."""
        self.work(f"Poll {device} over SSH and SNMP", **common)
        if times_asked(f"back:{device.pk}:{common.get('ticket')}") == 1:
            return self.check("Device back", "not-yet", f"{device} is reloading: no answer on SSH yet")
        version = running_version(device, common.get("ticket"))
        if version != target_version:
            return self.check("Device back", "failed", f"{device} came back on {version}, not {target_version}")
        return self.check(
            "Device back", "done", f"{device} answers on {version}", boot_seconds=stable(device.name).randint(380, 610)
        )


class ComparePrePost(DemoJob):
    """Compare the snapshots: the version must change, and every neighbour and peer must be back."""

    device = ObjectVar(model=Device)
    pre = JSONVar()
    post = JSONVar()
    min_seconds, max_seconds = timing(5, 10)
    failure = "peer-lost"
    failure_message = (
        "BGP peer 10.255.0.2 (AS 65010) is Idle after the upgrade; it was Established before. "
        "Check the peer, then Retry"
    )

    class Meta:
        """Meta."""

        name = "Demo software: compare before and after"
        description = "Compare the snapshots: the version changed and every neighbour is back."

    def run(self, device, pre, post, **common):  # pylint: disable=arguments-differ
        """Return {differences, verdict}; fail when anything that matters got worse."""
        self.work(f"Compare {device} before and after", **common)
        differences = [
            {"check": key, "before": pre.get(key), "after": post.get(key)}
            for key in sorted(set(pre) | set(post))
            if pre.get(key) != post.get(key)
        ]
        for key in ("ospf_neighbours", "bgp_peers_established"):
            if (post.get(key) or 0) < (pre.get(key) or 0):
                raise DemoFailure(f"{key} went from {pre.get(key)} to {post.get(key)}")
        return {
            "differences": differences,
            "verdict": f"upgraded {pre.get('version')} → {post.get('version')}, nothing lost",
        }


class RecordVersion(DemoJob):
    """Record the device's new version in Nautobot, creating the software version if it's new."""

    device = ObjectVar(model=Device)
    target_version = StringVar()
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "Demo software: record version in Nautobot"
        description = "Set the device's software version in Nautobot. Setting it twice changes nothing."

    def run(self, device, target_version, **common):  # pylint: disable=arguments-differ
        """Return {software_version}; skip when the device has no platform."""
        self.work(f"Set {device}'s software version to {target_version}", **common)
        if device.platform is None:
            self.logger.warning("%s has no platform, so it can't have a software version.", device)
            return {"software_version": target_version}
        active, _ = Status.objects.get_or_create(name="Active")
        active.content_types.add(ContentType.objects.get_for_model(SoftwareVersion))
        software, _ = SoftwareVersion.objects.get_or_create(
            platform=device.platform, version=target_version, defaults={"status": active}
        )
        device.software_version = software
        device.validated_save()
        return {"software_version": str(software)}


JOBS = (
    PlanUpgrade,
    StageImage,
    Snapshot,
    CheckFlash,
    CopyImage,
    VerifyImage,
    InstallImage,
    RollbackImage,
    DeviceBack,
    ComparePrePost,
    RecordVersion,
)

register_jobs(*JOBS)
