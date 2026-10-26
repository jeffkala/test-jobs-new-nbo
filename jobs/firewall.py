"""Firewall policy change: validate the flows, find the firewalls on their path, score the risk, and push the rules.

Each flow is a mapping: `{src: 10.20.30.0/24, dst: 172.16.10.25, port: 443, proto: tcp, app: payments-api}`.
Addresses resolve to the demo's zones below; the firewalls are the Nautobot devices with the role `firewall` at
the zones' sites, as the seed job creates them.
"""

import ipaddress

from nautobot.apps.jobs import JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoFailure, DemoJob, recall, ref, stable, store, timing

name = "NBO demo: firewall policy"  # pylint: disable=invalid-name

# The demo network's security zones: (network, zone, the site whose firewalls guard it).
ZONES = (
    (ipaddress.ip_network("10.10.0.0/16"), "nyc-datacenter", "NYC-DC1"),
    (ipaddress.ip_network("172.16.0.0/16"), "nyc-dmz", "NYC-DC1"),
    (ipaddress.ip_network("10.20.0.0/16"), "lon-campus", "LON-HQ"),
    (ipaddress.ip_network("10.30.0.0/16"), "bhm-branch", "LON-HQ"),
)
INTERNET = ("internet", "NYC-DC1")
ANY = {"any", "0.0.0.0/0", "::/0", "*"}
# Services a security architect must look at, and why.
RISKY_PORTS = {
    23: ("critical", "Telnet sends credentials in clear text"),
    3389: ("high", "RDP is a common ransomware entry point"),
    22: ("high", "SSH gives an interactive shell on the destination"),
    445: ("high", "SMB across zones spreads malware"),
    1433: ("medium", "Direct database access bypasses the application tier"),
    161: ("low", "SNMP read access for monitoring"),
}
RISK_ORDER = ("low", "medium", "high", "critical")


def zone_of(address):
    """Return (zone, site) for an address or a network; anything outside the demo's networks is the internet."""
    network = ipaddress.ip_network(str(address), strict=False)
    for known, zone, site in ZONES:
        if network.version == known.version and network.subnet_of(known):
            return zone, site
    return INTERNET


def firewalls_at(sites):
    """Return the firewalls at `sites`, by name."""
    return list(Device.objects.filter(role__name="firewall", location__name__in=sites).order_by("name"))


class ValidateFlows(DemoJob):
    """Check every flow is complete, well formed and specific: no `any` destinations, real ports, known protocols."""

    flows = JSONVar(description="The flows to open, each {src, dst, port, proto, app}")
    min_seconds, max_seconds = timing(6, 14)

    class Meta:
        """Meta."""

        name = "Demo firewall: validate flows"
        description = "Check every flow is complete and specific, and normalise it."

    def run(self, flows, **common):  # pylint: disable=arguments-differ
        """Return {flows, flow_count, objects}; fail naming the first flow that isn't acceptable."""
        self.work(f"Validate {len(flows or [])} flows", **common)
        if not flows:
            raise DemoFailure("the request has no flows: give at least one {src, dst, port, proto}")
        clean, objects = [], []
        for number, flow in enumerate(flows, start=1):
            if not isinstance(flow, dict) or not {"src", "dst", "port"} <= set(flow):
                raise DemoFailure(f"flow {number} needs src, dst and port: {flow}")
            # Only the DMZ takes traffic from anywhere; everything else names its source and destination.
            from_anywhere = str(flow["src"]).lower() in ANY
            if str(flow["dst"]).lower() in ANY or (from_anywhere and zone_of(flow["dst"])[0] != "nyc-dmz"):
                raise DemoFailure(
                    f"flow {number} ({flow.get('app', 'unnamed')}) uses 'any' as an address: name the hosts or "
                    "networks. Use Retry with changes to give corrected flows"
                )
            try:
                src = ipaddress.ip_network("0.0.0.0/0" if from_anywhere else str(flow["src"]), strict=False)
                dst = ipaddress.ip_network(str(flow["dst"]), strict=False)
                port = int(flow["port"])
            except ValueError as error:
                raise DemoFailure(f"flow {number}: {error}") from error
            if not 0 < port < 65536:
                raise DemoFailure(f"flow {number}: port {port} isn't between 1 and 65535")
            proto = str(flow.get("proto", "tcp")).lower()
            if proto not in ("tcp", "udp"):
                raise DemoFailure(f"flow {number}: protocol {proto} isn't tcp or udp")
            app = flow.get("app") or f"flow-{number}"
            clean.append({"app": app, "src": str(src), "dst": str(dst), "port": port, "proto": proto})
            objects += [{"object": f"{app}-src", "value": str(src)}, {"object": f"{app}-dst", "value": str(dst)}]
        return {"flows": clean, "flow_count": len(clean), "objects": objects}


