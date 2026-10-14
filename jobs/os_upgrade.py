"""Mock OS upgrade jobs and a mock wait check, for trying an NBO campaign.

A campaign opens one child record per device. Each child runs the same procedure: five pre-checks, a free space
check, an upload, a manual install and reboot, a wait for the device to come back, a post-check and a validation
that compares the pre-checks with the post-check. None of these jobs touches the network or Nautobot's data: they
make up plausible numbers, from the device's ID so that each device gives the same ones every run.

The jobs use `NBOStepStatusMixin`, unlike the site build demo's. The install and the reboot are manual steps that a
person runs from the step's Job Form, outside NBO's own start, and the mixin's hooks are what record such a run as the
step's attempt and set the step's status. The chained steps use it too, so every job reads its step the same way.
Each job writes what it found into the record's workflow data, under a key of its own, because the merge keeps keys,
not the values inside them.
"""

import random
import time
from datetime import datetime

from django.utils import timezone
from nautobot.apps.jobs import IntegerVar, Job, ObjectVar, StringVar
from nautobot.dcim.models import Device

from nautobot_orchestrator.checks import CheckResult
from nautobot_orchestrator.choices import CheckOutcome
from nautobot_orchestrator.jobs.workflow_jobs import NBOStepStatusMixin
from nautobot_orchestrator.utilities.utils import create_or_update_workflow_data

name = "NBO OS upgrade demo"  # pylint: disable=invalid-name

#: The five things a pre-check and the post-check look at, and the workflow data key of each pre-check.
CHECKS = ("interfaces", "routing", "neighbors", "processor", "uptime")
#: The image the upload sends when the campaign's intake gives no size, in MB.
DEFAULT_IMAGE_MB = 1450


class ProvokedStop(RuntimeError):
    """The failure a mock job raises on purpose."""


def baseline(device):
    """Return the numbers a healthy `device` reports, the same every time for the same device."""
    rng = random.Random(str(device.pk))  # noqa: S311  # demo numbers, not security
    return {
        "interfaces": {"up": rng.randint(12, 48), "down": rng.randint(0, 3)},
        "routing": {"routes": rng.randint(800, 4000), "default_route": True},
        "neighbors": {"bgp": rng.randint(2, 6), "ospf": rng.randint(1, 4)},
        "processor": {"cpu_percent": rng.randint(5, 35)},
        "uptime": {"days": rng.randint(30, 400)},
    }


class MockDeviceJob(NBOStepStatusMixin, Job):
    """Sleep for a random time between `min_seconds` and `max_seconds`, then fail with `failure_percent` chance.

    The mixin declares `nbo_step`, which NBO fills in, and finishes the step when the job ends.
    """

    device = ObjectVar(model=Device, description="The record's device; NBO fills it in.")
    min_seconds = IntegerVar(label="Shortest run (s)", default=10, min_value=1, max_value=600)
    max_seconds = IntegerVar(label="Longest run (s)", default=45, min_value=1, max_value=600)
    failure_percent = IntegerVar(label="Failure chance (%)", default=5, min_value=0, max_value=100)

    #: What the job pretends to do, for its log lines and its failure reason.
    action = "work on the device"

    class Meta:
        """Shared Meta: room for a long upload under Nautobot's default 300-second soft limit."""

        has_sensitive_variables = False
        soft_time_limit = 900
        time_limit = 960

    def run(  # pylint: disable=arguments-differ
        self, *, device, nbo_step=None, min_seconds=10, max_seconds=45, failure_percent=5, **kwargs
    ):
        """Pretend to do `action` on `device`, then stop on purpose or call `finish`."""
        low, high = sorted((int(min_seconds), int(max_seconds)))
        duration = random.randint(low, high)  # noqa: S311
        fails = random.randint(1, 100) <= int(failure_percent)  # noqa: S311
        stop_at = random.randint(1, duration) if fails else duration  # noqa: S311
        self.logger.info("Starting to %s on %s; this run takes up to %ss.", self.action, device.name, duration)
        for detail, value in sorted(kwargs.items()):
            self.logger.info("Input %s = %r", detail, value)
        elapsed = 0
        while elapsed < stop_at:
            pause = min(15, stop_at - elapsed)
            time.sleep(pause)
            elapsed += pause
            self.logger.info("Still working to %s on %s: %ss of %ss.", self.action, device.name, elapsed, duration)
        if fails:
            reason = (
                f"Provoked stop: {self.Meta.name} was stopped on purpose after {elapsed}s on {device.name} "
                f"(failure chance {failure_percent}%), to show how NBO handles a failed step."
            )
            self.logger.error(reason)
            raise ProvokedStop(reason)
        return self.finish(device, nbo_step, **kwargs)

    def finish(self, device, nbo_step, **kwargs):  # pylint: disable=unused-argument
        """Do the job's own part once the wait is over, and return what it found."""
        self.logger.info("Done: %s on %s.", self.action, device.name)
        return {}

    def write(self, nbo_step, data):
        """Merge `data` into the step's record's workflow data, when NBO passed the step."""
        if nbo_step is None:
            self.logger.warning("Run without an NBO step, so nothing is written to workflow data.")
            return
        create_or_update_workflow_data(job=self, nbo_record=nbo_step.nbo_record, workflow_data=data)

    @staticmethod
    def workflow_data(nbo_step):
        """Return the step's record's workflow data, or an empty dict."""
        return dict(nbo_step.nbo_record.workflow_data or {}) if nbo_step else {}


