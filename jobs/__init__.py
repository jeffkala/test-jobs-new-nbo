"""Register the mock site build jobs, as Nautobot expects from a Git repository's `jobs` submodule."""

from nautobot.apps.jobs import register_jobs

from .site_build import AllocateSitePrefixes, PushSiteConfigs, RenderSiteConfigs, RollBackSiteConfigs

register_jobs(AllocateSitePrefixes, RenderSiteConfigs, PushSiteConfigs, RollBackSiteConfigs)
