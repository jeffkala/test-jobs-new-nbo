"""Port activation: check an access port is free, make sure its VLAN exists, configure it, and see the endpoint.

The checks read the real interface: a port whose status is Active is in use, and the request fails on it. The VLAN
check reads Nautobot's VLANs; a VLAN Nautobot lacks is created on the switch and in Nautobot. The last step records
the port's mode, VLAN and description on the interface, and sets it Active.
"""

from django.contrib.contenttypes.models import ContentType
from nautobot.apps.jobs import BooleanVar, IntegerVar, JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Interface
from nautobot.extras.models import Status
from nautobot.ipam.models import VLAN

from .demo import DemoFailure, DemoJob, recall, set_status, stable, store, times_asked, timing

name = "NBO demo: port activation"  # pylint: disable=invalid-name

LIFECYCLE = ("Planned", "Reserved", "Active")
# What each service needs on the port: PoE class watts, and the port's template.
SERVICES = {
    "user-desk": (0, ["spanning-tree portfast", "authentication port-control auto", "dot1x pae authenticator"]),
    "voice-phone": (
        15,
        ["spanning-tree portfast", "authentication port-control auto", "mab", "trust device cisco-phone"],
    ),
    "wireless-ap": (30, ["spanning-tree portfast", "power inline port maximum 30000"]),
    "camera": (15, ["spanning-tree portfast", "mab", "storm-control broadcast level 1.00"]),
    "server": (0, ["spanning-tree portfast", "spanning-tree bpduguard enable", "no cdp enable"]),
}


def port_state(interface, ticket):
    """Return the port's state as a show command reads it: what this request configured, else its record."""
    configured = recall(f"port:{interface.pk}:{ticket}")
    if configured:
        return configured
    return {
        "status": interface.status.name if interface.status else "",
        "admin": "up" if interface.enabled else "down",
        "link": "down",
        "mode": interface.mode or "-",
        "access_vlan": interface.untagged_vlan.vid if interface.untagged_vlan else "-",
        "description": interface.description or "-",
        "poe_watts": 0,
    }


class SetLifecycle(DemoJob):
    """Move the port along its lifecycle: Planned, Reserved, Active."""

    interface = ObjectVar(model=Interface)
    stage = IntegerVar(min_value=0, max_value=len(LIFECYCLE) - 1, description="Index into the lifecycle")
    min_seconds, max_seconds = timing(2, 5)

    class Meta:
        """Meta."""

        name = "Demo ports: set lifecycle stage"
        description = "Set the interface's status to a stage of the port lifecycle."

    def run(self, interface, stage, **common):  # pylint: disable=arguments-differ
        """Return {stage}."""
        self.work(f"Mark {interface.device} {interface} {LIFECYCLE[stage]}", **common)
        return {"stage": set_status(interface, LIFECYCLE[stage])}


class CheckPort(DemoJob):
    """Check the port is free: not Active in Nautobot, no cable to a live endpoint, shut down on the switch."""

    interface = ObjectVar(model=Interface)
    min_seconds, max_seconds = timing(6, 12)

    class Meta:
        """Meta."""

        name = "Demo ports: check port free"
        description = "Check the port is free in Nautobot and on the switch."

    def run(self, interface, **common):  # pylint: disable=arguments-differ
        """Return {port_state, switch, patch_panel}; fail when the port is in use."""
        self.work(f"Read show interface {interface} on {interface.device}", **common)
        if interface.status and interface.status.name == "Active":
            raise DemoFailure(
                f"{interface.device} {interface} is in use: its status is Active ({interface.description or 'no description'}). "
                "Abandon this request and pick a free port"
            )
        rng = stable(interface.device.name, interface.name)
        return {
            "port_state": port_state(interface, common.get("ticket")),
            "switch": interface.device.name,
            "patch_panel": f"{interface.device.location.name}-PP{rng.randint(1, 4)}-{rng.randint(1, 48):02d}",
        }


class CheckVlan(DemoJob):
    """Check the VLAN exists, in Nautobot and on the switch."""

    interface = ObjectVar(model=Interface)
    vlan = IntegerVar(min_value=1, max_value=4094)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo ports: check VLAN"
        description = "Check the VLAN exists in Nautobot and on the switch."

    def run(self, interface, vlan, **common):  # pylint: disable=arguments-differ
        """Return {vlan_present, vlan_name}."""
        self.work(f"Look for VLAN {vlan} on {interface.device}", **common)
        found = VLAN.objects.filter(vid=vlan).first()
        return {"vlan_present": found is not None, "vlan_name": found.name if found else f"VLAN{vlan:04d}"}


