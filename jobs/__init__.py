"""Jobs for the NBO 3 example workflows. Nautobot imports this package when it syncs the repository."""

# Importing each module registers its jobs.
from . import (  # noqa: F401
    apc,
    backup,
    compliance,
    datadog,
    demo,
    firewall,
    itsm,
    meraki,
    mock,
    onboarding,
    opengear,
    pam,
    panorama,
    ports,
    seed,
    software,
    ztp,
)