class MockPreCheck(MockDeviceJob):
    """Record one of the device's numbers before the upgrade, under `pre_check_<check>`."""

    check = ""

    def finish(self, device, nbo_step, **kwargs):
        """Record the device's number for `check`."""
        found = baseline(device)[self.check]
        self.logger.info("%s on %s: %s", self.check.title(), device.name, found)
        self.write(nbo_step, {f"pre_check_{self.check}": found})
        return found


class PreCheckInterfaces(MockPreCheck):
    """Pretend to count the interfaces that are up and down."""

    check, action = "interfaces", "check interfaces"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Pre-check interfaces"
        description = "Mock: records how many interfaces are up and down."


class PreCheckRouting(MockPreCheck):
    """Pretend to count the routes and look for a default route."""

    check, action = "routing", "check the routing table"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Pre-check routing"
        description = "Mock: records the route count and the default route."


class PreCheckNeighbors(MockPreCheck):
    """Pretend to count BGP and OSPF neighbors."""

    check, action = "neighbors", "check routing neighbors"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Pre-check neighbors"
        description = "Mock: records the BGP and OSPF neighbor counts."


class PreCheckProcessor(MockPreCheck):
    """Pretend to read the processor load."""

    check, action = "processor", "check the processor"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Pre-check processor"
        description = "Mock: records the processor load."


class PreCheckUptime(MockPreCheck):
    """Pretend to read the uptime."""

    check, action = "uptime", "check the uptime"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Pre-check uptime"
        description = "Mock: records the uptime."