class PathAnalysis(DemoJob):
    """Find the zones each flow crosses and the firewalls that guard them."""

    flows = JSONVar()
    min_seconds, max_seconds = timing(10, 20)

    class Meta:
        """Meta."""

        name = "Demo firewall: path analysis"
        description = "Find the zones each flow crosses and the firewalls on its path."

    def run(self, flows, **common):  # pylint: disable=arguments-differ
        """Return {firewalls, path, firewall_count, already_permitted}."""
        self.work("Trace each flow through the topology", **common)
        path, sites = [], set()
        for flow in flows:
            (src_zone, src_site), (dst_zone, dst_site) = zone_of(flow["src"]), zone_of(flow["dst"])
            sites |= {src_site, dst_site}
            crossed = [fw.name for fw in firewalls_at({src_site, dst_site})]
            path.append(
                {
                    "app": flow["app"],
                    "from": src_zone,
                    "to": dst_zone,
                    "service": f"{flow['proto']}/{flow['port']}",
                    "firewalls": ", ".join(crossed) or "none",
                }
            )
        firewalls = firewalls_at(sites)
        if not firewalls:
            raise DemoFailure(
                f"no firewall guards {', '.join(sorted(sites))}: Nautobot has no device with the role 'firewall' "
                "there. Run the seed job"
            )
        return {
            "firewalls": [ref(fw) for fw in firewalls],
            "firewall_count": len(firewalls),
            "path": path,
            "already_permitted": 0,
        }


class AssessRisk(DemoJob):
    """Score the flows against the security standard: which services, which zones, and whether a person must review."""

    flows = JSONVar()
    environment = StringVar(required=False, default="production")
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "Demo firewall: assess risk"
        description = "Score the flows against the security standard."

    def run(self, flows, environment, **common):  # pylint: disable=arguments-differ
        """Return {risk, score, findings, needs_review}."""
        self.work(f"Score {len(flows)} flows against the {environment} standard", **common)
        risk, score, findings = "low", 10, []
        for flow in flows:
            level, why = RISKY_PORTS.get(flow["port"], ("low", "An application port"))
            if zone_of(flow["src"])[0] == "internet" and level != "low":
                level, why = "critical", f"{why}, from the internet"
            score += {"low": 5, "medium": 15, "high": 30, "critical": 50}[level]
            if level != "low":
                findings.append(
                    {"app": flow["app"], "service": f"{flow['proto']}/{flow['port']}", "risk": level, "why": why}
                )
            risk = max(risk, level, key=RISK_ORDER.index)
        if environment != "production" and risk == "high":
            risk = "medium"
        return {
            "risk": risk,
            "score": min(score, 100),
            "findings": findings,
            "needs_review": risk in ("high", "critical"),
        }


class CompilePolicy(DemoJob):
    """Turn the flows into vendor rules for each firewall on their path, named after the change."""

    flows = JSONVar()
    firewalls = JSONVar()
    change_number = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "Demo firewall: compile policy"
        description = "Turn the flows into security rules for each firewall on their path."

    def run(self, flows, firewalls, change_number, **common):  # pylint: disable=arguments-differ
        """Return {rules, rule_count}."""
        self.work(f"Compile rules for {len(firewalls)} firewalls", **common)
        rules = []
        for firewall in firewalls:
            site = Device.objects.get(pk=firewall["id"]).location.name
            for number, flow in enumerate(flows, start=1):
                if site not in (zone_of(flow["src"])[1], zone_of(flow["dst"])[1]):
                    continue
                rules.append(
                    {
                        "firewall": firewall["display"],
                        "rule": f"{change_number or 'CHG'}-{flow['app']}-{number}",
                        "from": zone_of(flow["src"])[0],
                        "to": zone_of(flow["dst"])[0],
                        "source": flow["src"],
                        "destination": flow["dst"],
                        "service": f"{flow['proto']}/{flow['port']}",
                        "action": "allow",
                        "log": "session-end",
                    }
                )
        return {"rules": rules, "rule_count": len(rules)}


class SnapshotPolicy(DemoJob):
    """Read the firewall's rulebase: rule count, last commit and config hash; it doubles as the backup."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "Demo firewall: snapshot policy"
        description = "Read the firewall's rulebase and save a restore point."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {policy, backup_id}."""
        self.work(f"Read {device}'s running policy and save a restore point", **common)
        rng = stable(device.name)
        pushed = recall_pushed(device, common.get("ticket"))
        rules = rng.randint(180, 900)
        policy = {
            "security_rules": rules + pushed,
            "nat_rules": rng.randint(20, 80),
            "address_objects": rng.randint(400, 2000) + 2 * pushed,
            "ha_state": "active" if rng.random() > 0.3 else "active-primary",
            "panos_version": "11.1.2",
            "last_commit": "this request" if pushed else f"admin-{rng.randint(1, 9)} {rng.randint(2, 30)} days ago",
        }
        return {"policy": policy, "backup_id": f"{device.name}-{rng.randint(100000, 999999)}"}


def recall_pushed(device, ticket):
    """Return how many rules this request pushed to `device`, or 0."""
    return recall(f"pushed:{device.pk}:{ticket}", 0)


