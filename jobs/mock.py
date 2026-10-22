"""Mock jobs for the NBO 3 examples: each one takes 30 to 60 seconds and fails at its declared rate.

They change nothing on the network. They read and, for the lifecycle, write only Nautobot objects, so the
example workflows can be run end to end on any Nautobot with a location and a few devices.

Every job takes the same three tuning variables, which a workflow sets with `with` values:

- `fail_percent`: the chance, from 0 to 100, that the job fails. Each job declares its own default.
- `min_seconds`, `max_seconds`: how long it runs; by default between 30 and 60 seconds.
"""

import random
import time

from nautobot.apps.jobs import BooleanVar, ChoiceVar, IntegerVar, Job, ObjectVar, StringVar, TextVar, register_jobs
from nautobot.dcim.models import Device, Location

name = "NBO examples: mock jobs"  # pylint: disable=invalid-name

# The example site's lifecycle, in order. `SetLifecycle` moves a location along it; the run page draws it.
LIFECYCLE = ("Planned", "Addressed", "Installed", "Live")


class MockFailure(Exception):
    """A failure the job chose to have, at its declared rate."""


def simulate(job, fail_percent, min_seconds, max_seconds, what, rng=random):
    """Take a while, then fail with chance `fail_percent`; the reason names the rate, so a run page shows it."""
    low, high = sorted((max(0, min_seconds), max(0, max_seconds)))
    seconds = rng.uniform(low, high)
    job.logger.info("%s: working for %.0f seconds (fails %s%% of the time).", what, seconds, fail_percent)
    time.sleep(seconds)
    if rng.uniform(0, 100) < fail_percent:
        raise MockFailure(f"{what} failed (a simulated failure; this job fails {fail_percent}% of the time)")


class MockJob(Job):
    """Common tuning variables. Each job sets `fail_percent`'s default to its own declared rate."""

    fail_percent = IntegerVar(default=10, min_value=0, max_value=100, description="Chance of failing, in percent")
    min_seconds = IntegerVar(default=30, min_value=0, description="Shortest run, in seconds")
    max_seconds = IntegerVar(default=60, min_value=0, description="Longest run, in seconds")

    class Meta:
        """Meta."""

        abstract = True
        has_sensitive_variables = False

    def work(self, what, fail_percent, min_seconds, max_seconds):
        """Simulate the job's work."""
        simulate(self, fail_percent, min_seconds, max_seconds, what)


def _rate(default):
    """A `fail_percent` variable with this job's declared default."""
    return IntegerVar(
        default=default, min_value=0, max_value=100, description=f"Chance of failing, in percent (declared: {default}%)"
    )


