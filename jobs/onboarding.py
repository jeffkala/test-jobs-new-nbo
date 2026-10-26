"""Device readiness: check the device's Nautobot record, reach it, and register it in every system that must know it.

The record check reads the real record: a device without a primary IPv4 address, a platform or a serial fails it,
so the demo can show a person fixing Nautobot and retrying. The seed job leaves one such device.
"""

from nautobot.apps.jobs import IntegerVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoFailure, DemoJob, set_status, stable, times_asked, timing

name = "NBO demo: onboarding"  # pylint: disable=invalid-name

LIFECYCLE = ("Planned", "Staged", "Validated", "Active")
# What registering a device means in each system the readiness fan-out reaches.
SYSTEMS = {
    "ise": ("Cisco ISE", "network device with RADIUS and TACACS+ shared secrets, in device group Location#{site}"),
    "monitoring": ("LibreNMS", "SNMPv3 device on poller group {site}, alerting to #noc"),
    "dns": ("Infoblox", "A and PTR records in the mgmt.example.com zone"),
    "backup": ("Oxidized", "nightly config backup to the configs Git repository"),
    "cmdb": ("ServiceNow CMDB", "configuration item in the cmdb_ci_netgear class, supported by Network Operations"),
    "syslog": ("Splunk", "syslog source with the network_device sourcetype"),
}


def mgmt_address(device):
    """Return the device's primary IPv4 address without its prefix length, or ''."""
    return str(device.primary_ip4.host) if device.primary_ip4 else ""


class SetLifecycle(DemoJob):
    """Move the device along the onboarding lifecycle: Planned, Staged, Validated, Active."""

    device = ObjectVar(model=Device)
    stage = IntegerVar(min_value=0, max_value=len(LIFECYCLE) - 1, description="Index into the lifecycle")
    min_seconds, max_seconds = timing(2, 5)

    class Meta:
        """Meta."""

        name = "Demo onboarding: set lifecycle stage"
        description = "Set the device's status to a stage of the onboarding lifecycle."

    def run(self, device, stage, **common):  # pylint: disable=arguments-differ
        """Return {stage}."""
        self.work(f"Mark {device} {LIFECYCLE[stage]}", **common)
        return {"stage": set_status(device, LIFECYCLE[stage])}


class ValidateRecord(DemoJob):
    """Check the device's Nautobot record has what onboarding needs: address, platform, serial, location, role."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(5, 12)

    class Meta:
        """Meta."""

        name = "Demo onboarding: validate record"
        description = "Check the Nautobot record has an address, a platform, a serial, a location and a role."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {mgmt_ip, platform, serial, checks}; fail naming every gap."""
        self.work(f"Validate {device}'s Nautobot record", **common)
        checks = [
            ("primary IPv4 address", mgmt_address(device)),
            ("platform", device.platform.name if device.platform else ""),
            ("serial number", device.serial),
            ("location", device.location.name if device.location else ""),
            ("role", device.role.name if device.role else ""),
        ]
        gaps = [what for what, value in checks if not value]
        if gaps:
            raise DemoFailure(f"{device}'s record has no {', no '.join(gaps)}: fix the device in Nautobot, then Retry")
        return {
            "mgmt_ip": mgmt_address(device),
            "platform": device.platform.name,
            "serial": device.serial,
            "checks": [{"check": what, "value": value, "result": "ok"} for what, value in checks],
        }


class Reachable(DemoJob):
    """A wait's check: done when the device answers ping and SSH on its management address."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "Demo onboarding: reachable (check)"
        description = "A wait's check: done when the device answers ping and SSH."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Answer done with the round trip."""
        self.work(f"Ping and SSH to {mgmt_address(device) or device}", **common)
        return self.check(
            "Reachable",
            "done",
            f"{device} answers on {mgmt_address(device)}",
            rtt_ms=stable(device.name).randint(1, 45),
        )


class DiscoverFacts(DemoJob):
    """Log in and read the device's facts, and compare its serial with the record."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(10, 20)
    failure = "serial-mismatch"
    failure_message = ""  # this failure isn't an error: the serial differs, and a person decides

    class Meta:
        """Meta."""

        name = "Demo onboarding: discover facts"
        description = "Log in, read the device's facts, and compare its serial with the record."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {facts, serial_matches}."""
        self.work(f"Read show version and show inventory from {device}", **{**common, "demo_failure": "none"})
        rng = stable(device.name)
        mismatch = self.failure_due(common.get("ticket"), common.get("demo_failure"))
        found = f"FOC{rng.randint(2000, 2999)}X{rng.randint(100, 999)}" if mismatch else device.serial
        facts = {
            "hostname": device.name,
            "serial": found,
            "model": str(device.device_type.model),
            "version": device.software_version.version if getattr(device, "software_version", None) else "17.9.4a",
            "interfaces": device.interfaces.count(),
            "uptime": f"{rng.randint(1, 6)} hours",
        }
        return {"facts": facts, "serial_matches": found == device.serial}