class PushPolicy(DemoJob):
    """Push this firewall's rules as a candidate config, then commit. Its rollback restores the snapshot."""

    device = ObjectVar(model=Device)
    rules = JSONVar()
    change_number = StringVar(required=False, default="")
    backup_id = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(18, 30)
    failure = "push-fails"
    failure_message = (
        "commit failed: rule 'payments-api' references address object 'payments-api-dst', which another "
        "administrator's uncommitted session has locked. The candidate config was reverted"
    )

    class Meta:
        """Meta."""

        name = "Demo firewall: push policy"
        description = "Push this firewall's rules and commit. Changes the network."

    def run(self, device, rules, change_number, backup_id, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {commit_id, rules_added, pushed}."""
        mine = [rule for rule in rules or [] if rule.get("firewall") == device.name]
        self.work(
            f"Push {len(mine)} rules to {device} for {change_number or 'the change'} (restore point {backup_id})",
            **common,
        )
        remember_pushed(device, common.get("ticket"), len(mine))
        return {
            "commit_id": f"{stable(device.name, change_number).randint(1000, 9999)}",
            "rules_added": len(mine),
            "pushed": [{"rule": rule["rule"], "service": rule["service"], "to": rule["to"]} for rule in mine],
        }


def remember_pushed(device, ticket, count):
    """Record what was pushed to `device`, so the next snapshot shows it."""
    store(f"pushed:{device.pk}:{ticket}", count)


class RestorePolicy(DemoJob):
    """Restore the firewall to its snapshot: the rollback when a push fails."""

    device = ObjectVar(model=Device)
    backup_id = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 15)

    class Meta:
        """Meta."""

        name = "Demo firewall: restore policy"
        description = "Load the restore point and commit: the rollback of a failed push."

    def run(self, device, backup_id, **common):  # pylint: disable=arguments-differ
        """Restore; return {restored}."""
        self.work(f"Load restore point {backup_id or '(latest)'} on {device} and commit", **common)
        remember_pushed(device, common.get("ticket"), 0)
        return {"restored": backup_id or "latest"}


class TestFlows(DemoJob):
    """A wait's check: send test traffic for each rule and read the hit counters."""

    device = ObjectVar(model=Device)
    rules = JSONVar()
    min_seconds, max_seconds = timing(6, 12)
    failure = "flow-test-fails"
    failure_message = (
        "test traffic for payments-api was dropped by rule 'deny-dmz-inbound', which sits above the new rule"
    )

    class Meta:
        """Meta."""

        name = "Demo firewall: test flows (check)"
        description = "A wait's check: send test traffic for each rule and read the hit counters."

    def run(self, device, rules, **common):  # pylint: disable=arguments-differ
        """Answer done with the hit counts, or failed naming the dropped flow."""
        mine = [rule for rule in rules or [] if rule.get("firewall") == device.name]
        try:
            self.work(f"Send test traffic through {device} for {len(mine)} rules", **common)
        except DemoFailure as failure:
            return self.check("Test flows", "failed", str(failure))
        hits = [
            {"rule": rule["rule"], "hits": stable(rule["rule"]).randint(3, 40), "result": "allowed"} for rule in mine
        ]
        return self.check("Test flows", "done", f"{len(mine)} rules pass traffic on {device}", hits=hits)


class TraceFlows(DemoJob):
    """A wait's check: trace every flow end to end, from source to destination, through all its firewalls."""

    flows = JSONVar()
    min_seconds, max_seconds = timing(8, 16)

    class Meta:
        """Meta."""

        name = "Demo firewall: trace flows end to end (check)"
        description = "A wait's check: trace every flow from source to destination."

    def run(self, flows, **common):  # pylint: disable=arguments-differ
        """Answer done with a trace per flow."""
        self.work(f"Trace {len(flows)} flows end to end", **common)
        traces = [
            {
                "app": flow["app"],
                "service": f"{flow['proto']}/{flow['port']}",
                "result": "reached",
                "rtt_ms": stable(flow["app"]).randint(2, 38),
            }
            for flow in flows
        ]
        return self.check("Trace flows", "done", f"all {len(flows)} flows reach their destination", traces=traces)


class ScheduleRecertification(DemoJob):
    """Schedule the rules' review before they expire, so temporary access doesn't become permanent."""

    expires = StringVar(required=False, default="")
    change_number = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(3, 8)

    class Meta:
        """Meta."""

        name = "Demo firewall: schedule recertification"
        description = "Schedule the rules' review before they expire."

    def run(self, expires, change_number, **common):  # pylint: disable=arguments-differ
        """Return {review_task, expires}."""
        self.work(f"Schedule the review of {change_number}'s rules before {expires}", **common)
        return {"review_task": f"SCTASK{stable(change_number, expires).randint(1000000, 9999999)}", "expires": expires}


JOBS = (
    ValidateFlows,
    PathAnalysis,
    AssessRisk,
    CompilePolicy,
    SnapshotPolicy,
    PushPolicy,
    RestorePolicy,
    TestFlows,
    TraceFlows,
    ScheduleRecertification,
)

register_jobs(*JOBS)
