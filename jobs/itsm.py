"""The change-management side of the demos: raise a change, hold for its window, close it, note the ticket.

They answer as ServiceNow would. Raising a change opens something outside NBO, so NBO never reruns it: a person who
checked the ITSM marks it done.
"""

from datetime import timedelta

from django.utils import timezone
from nautobot.apps.jobs import JSONVar, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Location
from nautobot.extras.models import DynamicGroup

from .demo import DemoJob, stable, times_asked, timing

name = "NBO demo: change management"  # pylint: disable=invalid-name


class RaiseChange(DemoJob):
    """Raise a normal change in the ITSM, with its risk and its implementation window."""

    risk = StringVar(required=False, default="moderate")
    application = StringVar(required=False, default="")
    target_version = StringVar(required=False, default="")
    dynamic_group = ObjectVar(model=DynamicGroup, required=False)
    location = ObjectVar(model=Location, required=False)
    failure = "change-not-raised"
    failure_message = "ServiceNow answered 503 after the change was submitted: check whether it was created"

    class Meta:
        """Meta."""

        name = "Demo ITSM: raise change"
        description = "Raise a normal change with its risk and implementation window. Opens a record outside NBO."

    def run(self, risk, application, target_version, dynamic_group, location, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {change_number, state, window_start, window_end, short_description, url}."""
        about = application or (
            f"upgrade {dynamic_group} to {target_version}"
            if target_version
            else f"connect {location} to the corporate network"
            if location
            else "a network change"
        )
        self.work(f"Raise a change for {about} (risk {risk})", **common)
        number = f"CHG{stable(common.get('ticket'), about).randint(1000000, 9999999)}"
        start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return {
            "change_number": number,
            "state": "Assess",
            "short_description": f"{common.get('ticket') or 'Request'}: {about}",
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(hours=4)).isoformat(),
            "url": f"https://itsm.example.com/change_request.do?number={number}",
        }


class InsideWindow(DemoJob):
    """A wait's check: done once the change's implementation window is open."""

    change_number = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(2, 6)

    class Meta:
        """Meta."""

        name = "Demo ITSM: inside the change window (check)"
        description = "A wait's check: done once the change's implementation window is open."

    def run(self, change_number, **common):  # pylint: disable=arguments-differ
        """Answer done: in the demo, every window is open when the device's turn comes."""
        self.work(f"Read {change_number or 'the change'}'s window", **common)
        return self.check("Change window", "done", f"{change_number or 'The change'} is inside its window")


class CloseChange(DemoJob):
    """Close the change as successful, with the request's outcome as the close notes."""

    change_number = StringVar()
    details = JSONVar(required=False)
    min_seconds, max_seconds = timing(4, 10)

    class Meta:
        """Meta."""

        name = "Demo ITSM: close change"
        description = "Close the change as successful. Closing twice changes nothing."

    def run(self, change_number, details, **common):  # pylint: disable=arguments-differ
        """Return {closed, close_code}."""
        self.work(f"Close {change_number}", **common)
        self.logger.info("Close notes: %s", details or "implemented as planned")
        return {"closed": True, "close_code": "Successful"}


class UpdateTicket(DemoJob):
    """Add a work note to the request's ticket and resolve it."""

    note = StringVar(required=False, default="")
    min_seconds, max_seconds = timing(3, 8)

    class Meta:
        """Meta."""

        name = "Demo ITSM: update ticket"
        description = "Add a work note to the request's ticket and resolve it. Opens a record outside NBO."

    def run(self, note, **common):  # pylint: disable=arguments-differ
        """Return {work_note, ticket_state}."""
        ticket = common.get("ticket") or "the ticket"
        self.work(f"Add a work note to {ticket} and resolve it", **common)
        return {"work_note": f"{ticket}: {note or 'done'}", "ticket_state": "Resolved"}


class RequestDnsRecords(DemoJob):
    """Ask the DNS team, by ServiceNow ticket, for the office's records: before ZTP starts (10-21 00:14)."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "Demo ITSM: request DNS records"
        description = "Raise a ServiceNow request to the DNS team for the office's records. Opens a record outside NBO."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {dns_ticket}."""
        self.work(f"Raise a DNS request for {location}", **common)
        return {"dns_ticket": f"RITM{stable('dns', location.name).randint(1000000, 9999999)}"}


class RequestCloudOnboarding(DemoJob):
    """Ask the cloud team, by JIRA, for the office's cloud prerequisites (10-21 00:15)."""

    location = ObjectVar(model=Location)
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "Demo ITSM: request cloud onboarding"
        description = "Open the cloud team's JIRA request for the office. Opens a record outside NBO."

    def run(self, location, **common):  # pylint: disable=arguments-differ
        """Return {jira_key}."""
        self.work(f"Open the cloud team's JIRA request for {location}", **common)
        return {"jira_key": f"CLOUD-{stable('jira', location.name).randint(1000, 9999)}"}


class TicketsResolved(DemoJob):
    """A wait's check: done when the other teams have resolved the pre-site tickets."""

    dns_ticket = StringVar()
    jira_key = StringVar()
    min_seconds, max_seconds = timing(2, 5)

    class Meta:
        """Meta."""

        name = "Demo ITSM: tickets resolved (check)"
        description = "A wait's check: done when the DNS ticket and the JIRA request are resolved."

    def run(self, dns_ticket, jira_key, **common):  # pylint: disable=arguments-differ
        """The DNS team answers first: not yet the first time, done after."""
        self.work(f"Read {dns_ticket} and {jira_key}", **common)
        if times_asked(f"tickets:{dns_ticket}:{jira_key}:{common.get('ticket')}") == 1:
            return self.check("Pre-site tickets", "not-yet", f"{jira_key} is In Progress with the cloud team")
        return self.check("Pre-site tickets", "done", f"{dns_ticket} and {jira_key} are resolved")


JOBS = (
    RaiseChange,
    InsideWindow,
    CloseChange,
    UpdateTicket,
    RequestDnsRecords,
    RequestCloudOnboarding,
    TicketsResolved,
)

register_jobs(*JOBS)
