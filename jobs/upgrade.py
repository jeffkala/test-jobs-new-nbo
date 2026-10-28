"""Mock jobs for the `os-upgrade-citrus` workflow: one job a step, each logging what the real job would do.

Nothing here touches a device, a file server or Athena. Every job takes the variables its workflow step passes
under `with`, plus the device the run is for, logs a line and returns the fields a later step reads. Snapshots are
small fixed dicts, so Compare, Interface remediation and ARP remediation have something to diff.
"""

from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, JSONVar, ObjectVar, StringVar, TextVar, register_jobs
from nautobot.dcim.models import Device

name = "NBO mock: OS upgrade (Citrus)"  # pylint: disable=invalid-name

# What a snapshot of a device looks like; the post-upgrade one differs just enough to remediate.
PRE_SNAPSHOT = {
    "version": "17.9.4a",
    "interfaces": {"Ethernet1": "connected", "Ethernet2": "connected", "Ethernet3": "notconnect"},
    "arp": {"10.0.0.1": "aabb.cc00.0001", "10.0.0.2": "aabb.cc00.0002", "10.0.0.3": "aabb.cc00.0003"},
    "users": ["netops", "svc-monitor"],
}
POST_SNAPSHOT = {
    "version": "17.12.4",
    "interfaces": {"Ethernet1": "connected", "Ethernet2": "notconnect", "Ethernet3": "notconnect"},
    "arp": {"10.0.0.1": "aabb.cc00.0001", "10.0.0.3": "aabb.cc00.0003"},
    "users": ["netops"],
}


class MockJob(Job):
    """A mock job: logs what the real job would do to the device, then answers as if it had."""

    device = ObjectVar(model=Device)

    class Meta:
        """Meta."""

        abstract = True
        has_sensitive_variables = False

    def mock(self, what):
        """Log `what` the real job would do."""
        self.logger.info("(mock) %s.", what)


class UpgradeSetup(MockJob):
    """Prepare the run: the real job collected the workflow data the inputs now carry."""

    class Meta:
        """Meta."""

        name = "Mock upgrade: upgrade setup"
        description = "Prepare the upgrade run for the device."

    def run(self, device):  # pylint: disable=arguments-differ
        """Return {ready}."""
        self.mock(f"Set up the upgrade run for {device}")
        return {"ready": True}


class AthenaCheckIn(MockJob):
    """Check in with Athena before the next stage of the upgrade."""

    mock_call = BooleanVar(default=True, description="Answer without calling Athena")

    class Meta:
        """Meta."""

        name = "Mock upgrade: Athena check-in"
        description = "Check in with Athena before the next stage of the upgrade."

    def run(self, device, mock_call):  # pylint: disable=arguments-differ
        """Return {checked_in, mock_call}."""
        self.mock(f"Check {device} in with Athena (mock_call={mock_call})")
        return {"checked_in": True, "mock_call": bool(mock_call)}


class CheckRunningOsVersion(MockJob):
    """Compare the version the device runs with the image's: different before the upgrade, the same after."""

    software_image_file = StringVar(description="The software image file to compare against")
    pre_upgrade = BooleanVar(default=True, description="Before the upgrade the versions must differ")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: check running OS version"
        description = "Compare the version the device runs with the image's version."

    def run(self, device, software_image_file, pre_upgrade, debug):  # pylint: disable=arguments-differ
        """Return {running_version, target_version, matches}."""
        running = PRE_SNAPSHOT["version"] if pre_upgrade else POST_SNAPSHOT["version"]
        self.mock(f"show version on {device}: {running}, image {software_image_file}, pre_upgrade={pre_upgrade}")
        if debug:
            self.logger.debug("pre_upgrade=%s software_image_file=%s", pre_upgrade, software_image_file)
        return {"running_version": running, "target_version": POST_SNAPSHOT["version"], "matches": not pre_upgrade}


class StoreSnapshotOutput(MockJob):
    """Run a validation rule and keep its raw output as a snapshot."""

    validation_rule = StringVar(description="The validation rule to run, by name")
    fail_job_on_task_failure = BooleanVar(default=True)

    class Meta:
        """Meta."""

        name = "Mock upgrade: store snapshot output"
        description = "Run a validation rule and keep its raw output as a snapshot."

    def run(self, device, validation_rule, fail_job_on_task_failure):  # pylint: disable=arguments-differ
        """Return {snapshot}."""
        self.mock(f"Run '{validation_rule}' on {device} and keep its output")
        return {
            "snapshot": {"users": PRE_SNAPSHOT["users"]},
            "fail_job_on_task_failure": bool(fail_job_on_task_failure),
        }