class CheckPoe(DemoJob):
    """Check the switch has PoE budget left for the endpoint."""

    interface = ObjectVar(model=Interface)
    service = StringVar()
    min_seconds, max_seconds = timing(4, 9)

    class Meta:
        """Meta."""

        name = "Demo ports: check PoE budget"
        description = "Check the switch has PoE budget left for the endpoint."

    def run(self, interface, service, **common):  # pylint: disable=arguments-differ
        """Return {poe_needed_w, poe_available_w}."""
        self.work(f"Read show power inline on {interface.device}", **common)
        needed = SERVICES.get(service, (15,))[0] or 15
        available = stable(interface.device.name, "poe").randint(180, 640)
        if available < needed:
            raise DemoFailure(f"{interface.device} has {available} W of PoE left and a {service} needs {needed} W")
        return {"poe_needed_w": needed, "poe_available_w": available}


class CreateVlan(DemoJob):
    """Create the VLAN on the switch, and in Nautobot. Its rollback removes it again."""

    interface = ObjectVar(model=Interface)
    vlan = IntegerVar(min_value=1, max_value=4094)
    vlan_name = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(8, 15)

    class Meta:
        """Meta."""

        name = "Demo ports: create VLAN"
        description = "Create the VLAN on the switch and in Nautobot. Changes the network."

    def run(self, interface, vlan, vlan_name, **common):  # pylint: disable=arguments-differ
        """Return {created_vlan}."""
        self.work(f"vlan {vlan} / name {vlan_name} on {interface.device}", **common)
        active, _ = Status.objects.get_or_create(name="Active")
        active.content_types.add(ContentType.objects.get_for_model(VLAN))
        VLAN.objects.get_or_create(vid=vlan, defaults={"name": vlan_name or f"VLAN{vlan:04d}", "status": active})
        return {"created_vlan": vlan}


class RemoveVlan(DemoJob):
    """Remove the VLAN CreateVlan made: its rollback."""

    interface = ObjectVar(model=Interface)
    vlan = IntegerVar(min_value=1, max_value=4094)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo ports: remove VLAN"
        description = "Remove the VLAN from the switch and Nautobot: the rollback of a failed create."

    def run(self, interface, vlan, **common):  # pylint: disable=arguments-differ
        """Return {removed_vlan}."""
        self.work(f"no vlan {vlan} on {interface.device}", **common)
        VLAN.objects.filter(vid=vlan, interfaces_as_untagged__isnull=True).delete()
        return {"removed_vlan": vlan}


