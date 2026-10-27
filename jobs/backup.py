"""Greenfield office ZTP, config backup: enroll the office's devices (MVP "Config Backup and Restore")."""

from nautobot.apps.jobs import ObjectVar, register_jobs
from nautobot.dcim.models import Device, Location

from .demo import DemoJob, timing

name = "NBO demo: ZTP backup"  # pylint: disable=invalid-name


class EnrollDevices(DemoJob):
    """Enroll the office's CLI-managed devices in the nightly config backup, and take the first backup now."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "ZTP backup: enroll devices"
        description = "Enroll the office's devices in the nightly config backup and take a first one. Safe to repeat."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {enrolled}; Panorama and the Dashboard back up the devices they manage."""
        self.work(f"Enroll {location}'s devices in config backup", **common)
        devices = Device.objects.filter(
            location=location, role__name__in=("office-switch", "console-server", "ups", "pdu")
        )
        return {"enrolled": [{"device": d.name, "first_backup": "ok"} for d in devices.order_by("name")]}


JOBS = (EnrollDevices,)

register_jobs(*JOBS)