class DetectAdminUsers(MockJob):
    """Look for a logged-in admin in the users snapshot."""

    regex_pattern = StringVar(description="Regex that matches admin usernames")
    snapshot = JSONVar(description="The users snapshot to search")

    class Meta:
        """Meta."""

        name = "Mock upgrade: detect admin users"
        description = "Look for a logged-in admin in the users snapshot."

    def run(self, device, regex_pattern, snapshot):  # pylint: disable=arguments-differ
        """Return {admin_users, blocked}."""
        users = (snapshot or {}).get("users", [])
        self.mock(f"Match {regex_pattern!r} against {users} on {device}")
        return {"admin_users": [], "blocked": False}


class VerifyPeerDevices(MockJob):
    """Check the device's peers are healthy enough to carry its traffic while it reloads."""

    admin_username_regex = StringVar()
    dispatch_pattern = JSONVar(description="Per-platform commands")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: verify peer devices"
        description = "Check the device's peers are healthy enough to carry its traffic while it reloads."

    def run(self, device, admin_username_regex, dispatch_pattern, debug):  # pylint: disable=arguments-differ
        """Return {peers, healthy}."""
        platforms = sorted(dispatch_pattern or {})
        self.mock(f"Check the peers of {device} with the {platforms} commands, admins {admin_username_regex!r}")
        if debug:
            self.logger.debug("dispatch_pattern=%s", dispatch_pattern)
        return {"peers": [f"{device.name}-peer"], "healthy": True}


class CustomFileTransfer(MockJob):
    """Copy the image to the device's flash from the SCP server."""

    software_image_file = StringVar()
    scp_server = StringVar()
    download_url = StringVar()
    vrf = StringVar()
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: custom file transfer"
        description = "Copy the image to the device's flash from the SCP server."

    def run(self, device, software_image_file, scp_server, download_url, vrf, debug):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {copied, transfer_mbps}."""
        self.mock(f"Copy {software_image_file} from {scp_server} ({download_url}) to {device} flash: via vrf {vrf}")
        if debug:
            self.logger.debug("download_url=%s vrf=%s", download_url, vrf)
        return {"copied": software_image_file, "transfer_mbps": 240}


class TakeSnapshot(MockJob):
    """Run validation rule groups, or one rule, and keep the result as a snapshot."""

    validation_rule_groups = JSONVar(required=False, description="The validation rule groups to run, by name")
    validation_rule = StringVar(required=False, default="", description="One validation rule to run, by name")
    post_upgrade = BooleanVar(default=False, description="Taken after the upgrade: the snapshot differs from before")
    fail_job_on_task_failure = BooleanVar(default=True)

    class Meta:
        """Meta."""

        name = "Mock upgrade: take snapshot"
        description = "Run validation rule groups, or one rule, and keep the result as a snapshot."

    def run(self, device, validation_rule_groups, validation_rule, post_upgrade, fail_job_on_task_failure):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {snapshot}: the post-upgrade snapshot differs from the pre-upgrade one."""
        what = validation_rule_groups or validation_rule
        self.mock(f"Run {what} on {device}, post_upgrade={post_upgrade}")
        return {
            "snapshot": POST_SNAPSHOT if post_upgrade else PRE_SNAPSHOT,
            "fail_job_on_task_failure": bool(fail_job_on_task_failure),
        }


