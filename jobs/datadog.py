"""Greenfield office ZTP, monitoring: add the office's devices to the Datadog SNMP pollers (10-23W 00:26).

The structure Datadog wants, which tags and which pollers, is Croissant's homework (open decision Z9); these jobs use
one poller per region and tag by site and role.
"""

from nautobot.apps.jobs import ObjectVar, register_jobs
from nautobot.dcim.models import Device, Location

from .demo import DemoJob, times_asked, timing

name = "NBO demo: ZTP Datadog"  # pylint: disable=invalid-name


class AddSnmpDevices(DemoJob):
    """Add every managed device of the office to the region's SNMP poller, with its tags."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "ZTP Datadog: add SNMP devices"
        description = "Add the office's devices to the Datadog SNMP poller with their tags. Safe to repeat."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {poller, monitored}."""
        self.work(f"Add {location}'s devices to the us-west SNMP poller", **common)
        devices = Device.objects.filter(location=location).exclude(role__name="wireless-ap").select_related("role")
        return {
            "poller": "dd-snmp-us-west-1",
            "monitored": [
                {"device": d.name, "tags": f"site:{location.name.lower()}, role:{d.role.name}, ztp:true"}
                for d in devices.order_by("name")
            ],
        }


class DevicesPolled(DemoJob):
    """A wait's check: done once Datadog has a first SNMP poll of every device."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "ZTP Datadog: devices polled (check)"
        description = "A wait's check: done when Datadog has polled every device of the office."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """The first poll comes on the next cycle: not yet the first time, done after."""
        self.work(f"Read {location}'s SNMP metrics in Datadog", **common)
        if times_asked(f"datadog:{location.pk}:{common.get('ticket')}") == 1:
            return self.check("Datadog", "not-yet", "waiting for the next 5-minute poll")
        return self.check("Datadog", "done", f"every device of {location} reports snmp.sysUpTime")


JOBS = (AddSnmpDevices, DevicesPolled)

register_jobs(*JOBS)
