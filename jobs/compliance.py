"""Configuration compliance: audit a group of devices against the golden config, then fix each one that drifted.

Which features drift on which device is fixed by the device's name, so a demo shows the same report every time.
A device a request remediated stays compliant for that request's ticket, so the re-audit shows the improvement.
"""

from nautobot.apps.jobs import BooleanVar, IntegerVar, JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import DynamicGroup

from .demo import DemoFailure, DemoJob, recall, ref, stable, store, timing

name = "NBO demo: compliance"  # pylint: disable=invalid-name

# Each feature of the golden config: the lines a drifted device lacks, and whether fixing it can lock people out.
FEATURES = {
    "aaa": (["aaa new-model", "aaa authentication login default group ISE-TACACS local"], True),
    "ntp": (["ntp server 10.10.0.11 prefer", "ntp server 10.10.0.12"], False),
    "snmp": (["snmp-server group NETOPS v3 priv", "no snmp-server community public"], False),
    "logging": (["logging host 10.10.0.21 transport tcp port 6514", "logging trap informational"], False),
    "banner": (["banner login ^Authorised access only. Activity is monitored.^"], False),
    "ssh": (["ip ssh version 2", "ip ssh time-out 60", "no ip http server"], True),
}


def drifted(device, features, ticket):
    """Return the features `device` fails: about one in four, the same every time, none once remediated."""
    if recall(f"remediated:{device.pk}:{ticket}"):
        return []
    return [feature for feature in features if feature in FEATURES and stable(device.name, feature).random() < 0.27]


def members(dynamic_group):
    """Return the group's devices, by name."""
    return sorted((m for m in dynamic_group.members if isinstance(m, Device)), key=lambda device: device.name)


class BackupConfigs(DemoJob):
    """Back up every member's running config before anything is judged or changed."""

    dynamic_group = ObjectVar(model=DynamicGroup)
    min_seconds, max_seconds = timing(10, 22)

    class Meta:
        """Meta."""

        name = "Demo compliance: back up configs"
        description = "Back up every member's running config."

    def run(self, dynamic_group, **common):  # pylint: disable=arguments-differ
        """Return {backed_up, backups}."""
        devices = members(dynamic_group)
        self.work(f"Back up {len(devices)} devices in {dynamic_group}", **common)
        backups = [
            {
                "device": device.name,
                "lines": stable(device.name).randint(350, 1400),
                "commit": f"{stable(device.name, 'git').getrandbits(28):07x}",
            }
            for device in devices
        ]
        return {"backed_up": len(backups), "backups": backups}


class RunCompliance(DemoJob):
    """Compare every member's config with the golden config, feature by feature."""

    dynamic_group = ObjectVar(model=DynamicGroup)
    features = JSONVar(description="The features to check, such as [aaa, ntp, snmp]")
    min_seconds, max_seconds = timing(12, 25)

    class Meta:
        """Meta."""

        name = "Demo compliance: run compliance"
        description = "Compare every member's config with the golden config, feature by feature."

    def run(self, dynamic_group, features, **common):  # pylint: disable=arguments-differ
        """Return {score, compliant_devices, noncompliant, noncompliant_count, report}."""
        devices = members(dynamic_group)
        self.work(f"Check {len(devices)} devices for {', '.join(features)}", **common)
        report, noncompliant, passed = [], [], 0
        for device in devices:
            failing = drifted(device, features, common.get("ticket"))
            passed += len(features) - len(failing)
            if failing:
                noncompliant.append(ref(device))
            report.append({"device": device.name, "compliant": not failing, "failing": ", ".join(failing) or "-"})
        checks = len(devices) * len(features) or 1
        return {
            "score": round(100 * passed / checks),
            "compliant_devices": len(devices) - len(noncompliant),
            "noncompliant": noncompliant,
            "noncompliant_count": len(noncompliant),
            "report": report,
        }