class CustomFileUpload(MockJob):
    """Copy the running config from the device to the SCP server."""

    scp_server = StringVar()
    file_name = StringVar()
    vrf = StringVar()
    dispatch_pattern = JSONVar(description="Per-platform copy command and prompt answers")

    class Meta:
        """Meta."""

        name = "Mock upgrade: custom file upload"
        description = "Copy the running config from the device to the SCP server."

    def run(self, device, scp_server, file_name, vrf, dispatch_pattern):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {uploaded, upload_url}."""
        platforms = sorted(dispatch_pattern or {})
        self.mock(f"Copy running-config of {device} to {scp_server} as {file_name} via vrf {vrf} ({platforms})")
        return {"uploaded": file_name, "upload_url": f"scp://{scp_server}/{file_name}"}


class CustomConfigWrapper(MockJob):
    """Push config lines to the device."""

    config = TextVar(description="The config lines to push")

    class Meta:
        """Meta."""

        name = "Mock upgrade: custom config wrapper"
        description = "Push config lines to the device."

    def run(self, device, config):  # pylint: disable=arguments-differ
        """Return {applied_lines}."""
        lines = [line for line in str(config or "").splitlines() if line.strip()]
        self.mock(f"Push {len(lines)} config lines to {device}")
        return {"applied_lines": len(lines)}


class CustomCommandWrapper(MockJob):
    """Run an exec command on the device."""

    command = StringVar()
    dispatch_pattern = JSONVar(description="Per-platform method and prompt answers")

    class Meta:
        """Meta."""

        name = "Mock upgrade: custom command wrapper"
        description = "Run an exec command on the device."

    def run(self, device, command, dispatch_pattern):  # pylint: disable=arguments-differ
        """Return {command, output}."""
        platforms = sorted(dispatch_pattern or {})
        self.mock(f"Run '{command}' on {device} ({platforms})")
        return {"command": command, "output": ""}


class MaintenanceMode(MockJob):
    """Put the device into, or take it out of, maintenance mode."""

    unit = StringVar(default="System")
    enable = BooleanVar(default=True, description="Enter maintenance mode; off leaves it")
    transition_timer = IntegerVar(default=300, min_value=0, description="Seconds to let traffic drain")

    class Meta:
        """Meta."""

        name = "Mock upgrade: maintenance mode"
        description = "Put the device into, or take it out of, maintenance mode."

    def run(self, device, unit, enable, transition_timer):  # pylint: disable=arguments-differ
        """Return {unit, maintenance}."""
        verb = "Enter" if enable else "Exit"
        self.mock(f"{verb} maintenance mode for unit {unit} on {device}, transition timer {transition_timer}s")
        return {"unit": unit, "maintenance": bool(enable)}


class DisableLeafInterfaces(MockJob):
    """Shut the access ports that match the pattern, so hosts fail over before the reload."""

    regex_pattern = StringVar(description="Interface descriptions to leave alone")
    snmp_cli_pattern = StringVar(description="The SNMP trap line that marks a port to skip")
    up_status = StringVar(default="connected", description="The status a port must have to be shut")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: disable leaf interfaces"
        description = "Shut the access ports that match the pattern before the reload."

    def run(self, device, regex_pattern, snmp_cli_pattern, up_status, debug):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {disabled}."""
        disabled = [intf for intf, status in PRE_SNAPSHOT["interfaces"].items() if status == up_status]
        self.mock(
            f"Shut {disabled} on {device}: status {up_status}, skipping {regex_pattern!r} and {snmp_cli_pattern!r}"
        )
        if debug:
            self.logger.debug("regex_pattern=%s snmp_cli_pattern=%s", regex_pattern, snmp_cli_pattern)
        return {"disabled": disabled}


class SaveConfig(MockJob):
    """Save the running config."""

    save_filename = StringVar(default="startup-config")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: save config"
        description = "Save the running config."

    def run(self, device, save_filename, debug):  # pylint: disable=arguments-differ
        """Return {saved}."""
        self.mock(f"copy running-config {save_filename} on {device}")
        if debug:
            self.logger.debug("save_filename=%s", save_filename)
        return {"saved": save_filename}


