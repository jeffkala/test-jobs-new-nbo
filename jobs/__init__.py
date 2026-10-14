"""Register the demo jobs, as Nautobot expects from a Git repository's `jobs` submodule."""

from nautobot.apps.jobs import register_jobs

from .os_upgrade import (
    CheckFreeSpace,
    DeviceBackAfterReboot,
    InstallOS,
    PostCheck,
    PreCheckInterfaces,
    PreCheckNeighbors,
    PreCheckProcessor,
    PreCheckRouting,
    PreCheckUptime,
    RebootDevice,
    UploadOS,
    ValidateUpgrade,
)
from .site_build import AllocateSitePrefixes, PushSiteConfigs, RenderSiteConfigs, RollBackSiteConfigs

register_jobs(AllocateSitePrefixes, RenderSiteConfigs, PushSiteConfigs, RollBackSiteConfigs)
register_jobs(
    PreCheckInterfaces,
    PreCheckRouting,
    PreCheckNeighbors,
    PreCheckProcessor,
    PreCheckUptime,
    CheckFreeSpace,
    UploadOS,
    InstallOS,
    RebootDevice,
    DeviceBackAfterReboot,
    PostCheck,
    ValidateUpgrade,
)
