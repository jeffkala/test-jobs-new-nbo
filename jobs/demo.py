"""What every NBO demo job shares: the time it takes, the failure a presenter asks for, and realistic data.

The demo jobs change nothing on a network. They read the Nautobot objects the seed job creates and answer as the
real system would, so every workflow runs end to end on a plain Nautobot. A few write to Nautobot, as the real job
would: a status, a software version, an interface's VLAN.

Every demo job takes these variables; a workflow binds the first two by name from its inputs:

- `ticket`: the request the work belongs to, such as RITM0012345.
- `demo_failure`: the failure the presenter picked on the start form. The job whose failure it names fails once
  for that ticket, then works when a person retries, so the recovery can be shown. Pick another ticket to see it
  again.
- `fail_percent`: an extra chance, from 0 to 100, that the job fails at random. 0 by default.
- `min_seconds`, `max_seconds`: how long the job works.
"""

import hashlib
import random
import time

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from nautobot.apps.jobs import IntegerVar, Job, JSONVar, StringVar, register_jobs
from nautobot.extras.models import Status

name = "NBO demo: shared jobs"  # pylint: disable=invalid-name

ONE_DAY = 24 * 60 * 60


class DemoFailure(Exception):
    """A failure the demo chose to have: the presenter's pick, or the job's random rate."""


def stable(*parts):
    """Return a random generator seeded by `parts`: the same device always gets the same uptime, peers and serial."""
    seed = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))  # noqa: S311 - demo data, not security


def remember(key, value=True):
    """Remember `value` for a day, where every worker sees it; return False when `key` was already there."""
    return cache.add(f"nbo-demo:{key}", value, timeout=ONE_DAY)


def recall(key, default=None):
    """Return what `remember` stored under `key`, or `default`."""
    return cache.get(f"nbo-demo:{key}", default)


def store(key, value):
    """Store `value` under `key` for a day, replacing what was there."""
    cache.set(f"nbo-demo:{key}", value, timeout=ONE_DAY)


def times_asked(key):
    """Count how often `key` was asked for, starting at 1: a check uses it to answer "not yet" the first time."""
    full = f"nbo-demo:asked:{key}"
    if cache.add(full, 1, timeout=ONE_DAY):
        return 1
    return cache.incr(full)


def ref(obj):
    """Return `obj` as the {model, id, display} reference a fan-out's items are."""
    return {"model": obj._meta.label_lower, "id": str(obj.pk), "display": str(obj)}


def timing(low, high):
    """Return a job's `min_seconds` and `max_seconds` variables with its own defaults."""
    return (
        IntegerVar(default=low, min_value=0, description="Shortest run, in seconds"),
        IntegerVar(default=high, min_value=0, description="Longest run, in seconds"),
    )


def set_status(obj, status_name):
    """Set `obj`'s status to `status_name`, creating that status for the object's type if Nautobot lacks it."""
    status, _ = Status.objects.get_or_create(name=status_name)
    status.content_types.add(ContentType.objects.get_for_model(type(obj)))
    obj.status = status
    obj.validated_save()
    return status_name


class DemoJob(Job):
    """A demo job: works for a while, then fails if the presenter asked for this job's failure."""

    ticket = StringVar(required=False, default="", description="The request this work belongs to")
    demo_failure = StringVar(required=False, default="none", description="Demo: the failure to show, once a ticket")
    fail_percent = IntegerVar(default=0, min_value=0, max_value=100, description="Extra chance of failing, in percent")
    min_seconds, max_seconds = timing(8, 20)

    # The `demo_failure` choice that makes this job fail, and what it says then.
    failure = ""
    failure_message = ""

    class Meta:
        """Meta."""

        abstract = True
        has_sensitive_variables = False

    def work(self, what, ticket="", demo_failure="none", fail_percent=0, min_seconds=8, max_seconds=20):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        """Work for `min_seconds` to `max_seconds`, then fail if this job's demo failure is due."""
        self.logger.info("%s.", what)
        low, high = sorted((max(0, min_seconds), max(0, max_seconds)))
        time.sleep(random.uniform(low, high))  # noqa: S311
        if self.failure_due(ticket, demo_failure):
            raise DemoFailure(self.failure_message)
        if random.uniform(0, 100) < fail_percent:  # noqa: S311
            raise DemoFailure(f"{what} failed (a random failure; this step fails {fail_percent}% of the time)")

    def failure_due(self, ticket, demo_failure):
        """Return True the first time this job runs for `ticket` with its own failure picked; never again."""
        if not self.failure or demo_failure != self.failure:
            return False
        return remember(f"failed:{self.failure}:{ticket}")

    def check(self, what, outcome, message, **found):
        """Answer a wait's check, logging the answer: done, not-yet or failed."""
        self.logger.info("%s: %s, %s.", what, outcome, message)
        return {"outcome": outcome, "message": message, **found}


class Notify(DemoJob):
    """Post the request's outcome to a chat channel."""

    channel = StringVar(default="#network-changes")
    run_id = StringVar(required=False, default="")
    requested_by = StringVar(required=False, default="")
    summary = StringVar(required=False, default="")
    details = JSONVar(required=False)
    min_seconds, max_seconds = timing(2, 5)

    class Meta:
        """Meta."""

        name = "Demo: notify the channel"
        description = "Post the request's outcome to a chat channel."

    def run(self, channel, run_id, requested_by, summary, details, **common):  # pylint: disable=arguments-differ,too-many-arguments
        """Return {posted, message}."""
        self.work(f"Post to {channel}", **common)
        if isinstance(details, dict):
            summary = " ".join(filter(None, (summary, ", ".join(f"{key}: {value}" for key, value in details.items()))))
        elif isinstance(details, list):
            summary = " ".join(filter(None, (summary, f"{len(details)} items.")))
        message = f"Request {run_id[:8] or '-'} for {requested_by or 'someone'} has finished. {summary}".strip()
        self.logger.info("Posted to %s: %s", channel, message)
        return {"posted": True, "message": message}


JOBS = (Notify,)

register_jobs(*JOBS)