class CheckFreeSpace(MockDeviceJob):
    """Fail, so the upload never starts, when the image doesn't fit on the device's flash."""

    # Optional, so a device record with no campaign above it still runs, with the default size, rather than failing
    # to start on an empty value.
    image_size_mb = IntegerVar(
        label="Image size (MB)", required=False, default=DEFAULT_IMAGE_MB, min_value=1, max_value=20000
    )
    short_percent = IntegerVar(label="Chance of too little space (%)", default=20, min_value=0, max_value=100)
    action = "check free space on flash"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Check free space"
        description = "Mock: fails when the image wouldn't fit, so the upload after it never starts."

    def finish(self, device, nbo_step, image_size_mb=DEFAULT_IMAGE_MB, short_percent=20, **kwargs):
        """Record the free space, and fail when it is less than the image needs."""
        needed = int(image_size_mb or DEFAULT_IMAGE_MB)
        short = random.randint(1, 100) <= int(short_percent)  # noqa: S311
        free = random.randint(needed // 3, needed - 1) if short else random.randint(needed + 200, needed * 4)  # noqa: S311
        self.write(nbo_step, {"free_space": {"free_mb": free, "needed_mb": needed}})
        if short:
            reason = (
                f"Not enough free space on {device.name}: {free} MB free, {needed} MB needed. The upload doesn't run. "
                "Free space on flash, then retry this step."
            )
            self.logger.error(reason)
            raise ProvokedStop(reason)
        self.logger.info("%s has %s MB free; the image needs %s MB.", device.name, free, needed)
        return {"free_mb": free, "needed_mb": needed}


class UploadOS(MockDeviceJob):
    """Pretend to copy the image to the device's flash."""

    target_version = StringVar(label="Target version", required=False, description="From the campaign's intake.")
    action = "upload the image"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Upload OS"
        description = "Mock: pretends to copy the image. Runs only once the free space check has passed."

    def finish(self, device, nbo_step, target_version="", **kwargs):
        """Record the uploaded version, or fail when the campaign gave none."""
        if not target_version:
            reason = "No target version: submit the campaign record's intake first, then retry this step."
            self.logger.error(reason)
            raise ProvokedStop(reason)
        self.write(nbo_step, {"upload": {"version": target_version}})
        self.logger.info("Uploaded %s to %s.", target_version, device.name)
        return {"version": target_version}


class InstallOS(MockDeviceJob):
    """Pretend to set the uploaded image as the boot image. A person runs it from its job form."""

    target_version = StringVar(label="Target version", required=False)
    action = "install the image"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Install OS"
        description = "Mock: pretends to set the boot image. Run it from the step's Job Form."

    def finish(self, device, nbo_step, target_version="", **kwargs):
        """Record the installed version."""
        self.write(nbo_step, {"install": {"version": target_version}})
        self.logger.info("%s will boot %s.", device.name, target_version or "the uploaded image")
        return {"version": target_version}


class RebootDevice(MockDeviceJob):
    """Pretend to reload the device, and record when, for the wait after it."""

    action = "reload the device"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Reboot device"
        description = "Mock: records the reload time. Run it from the step's Job Form."

    def finish(self, device, nbo_step, **kwargs):
        """Record the time of the reload."""
        at = timezone.now().isoformat()
        self.write(nbo_step, {"reboot": {"at": at}})
        self.logger.info("%s is reloading.", device.name)
        return {"at": at}


class DeviceBackAfterReboot(Job):
    """A wait check: done once the device is "back", a few minutes after its reload, or never for a few devices.

    Each device comes back after the same 2 to 8 minutes every run, from its ID, and about one device in ten never
    does, so its wait ends Failed at the step's one-hour deadline.
    """

    class Meta:
        """Meta: a check, which the NBO check runner calls; nobody runs it."""

        name = "OS upgrade demo - Device back after reboot"
        description = "Mock wait check: done a few minutes after the reload recorded by the reboot step."
        has_sensitive_variables = False
        hidden = True
        nbo_check = True

    @classmethod
    def check(cls, step):
        """Return done once the device's mock return time has passed since its reload."""
        record = step.nbo_record
        device = record.device
        reboot_at = ((record.workflow_data or {}).get("reboot") or {}).get("at")
        if device is None:
            return CheckResult(CheckOutcome.ERROR, "The record has no device.")
        if not reboot_at:
            return CheckResult(CheckOutcome.ERROR, "No reload recorded: run the reboot step's job first.")
        rng = random.Random(f"back-{device.pk}")  # noqa: S311
        if rng.randint(1, 10) == 1:
            return CheckResult(CheckOutcome.NOT_YET, f"{device.name} doesn't answer yet; it never will in this demo.")
        back_after = rng.randint(120, 480)
        waited = (timezone.now() - datetime.fromisoformat(reboot_at)).total_seconds()
        if waited < back_after:
            return CheckResult(
                CheckOutcome.NOT_YET, f"{device.name} doesn't answer yet, {int(waited)}s after its reload."
            )
        return CheckResult(
            CheckOutcome.DONE,
            f"{device.name} answers again, {int(waited)}s after its reload.",
            {"after_s": int(waited)},
        )

    def run(self, *args, **kwargs):
        """Not used: the check runner calls `check`."""


class PostCheck(MockDeviceJob):
    """Record the device's five numbers after the upgrade, under `post_check`."""

    mismatch_percent = IntegerVar(label="Chance of a lost neighbor (%)", default=15, min_value=0, max_value=100)
    action = "run the post-checks"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Post-check"
        description = "Mock: records the same five numbers as the pre-checks, after the reload."

    def finish(self, device, nbo_step, mismatch_percent=15, **kwargs):
        """Record the numbers after the reload: the uptime starts again, and now and then a neighbor is lost."""
        found = baseline(device)
        found["uptime"] = {"days": 0}
        found["processor"] = {"cpu_percent": min(99, found["processor"]["cpu_percent"] + random.randint(-3, 8))}  # noqa: S311
        if random.randint(1, 100) <= int(mismatch_percent):  # noqa: S311
            found["neighbors"] = {**found["neighbors"], "bgp": found["neighbors"]["bgp"] - 1}
            self.logger.warning("%s came back with one BGP neighbor fewer.", device.name)
        self.write(nbo_step, {"post_check": found})
        self.logger.info("Post-check on %s: %s", device.name, found)
        return found


class ValidateUpgrade(MockDeviceJob):
    """Compare the pre-checks with the post-check, and fail on any difference that matters."""

    action = "compare the pre-checks with the post-check"

    class Meta(MockDeviceJob.Meta):
        """Meta."""

        name = "OS upgrade demo - Validate upgrade"
        description = "Mock: compares the pre-checks with the post-check, and fails on a difference."

    def finish(self, device, nbo_step, **kwargs):
        """List each difference; fail when interfaces, routes or neighbors differ, or the uptime didn't reset."""
        data = self.workflow_data(nbo_step)
        post = data.get("post_check")
        pre = {check: data.get(f"pre_check_{check}") for check in CHECKS}
        missing = [f"pre_check_{check}" for check, value in pre.items() if value is None] + (
            ["post_check"] * (not post)
        )
        if missing:
            reason = f"Can't validate {device.name}: workflow data has no {', '.join(missing)}."
            self.logger.error(reason)
            raise ProvokedStop(reason)
        problems = []
        for check in ("interfaces", "routing", "neighbors"):
            if pre[check] != post[check]:
                problems.append(f"{check} changed from {pre[check]} to {post[check]}")
        if post["uptime"]["days"] >= pre["uptime"]["days"]:
            problems.append("the uptime didn't reset, so the device may not have reloaded")
        cpu_change = post["processor"]["cpu_percent"] - pre["processor"]["cpu_percent"]
        self.logger.info("Processor load changed by %+d points; that alone doesn't fail the upgrade.", cpu_change)
        self.write(nbo_step, {"validation": {"passed": not problems, "problems": problems}})
        if problems:
            reason = f"{device.name} doesn't match its pre-checks: {'; '.join(problems)}."
            self.logger.error(reason)
            raise ProvokedStop(reason)
        self.logger.info("%s matches its pre-checks: interfaces, routing and neighbors are unchanged.", device.name)
        return {"passed": True}