# ------------------------------------------------------------------------------------------ site rollout jobs
class AllocateAddressing(MockJob):
    """Reserve the site's management prefix. Its rollback is ReleaseAddressing."""

    location = ObjectVar(model=Location)
    site_code = StringVar()
    fail_percent = _rate(10)

    class Meta:
        """Meta."""

        name = "Mock: allocate addressing"

    def run(self, location, site_code, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {mgmt_prefix}."""
        self.work(f"Allocate addressing for {site_code}", fail_percent, min_seconds, max_seconds)
        third = sum(map(ord, site_code)) % 250
        return {"mgmt_prefix": f"10.{third}.0.0/24"}


class ReleaseAddressing(MockJob):
    """Release what AllocateAddressing reserved: the rollback when allocation fails."""

    location = ObjectVar(model=Location)
    site_code = StringVar()
    fail_percent = _rate(0)

    class Meta:
        """Meta."""

        name = "Mock: release addressing"

    def run(self, location, site_code, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Release; never fails by default, so the undo is reliable."""
        self.work(f"Release addressing for {site_code}", fail_percent, min_seconds, max_seconds)
        return {}


class DiscoverDevices(MockJob):
    """List the devices at the location: the fan-out's items."""

    location = ObjectVar(model=Location)
    fail_percent = _rate(5)

    class Meta:
        """Meta."""

        name = "Mock: discover devices"

    def run(self, location, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {devices, count}."""
        self.work(f"Discover devices at {location}", fail_percent, min_seconds, max_seconds)
        devices = [
            {"model": "dcim.device", "id": str(device.pk), "display": device.name or str(device.pk)}
            for device in Device.objects.filter(location=location).order_by("name")
        ]
        return {"devices": devices, "count": len(devices)}


class GeneratePortMap(MockJob):
    """Build the cabling plan the field team follows: a list of mappings, shown as a table in their task."""

    location = ObjectVar(model=Location)
    fail_percent = _rate(5)

    class Meta:
        """Meta."""

        name = "Mock: generate port map"

    def run(self, location, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {port_map}."""
        self.work(f"Generate the port map for {location}", fail_percent, min_seconds, max_seconds)
        rows = []
        for index, device in enumerate(Device.objects.filter(location=location).order_by("name")[:24], start=1):
            rows.append({"device": device.name, "port": f"Gi1/0/{index}", "patch": f"PP1-{index:02d}"})
        return {"port_map": rows or [{"device": "(no devices yet)", "port": "-", "patch": "-"}]}


class OrderCircuit(MockJob):
    """Order the site's circuit from the carrier. It opens something outside NBO, so NBO never reruns it."""

    location = ObjectVar(model=Location)
    site_code = StringVar()
    fail_percent = _rate(10)

    class Meta:
        """Meta."""

        name = "Mock: order circuit"

    def run(self, location, site_code, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {order_id}; the carrier's callback matches on it."""
        self.work(f"Order a circuit for {site_code}", fail_percent, min_seconds, max_seconds)
        return {"order_id": f"ORD-{site_code}-{random.randint(1000, 9999)}"}  # noqa: S311 - a mock order number


class SetLifecycle(MockJob):
    """Move the location to a stage of the example lifecycle, creating that Status if needed."""

    location = ObjectVar(model=Location)
    stage = IntegerVar(min_value=0, max_value=len(LIFECYCLE) - 1, description="Index into the lifecycle")
    fail_percent = _rate(0)

    class Meta:
        """Meta."""

        name = "Mock: set lifecycle stage"

    def run(self, location, stage, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ,too-many-arguments
        """Set the status; the run page's lifecycle track reads it."""
        from django.contrib.contenttypes.models import ContentType  # pylint: disable=import-outside-toplevel
        from nautobot.extras.models import Status  # pylint: disable=import-outside-toplevel

        self.work(f"Mark {location} {LIFECYCLE[stage]}", fail_percent, min_seconds, max_seconds)
        status, _ = Status.objects.get_or_create(name=LIFECYCLE[stage])
        status.content_types.add(ContentType.objects.get_for_model(Location))
        location.status = status
        location.validated_save()
        return {"stage": LIFECYCLE[stage]}


class Notify(MockJob):
    """Post a message, as a chat notification would. Safe to repeat."""

    message = StringVar()
    run_id = StringVar(required=False, default="")
    fail_percent = _rate(2)

    class Meta:
        """Meta."""

        name = "Mock: notify"

    def run(self, message, fail_percent, min_seconds, max_seconds, run_id=""):  # pylint: disable=arguments-differ,too-many-arguments
        """Log the message."""
        self.work("Notify", fail_percent, min_seconds, max_seconds)
        self.logger.info("Notification: %s (run %s)", message, run_id or "-")
        return {"sent": True}


# ------------------------------------------------------------------------------------------ device change jobs
class PreCheck(MockJob):
    """Read the device: a snapshot, whether the change fits, and how risky it is."""

    device = ObjectVar(model=Device)
    fail_percent = _rate(5)

    class Meta:
        """Meta."""

        name = "Mock: pre-check"

    def run(self, device, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {snapshot, fits, risk}."""
        self.work(f"Pre-check {device}", fail_percent, min_seconds, max_seconds)
        risk = random.choice(("low", "low", "high"))  # noqa: S311 - a mock judgement
        return {"snapshot": {"device": device.name, "uptime_days": random.randint(1, 400)}, "fits": True, "risk": risk}  # noqa: S311


class Reachable(MockJob):
    """A wait's check: done when the device answers, not-yet while it doesn't, failed at its declared rate."""

    device = ObjectVar(model=Device)
    ready_percent = IntegerVar(default=60, min_value=0, max_value=100, description="Chance a check finds it ready")
    fail_percent = _rate(5)

    class Meta:
        """Meta."""

        name = "Mock: reachable (check)"

    def run(self, device, ready_percent, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ,too-many-arguments
        """Answer done, not-yet or failed, as a wait's check does."""
        try:
            self.work(f"Check {device} is reachable", fail_percent, min_seconds, max_seconds)
        except MockFailure as failure:
            return {"outcome": "failed", "message": str(failure)}  # failed ends the wait at once
        if random.uniform(0, 100) < ready_percent:  # noqa: S311
            return {"outcome": "done", "message": f"{device} answers", "latency_ms": random.randint(2, 40)}  # noqa: S311
        return {"outcome": "not-yet", "message": f"{device} doesn't answer yet"}


class RecordWindow(MockJob):
    """The maintenance window form a person fills when they start the step: its values are the step's outputs."""

    window = ChoiceVar(choices=(("tonight", "Tonight 22:00"), ("weekend", "Saturday 06:00"), ("now", "Now")))
    notes = TextVar(required=False, default="")
    confirmed = BooleanVar(default=False, description="I have told the site contact")
    fail_percent = _rate(0)

    class Meta:
        """Meta."""

        name = "Mock: record maintenance window"

    def run(self, window, notes, confirmed, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ,too-many-arguments
        """Return what the person entered."""
        # A person's form values may arrive as text: "false" isn't a confirmation.
        if str(confirmed).strip().lower() not in ("true", "1", "yes", "on"):
            raise MockFailure("the site contact hasn't been told; use Retry with changes and set confirmed")
        self.work("Record the maintenance window", fail_percent, min_seconds, max_seconds)
        return {"window": window, "notes": notes, "confirmed": confirmed}


class ApplyChange(MockJob):
    """Change the device's configuration. It changes the network: Retry asks first. Its rollback is RevertChange."""

    device = ObjectVar(model=Device)
    vlan = IntegerVar(min_value=1, max_value=4094)
    change_ticket = StringVar()
    fail_percent = _rate(20)

    class Meta:
        """Meta."""

        name = "Mock: apply change"

    def run(self, device, vlan, change_ticket, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {applied_vlan}."""
        self.work(f"Apply VLAN {vlan} on {device} for {change_ticket}", fail_percent, min_seconds, max_seconds)
        return {"applied_vlan": vlan, "config_lines": 3}


class RevertChange(MockJob):
    """Undo ApplyChange: the rollback that runs when the change fails."""

    device = ObjectVar(model=Device)
    vlan = IntegerVar(required=False)
    fail_percent = _rate(0)

    class Meta:
        """Meta."""

        name = "Mock: revert change"

    def run(self, device, fail_percent, min_seconds, max_seconds, vlan=None):  # pylint: disable=arguments-differ,too-many-arguments
        """Revert."""
        self.work(f"Revert the change on {device}", fail_percent, min_seconds, max_seconds)
        return {}


class PostCheck(MockJob):
    """Read the device again, to compare with the pre-check."""

    device = ObjectVar(model=Device)
    fail_percent = _rate(5)

    class Meta:
        """Meta."""

        name = "Mock: post-check"

    def run(self, device, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ
        """Return {snapshot}."""
        self.work(f"Post-check {device}", fail_percent, min_seconds, max_seconds)
        return {"snapshot": {"device": device.name, "uptime_days": 0}}


class RegisterInCmdb(MockJob):
    """Register the change in an outside CMDB. It opens something outside NBO: it never reruns; mark it done."""

    device = ObjectVar(model=Device)
    change_ticket = StringVar()
    fail_percent = _rate(15)

    class Meta:
        """Meta."""

        name = "Mock: register in CMDB"

    def run(self, device, change_ticket, fail_percent, min_seconds, max_seconds):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {record_id}."""
        self.work(f"Register {device} in the CMDB", fail_percent, min_seconds, max_seconds)
        return {"record_id": f"CMDB-{random.randint(10000, 99999)}"}  # noqa: S311


JOBS = (
    AllocateAddressing,
    ReleaseAddressing,
    DiscoverDevices,
    GeneratePortMap,
    OrderCircuit,
    SetLifecycle,
    Notify,
    PreCheck,
    Reachable,
    RecordWindow,
    ApplyChange,
    RevertChange,
    PostCheck,
    RegisterInCmdb,
)

register_jobs(*JOBS)