class InstallOs(MockJob):
    """Install the image and reload the device."""

    software_image_file = StringVar()
    reboot = BooleanVar(default=True)
    extra_options = JSONVar(required=False, description="Platform-specific install options")
    convergence_timer = IntegerVar(default=60, min_value=0, description="Seconds to wait after the reload")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: install OS"
        description = "Install the image and reload the device."

    def run(self, device, software_image_file, reboot, extra_options, convergence_timer, debug):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {installed, rebooted}."""
        self.mock(f"Install {software_image_file} on {device}, reboot={reboot}, wait {convergence_timer}s")
        if debug:
            self.logger.debug("extra_options=%s", extra_options)
        return {"installed": POST_SNAPSHOT["version"], "rebooted": bool(reboot)}


class ReenableLeafInterfaces(MockJob):
    """Bring back the access ports the upgrade shut."""

    down_status = StringVar(default="disabled", description="The status a port must have to be re-enabled")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: re-enable leaf interfaces"
        description = "Bring back the access ports the upgrade shut."

    def run(self, device, down_status, debug):  # pylint: disable=arguments-differ
        """Return {enabled}."""
        enabled = [intf for intf, status in PRE_SNAPSHOT["interfaces"].items() if status == "connected"]
        self.mock(f"no shutdown {enabled} on {device}: ports with status {down_status}")
        if debug:
            self.logger.debug("down_status=%s", down_status)
        return {"enabled": enabled}


class CompareSnapshots(MockJob):
    """Compare the pre- and post-upgrade snapshots."""

    pre_snapshot = JSONVar()
    post_snapshot = JSONVar()
    fail_fast = BooleanVar(default=False, description="Fail on the first difference")

    class Meta:
        """Meta."""

        name = "Mock upgrade: compare snapshots"
        description = "Compare the pre- and post-upgrade snapshots."

    def run(self, device, pre_snapshot, post_snapshot, fail_fast):  # pylint: disable=arguments-differ
        """Return {differences, verdict}."""
        pre, post = pre_snapshot or {}, post_snapshot or {}
        differences = [
            {"check": key, "before": pre.get(key), "after": post.get(key)}
            for key in sorted(set(pre) | set(post))
            if pre.get(key) != post.get(key)
        ]
        self.mock(f"Compare {device} before and after: {len(differences)} differences, fail_fast={fail_fast}")
        return {"differences": differences, "verdict": f"{pre.get('version')} -> {post.get('version')}"}


class InterfaceRemediation(MockJob):
    """Bounce the interfaces that were up before the upgrade and are down after it."""

    validation_rule = StringVar(default="Get Interfaces")
    precheck = JSONVar(description="The pre-upgrade snapshot")
    postcheck = JSONVar(description="The post-upgrade snapshot")
    dispatch_pattern = JSONVar(description="Per-platform status and bounce commands")
    convergence_timer = IntegerVar(default=10, min_value=0)
    dryrun = BooleanVar(default=False)
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: interface remediation"
        description = "Bounce the interfaces that were up before the upgrade and are down after it."

    def run(self, device, validation_rule, precheck, postcheck, dispatch_pattern, convergence_timer, dryrun, debug):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {bounced, dryrun}."""
        before = (precheck or {}).get("interfaces", {})
        after = (postcheck or {}).get("interfaces", {})
        bounced = [intf for intf, status in before.items() if status == "connected" and after.get(intf) != "connected"]
        self.mock(f"Bounce {bounced} on {device} per '{validation_rule}', wait {convergence_timer}s, dryrun={dryrun}")
        if debug:
            self.logger.debug("dispatch_pattern=%s", dispatch_pattern)
        return {"bounced": bounced, "dryrun": bool(dryrun)}


class ArpRemediation(MockJob):
    """Ping the ARP entries that went missing over the upgrade, so they re-learn."""

    pre_snapshot = JSONVar(description="The pre-upgrade ARP snapshot")
    post_snapshot = JSONVar(description="The post-upgrade ARP snapshot")
    arp_vrf = StringVar()
    delta_threshold = IntegerVar(default=5, min_value=0, description="More missing entries than this fails the job")
    dispatch_pattern = JSONVar(description="Per-platform ping and ARP commands")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: ARP remediation"
        description = "Ping the ARP entries that went missing over the upgrade, so they re-learn."

    def run(self, device, pre_snapshot, post_snapshot, arp_vrf, delta_threshold, dispatch_pattern, debug):  # pylint: disable=arguments-differ,too-many-arguments,too-many-positional-arguments
        """Return {missing, pinged, within_threshold}."""
        before = (pre_snapshot or {}).get("arp", {})
        after = (post_snapshot or {}).get("arp", {})
        missing = sorted(set(before) - set(after))
        self.mock(f"Ping {missing} in vrf {arp_vrf} from {device}; {len(missing)} missing, threshold {delta_threshold}")
        if debug:
            self.logger.debug("dispatch_pattern=%s", dispatch_pattern)
        return {"missing": missing, "pinged": missing, "within_threshold": len(missing) <= delta_threshold}


class AssignNewSoftware(MockJob):
    """Record the device's new software version in Nautobot."""

    software_version = StringVar(description="The software version to assign")
    debug = BooleanVar(default=False)

    class Meta:
        """Meta."""

        name = "Mock upgrade: assign new software"
        description = "Record the device's new software version in Nautobot."

    def run(self, device, software_version, debug):  # pylint: disable=arguments-differ
        """Return {software_version}."""
        self.mock(f"Set the software version of {device} to {software_version}")
        if debug:
            self.logger.debug("software_version=%s", software_version)
        return {"software_version": software_version}


JOBS = (
    UpgradeSetup,
    AthenaCheckIn,
    CheckRunningOsVersion,
    StoreSnapshotOutput,
    DetectAdminUsers,
    VerifyPeerDevices,
    CustomFileTransfer,
    TakeSnapshot,
    CustomFileUpload,
    CustomConfigWrapper,
    CustomCommandWrapper,
    MaintenanceMode,
    DisableLeafInterfaces,
    SaveConfig,
    InstallOs,
    ReenableLeafInterfaces,
    CompareSnapshots,
    InterfaceRemediation,
    ArpRemediation,
    AssignNewSoftware,
)

register_jobs(*JOBS)