class CheckDevice(DemoJob):
    """Compare one device's config with the golden config; with `must_pass`, fail unless it complies."""

    device = ObjectVar(model=Device)
    features = JSONVar()
    must_pass = BooleanVar(default=False, description="Fail unless the device complies: the check after a fix")
    min_seconds, max_seconds = timing(8, 16)
    failure = "still-noncompliant"
    failure_message = "ntp still fails after the fix: the device kept 'ntp server 10.1.1.1' from a local override"

    class Meta:
        """Meta."""

        name = "Demo compliance: check one device"
        description = "Compare one device's config with the golden config."

    def run(self, device, features, must_pass, **common):  # pylint: disable=arguments-differ
        """Return {compliance, failing, risk, score}."""
        failing = drifted(device, features, common.get("ticket"))
        what = f"Check {device} for {', '.join(features)}"
        if must_pass:
            self.work(what, **common)
            if failing:
                raise DemoFailure(f"{device} still fails {', '.join(failing)}")
        else:
            self.work(what, **{**common, "demo_failure": "none"})
        risky = any(FEATURES[feature][1] for feature in failing)
        return {
            "compliance": {feature: "fail" if feature in failing else "pass" for feature in features},
            "failing": failing,
            "risk": "high" if risky else "low",
            "score": round(100 * (len(features) - len(failing)) / (len(features) or 1)),
        }


class GenerateRemediation(DemoJob):
    """Render the config lines that bring the device back to the golden config."""

    device = ObjectVar(model=Device)
    failing = JSONVar()
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "Demo compliance: generate remediation"
        description = "Render the config lines that bring the device back to the golden config."

    def run(self, device, failing, **common):  # pylint: disable=arguments-differ
        """Return {remediation, line_count}."""
        self.work(f"Render the remediation for {device}", **common)
        lines = [line for feature in failing for line in FEATURES[feature][0]]
        return {"remediation": lines, "line_count": len(lines)}


class BackupConfig(DemoJob):
    """Save the device's running config as a restore point."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo compliance: back up one config"
        description = "Save the device's running config as a restore point."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {backup_id}."""
        self.work(f"Save {device}'s running config", **common)
        return {"backup_id": f"{device.name}-{stable(device.name, common.get('ticket')).getrandbits(32):08x}"}


class ApplyRemediation(DemoJob):
    """Push the remediation lines and save the config. Its rollback restores the backup."""

    device = ObjectVar(model=Device)
    remediation = JSONVar()
    backup_id = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(15, 28)
    failure = "remediation-fails"
    failure_message = (
        "the device rejected 'aaa authentication login default group ISE-TACACS local': % Invalid server group"
    )

    class Meta:
        """Meta."""

        name = "Demo compliance: apply remediation"
        description = "Push the remediation lines and save the config. Changes the network."

    def run(self, device, remediation, backup_id, **common):  # pylint: disable=arguments-differ
        """Return {applied_lines}."""
        self.work(f"Push {len(remediation)} lines to {device} (restore point {backup_id})", **common)
        store(f"remediated:{device.pk}:{common.get('ticket')}", True)
        return {"applied_lines": len(remediation)}


class RestoreConfig(DemoJob):
    """Restore the device's backup: the rollback when a remediation fails."""

    device = ObjectVar(model=Device)
    backup_id = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 15)

    class Meta:
        """Meta."""

        name = "Demo compliance: restore config"
        description = "Restore the device's backup: the rollback of a failed remediation."

    def run(self, device, backup_id, **common):  # pylint: disable=arguments-differ
        """Return {restored}."""
        self.work(f"Restore {backup_id} on {device}", **common)
        return {"restored": backup_id}


class PublishReport(DemoJob):
    """Publish the compliance report, before and after, where the auditors read it."""

    dynamic_group = ObjectVar(model=DynamicGroup)
    score_before = IntegerVar(required=False)
    score_after = IntegerVar(required=False)
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "Demo compliance: publish report"
        description = "Publish the compliance report, before and after, for the auditors."

    def run(self, dynamic_group, score_before, score_after, **common):  # pylint: disable=arguments-differ
        """Return {report_url, improvement}."""
        self.work(f"Publish {dynamic_group}'s compliance report", **common)
        ticket = common.get("ticket") or "report"
        return {
            "report_url": f"https://reports.example.com/compliance/{ticket}",
            "improvement": f"{score_before}% → {score_after}%",
        }


JOBS = (
    BackupConfigs,
    RunCompliance,
    CheckDevice,
    GenerateRemediation,
    BackupConfig,
    ApplyRemediation,
    RestoreConfig,
    PublishReport,
)

register_jobs(*JOBS)