class Register(DemoJob):
    """Register the device in one system. It opens a record outside NBO, so NBO never reruns it."""

    system = StringVar()
    hostname = StringVar()
    mgmt_ip = StringVar(required=False, default="")
    site = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 22)
    failure = "ise-rejects"

    class Meta:
        """Meta."""

        name = "Demo onboarding: register in a system"
        description = "Register the device in one system, such as ISE or monitoring. Opens a record outside NBO."

    def run(self, system, hostname, mgmt_ip, site, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {record_id, record_url, registered_as}."""
        product, what = SYSTEMS.get(system, (system, "record"))
        self.failure_message = (
            f"{product} answered 409: a network device with IP {mgmt_ip} already exists (left over from the RMA). "
            "Check it in ISE, then Mark done with its record_id"
        )
        self.work(
            f"Register {hostname} ({mgmt_ip}) in {product}",
            **(common if system == "ise" else {**common, "demo_failure": "none"}),
        )
        record = f"{system.upper()}-{stable(system, hostname).randint(10000, 99999)}"
        return {
            "record_id": record,
            "record_url": f"https://{system}.example.com/records/{record}",
            "registered_as": what.format(site=site or "default"),
        }


class PushAaa(DemoJob):
    """Point the device's RADIUS and TACACS+ at ISE. Its rollback removes the lines again."""

    hostname = StringVar()
    mgmt_ip = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(10, 18)

    class Meta:
        """Meta."""

        name = "Demo onboarding: push AAA config"
        description = "Point the device's RADIUS and TACACS+ at ISE. Changes the network."

    def run(self, hostname, mgmt_ip, **common):  # pylint: disable=arguments-differ
        """Return {aaa_lines}."""
        self.work(f"Configure aaa group server tacacs+ ISE-TACACS on {hostname}", **common)
        return {
            "aaa_lines": [
                "aaa group server tacacs+ ISE-TACACS",
                " server name ise-01",
                " server name ise-02",
                f" ip tacacs source-interface Vlan999 ! {mgmt_ip}",
            ]
        }


class RemoveAaa(DemoJob):
    """Remove the AAA lines PushAaa added: its rollback."""

    hostname = StringVar()
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo onboarding: remove AAA config"
        description = "Remove the AAA lines: the rollback of a failed push."

    def run(self, hostname, **common):  # pylint: disable=arguments-differ
        """Return {removed}."""
        self.work(f"Remove the ISE-TACACS group from {hostname}", **common)
        return {"removed": True}


class RecordHealthy(DemoJob):
    """A wait's check: done when the system shows the device healthy, such as monitoring's first poll."""

    system = StringVar()
    hostname = StringVar()
    record_id = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(4, 10)
    failure = "monitoring-not-polling"

    class Meta:
        """Meta."""

        name = "Demo onboarding: record healthy (check)"
        description = "A wait's check: done when the system shows the device healthy."

    def run(self, system, hostname, record_id, **common):  # pylint: disable=arguments-differ
        """Monitoring's first poll comes a minute later; a picked failure says SNMP times out."""
        product = SYSTEMS.get(system, (system,))[0]
        self.failure_message = (
            f"{product} can't poll {hostname}: SNMPv3 authentication fails (wrong auth key in the credential set)"
        )
        try:
            self.work(
                f"Ask {product} about {record_id}",
                **(common if system == "monitoring" else {**common, "demo_failure": "none"}),
            )
        except DemoFailure as failure:
            return self.check(f"{product} health", "failed", str(failure))
        if system == "monitoring" and times_asked(f"polled:{hostname}:{common.get('ticket')}") == 1:
            return self.check(f"{product} health", "not-yet", f"{hostname} is queued for its first poll")
        return self.check(f"{product} health", "done", f"{product} shows {hostname} healthy ({record_id})")


class SoakClean(DemoJob):
    """A wait's check: done when the device has run without alarms, flaps or errors through its soak."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "Demo onboarding: soak clean (check)"
        description = "A wait's check: done when the device has run clean through its soak."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Answer done with the soak's numbers."""
        self.work(f"Read {device}'s alarms, flaps and interface errors", **common)
        return self.check("Soak", "done", f"{device} ran clean: no alarms, no flaps, no CRC errors", alarms=0, flaps=0)


JOBS = (SetLifecycle, ValidateRecord, Reachable, DiscoverFacts, Register, PushAaa, RemoveAaa, RecordHealthy, SoakClean)

register_jobs(*JOBS)
