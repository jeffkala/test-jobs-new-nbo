"""Jobs for the NBO 3 example workflows. Nautobot imports this package when it syncs the repository."""

# Importing each module registers its jobs.
from . import compliance, demo, firewall, itsm, mock, onboarding, ports, seed, software  # noqa: F401
