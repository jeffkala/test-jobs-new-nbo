"""Mock site build jobs for trying NBO.

Each job takes a few inputs, runs for a random 1 to 5 minutes, logging as it goes, and fails at random with a
reason that says the failure was provoked. None of them touches the network or Nautobot's data.

They don't use `NBOStepStatusMixin` and don't declare `nbo_step`: NBO finishes their steps from their JobResult,
as it does for any app's job.
"""

import random
import time

from nautobot.apps.jobs import IntegerVar, Job, StringVar

name = "NBO site build demo"  # pylint: disable=invalid-name


class ProvokedStop(RuntimeError):
    """The failure a mock job raises on purpose."""


class MockSiteJob(Job):
    """Sleep for a random time between `min_seconds` and `max_seconds`, then fail with `failure_percent` chance."""

    site_code = StringVar(label="Site code", required=False, description="Such as SJC04.")
    min_seconds = IntegerVar(label="Shortest run (s)", default=60, min_value=1, max_value=600)
    max_seconds = IntegerVar(label="Longest run (s)", default=300, min_value=1, max_value=600)
    failure_percent = IntegerVar(label="Failure chance (%)", default=25, min_value=0, max_value=100)

    #: What the job pretends to do, for its log lines and its failure reason.
    action = "work on the site"

    class Meta:
        """Shared Meta: room for a 5-minute run under Nautobot's default 300-second soft limit."""

        has_sensitive_variables = False
        soft_time_limit = 600
        time_limit = 660

    def run(self, *, site_code="", min_seconds=60, max_seconds=300, failure_percent=25, **kwargs):  # pylint: disable=arguments-differ
        """Pretend to do `action`, then succeed or stop on purpose."""
        low, high = sorted((int(min_seconds), int(max_seconds)))
        duration = random.randint(low, high)  # noqa: S311  # demo timing, not security
        fails = random.randint(1, 100) <= int(failure_percent)  # noqa: S311
        stop_at = random.randint(1, duration) if fails else duration  # noqa: S311
        site = site_code or "the site"
        self.logger.info("Starting to %s for %s; this run takes up to %ss.", self.action, site, duration)
        for detail, value in sorted(kwargs.items()):
            self.logger.info("Input %s = %r", detail, value)
        elapsed = 0
        while elapsed < stop_at:
            step = min(30, stop_at - elapsed)
            time.sleep(step)
            elapsed += step
            self.logger.info("Still working to %s for %s: %ss of %ss.", self.action, site, elapsed, duration)
        if fails:
            reason = (
                f"Provoked stop: {self.Meta.name} was stopped on purpose after {elapsed}s "
                f"(failure chance {failure_percent}%), to show how NBO handles a failed step."
            )
            self.logger.error(reason)
            raise ProvokedStop(reason)
        self.logger.info("Done: %s for %s in %ss.", self.action, site, elapsed)
        return {"site_code": site_code, "seconds": elapsed}


class AllocateSitePrefixes(MockSiteJob):
    """Pretend to allocate the site's management and user prefixes."""

    device_count = IntegerVar(label="Devices", default=4, min_value=1, max_value=48)
    action = "allocate prefixes"

    class Meta(MockSiteJob.Meta):
        """Meta."""

        name = "Site build demo - Allocate site prefixes"
        description = "Mock: pretends to allocate the site's prefixes. Runs 1-5 minutes and fails at random."


class RenderSiteConfigs(MockSiteJob):
    """Pretend to render the configuration of every device at the site."""

    site_name = StringVar(label="Site name", required=False)
    action = "render configurations"

    class Meta(MockSiteJob.Meta):
        """Meta."""

        name = "Site build demo - Render site configs"
        description = "Mock: pretends to render device configurations. Runs 1-5 minutes and fails at random."


class PushSiteConfigs(MockSiteJob):
    """Pretend to push the rendered configurations to the site's devices."""

    image = StringVar(label="Software image", default="17.12.4", required=False)
    action = "push configurations"

    class Meta(MockSiteJob.Meta):
        """Meta."""

        name = "Site build demo - Push site configs"
        description = "Mock: pretends to push configurations. Runs 1-5 minutes and fails at random."


class RollBackSiteConfigs(MockSiteJob):
    """Pretend to restore the configurations saved before the push."""

    action = "roll back configurations"

    class Meta(MockSiteJob.Meta):
        """Meta."""

        name = "Site build demo - Roll back site configs"
        description = "Mock: pretends to roll back a push. Runs 1-5 minutes and fails at random."
