"""Greenfield office ZTP, the local admin: generate the secret in the PAM, then set it on the device (10-21 00:21).

The PAM is BeyondTrust Password Safe (Z8). The secret never leaves the PAM: NBO sees its ID only.
"""

from nautobot.apps.jobs import ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Device

from .demo import DemoJob, stable, timing

name = "NBO demo: ZTP PAM"  # pylint: disable=invalid-name

PRODUCT = "BeyondTrust Password Safe"


class CreateLocalAdmin(DemoJob):
    """Create the device's local admin account in the PAM, with a generated password. Opens a record outside NBO."""

    device = ObjectVar(model=Device)
    min_seconds, max_seconds = timing(4, 10)
    failure = "pam-timeout"

    class Meta:
        """Meta."""

        name = "ZTP PAM: create local admin secret"
        description = "Create the device's local admin secret in the PAM. Opens a record outside NBO."

    def run(self, device, **common):  # pylint: disable=arguments-differ
        """Return {secret_id, account, vault}."""
        self.failure_message = (
            f"{PRODUCT} timed out after the secret was submitted: check whether {device}'s secret exists, then "
            "Override secret_id and Mark done"
        )
        self.work(
            f"Create {device}'s local admin in {PRODUCT}",
            **(common if device.role.name == "console-server" else {**common, "demo_failure": "none"}),
        )
        return {
            "secret_id": f"BT-{stable('secret', device.name).randint(100000, 999999)}",
            "account": "ztp-localadmin",
            "vault": f"{PRODUCT} / Network / {device.location.name}",
        }


class SetLocalAdmin(DemoJob):
    """Fetch the secret from the PAM at run time and set it on the device: the password rotates once for ZTP."""

    device = ObjectVar(model=Device)
    secret_id = StringVar()
    min_seconds, max_seconds = timing(4, 8)

    class Meta:
        """Meta."""

        name = "ZTP PAM: set local admin on device"
        description = "Set the local admin from the PAM secret on the device. Changes the network."

    def run(self, device, secret_id, **common):  # pylint: disable=arguments-differ
        """Return {rotated}."""
        self.work(f"Set ztp-localadmin on {device} from {secret_id}", **common)
        return {"rotated": True, "next_rotation": "90 days, by the PAM"}


JOBS = (CreateLocalAdmin, SetLocalAdmin)

register_jobs(*JOBS)