class ConfigurePort(DemoJob):
    """Render and push the port's config for its service. Its rollback shuts the port down."""

    interface = ObjectVar(model=Interface)
    service = StringVar()
    vlan = IntegerVar(min_value=1, max_value=4094)
    voice_vlan = IntegerVar(required=False, min_value=1, max_value=4094)
    poe = BooleanVar(default=True)
    port_description = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(15, 25)
    failure = "config-rejected"
    failure_message = (
        "the switch rejected 'authentication port-control auto': dot1x system-auth-control is off on this switch. "
        "It's been turned on; Retry pushes the port again"
    )

    class Meta:
        """Meta."""

        name = "Demo ports: configure port"
        description = "Render and push the port's config for its service. Changes the network."

    def run(self, interface, service, vlan, voice_vlan, poe, port_description, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {config, config_lines}."""
        description = port_description or f"{service} {common.get('ticket') or ''}".strip()
        config = [
            f"interface {interface.name}",
            f" description {description}",
            " switchport mode access",
            f" switchport access vlan {vlan}",
        ]
        if voice_vlan:
            config.append(f" switchport voice vlan {voice_vlan}")
        config += [f" {line}" for line in SERVICES.get(service, (0, []))[1]]
        if not poe:
            config.append(" power inline never")
        config.append(" no shutdown")
        self.work(f"Push {len(config)} lines to {interface.device} {interface}", **common)
        store(
            f"port:{interface.pk}:{common.get('ticket')}",
            {
                "status": interface.status.name if interface.status else "",
                "admin": "up",
                "link": "up",
                "mode": "access",
                "access_vlan": vlan,
                "description": description,
                "poe_watts": SERVICES.get(service, (0,))[0] if poe else 0,
            },
        )
        return {"config": config, "config_lines": len(config)}


class ShutdownPort(DemoJob):
    """Shut the port and default its config: the rollback of a failed configure."""

    interface = ObjectVar(model=Interface)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo ports: shut down port"
        description = "Shut the port and default its config: the rollback of a failed configure."

    def run(self, interface, **common):  # pylint: disable=arguments-differ
        """Return {shut}."""
        self.work(f"default interface {interface} / shutdown on {interface.device}", **common)
        store(f"port:{interface.pk}:{common.get('ticket')}", None)
        return {"shut": True}


class LinkUp(DemoJob):
    """A wait's check: not yet until the endpoint is patched and powers up, then done with what LLDP sees."""

    interface = ObjectVar(model=Interface)
    service = StringVar()
    min_seconds, max_seconds = timing(4, 9)
    failure = "link-down"
    failure_message = (
        "the link is err-disabled (bpduguard): the endpoint sent BPDUs. The cable has been moved; Retry the wait"
    )

    class Meta:
        """Meta."""

        name = "Demo ports: link up (check)"
        description = "A wait's check: done when the link is up and the endpoint is seen."

    def run(self, interface, service, **common):  # pylint: disable=arguments-differ
        """The first check finds the endpoint still booting; the next sees it."""
        try:
            self.work(f"Read show interface {interface} status and show lldp neighbors", **common)
        except DemoFailure as failure:
            return self.check("Link up", "failed", str(failure))
        if times_asked(f"link:{interface.pk}:{common.get('ticket')}") == 1:
            return self.check("Link up", "not-yet", f"{interface} is down: the {service} is still booting")
        rng = stable(interface.device.name, interface.name)
        mac = ":".join(f"{rng.randint(0, 255):02x}" for _ in range(6))
        return self.check(
            "Link up",
            "done",
            f"{interface} is up, 1000/full, {service} {mac}",
            speed="1000",
            duplex="full",
            endpoint_mac=mac,
        )


class VerifyPort(DemoJob):
    """Read the port back after the change: the state Compare sets beside the check before."""

    interface = ObjectVar(model=Interface)
    min_seconds, max_seconds = timing(5, 10)

    class Meta:
        """Meta."""

        name = "Demo ports: verify port"
        description = "Read the port back after the change."

    def run(self, interface, **common):  # pylint: disable=arguments-differ
        """Return {port_state}; fail unless the port is up."""
        self.work(f"Read show interface {interface} and show authentication sessions", **common)
        state = port_state(interface, common.get("ticket"))
        if state.get("link") != "up":
            raise DemoFailure(f"{interface} isn't up after the change: {state}")
        return {"port_state": state}


class RecordInterface(DemoJob):
    """Record the port in Nautobot: access mode, its VLAN, its description, enabled and Active."""

    interface = ObjectVar(model=Interface)
    vlan = IntegerVar(min_value=1, max_value=4094)
    config = JSONVar(required=False)
    min_seconds, max_seconds = timing(3, 6)

    class Meta:
        """Meta."""

        name = "Demo ports: record interface in Nautobot"
        description = "Record the port's mode, VLAN and description in Nautobot, and set it Active."

    def run(self, interface, vlan, config, **common):  # pylint: disable=arguments-differ
        """Return {recorded}."""
        self.work(f"Update {interface.device} {interface} in Nautobot", **common)
        description = next((line.split(" ", 2)[2] for line in config or [] if line.startswith(" description ")), "")
        interface.mode = "access"
        interface.untagged_vlan = VLAN.objects.filter(vid=vlan).first()
        interface.description = description[:200]
        interface.enabled = True
        set_status(interface, "Active")
        return {"recorded": f"{interface.device} {interface}: access VLAN {vlan}, Active"}


JOBS = (
    SetLifecycle,
    CheckPort,
    CheckVlan,
    CheckPoe,
    CreateVlan,
    RemoveVlan,
    ConfigurePort,
    ShutdownPort,
    LinkUp,
    VerifyPort,
    RecordInterface,
)

register_jobs(*JOBS)
