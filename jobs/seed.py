"""Seed a plain Nautobot with everything the NBO demo workflows need, and reset it between demos.

Run "NBO demo: seed data" once after syncing this repository. It creates three sites, their switches and
firewalls with interfaces and management addresses, VLANs, software versions, the dynamic groups the campaigns run
for, the people's groups, a permission for them, and enables the demo jobs. For the ZTP demo it adds the
greenfield offices, still empty, with the office pool and the device types, platforms and roles ZTP creates them with. Running it again changes nothing,
unless "Reset the demo" is ticked: then every demo object goes back to how a demo starts.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from nautobot.apps.jobs import BooleanVar, Job, StringVar, register_jobs
from nautobot.dcim.models import (
    Cable,
    Device,
    DeviceType,
    Interface,
    Location,
    LocationType,
    Manufacturer,
    Platform,
    Rack,
    SoftwareVersion,
)
from nautobot.extras.choices import DynamicGroupTypeChoices
from nautobot.extras.models import DynamicGroup, Role, Status
from nautobot.extras.models import Job as JobModel
from nautobot.ipam.models import VLAN, IPAddress, Namespace, Prefix
from nautobot.users.models import ObjectPermission

from . import ztp

name = "NBO demo: seed data"  # pylint: disable=invalid-name

# Sites: name, what it is, its management /24.
SITES = (
    ("NYC-DC1", "Data centre, New York", "10.255.1.0/24"),
    ("LON-HQ", "Campus headquarters, London", "10.255.2.0/24"),
    ("BHM-BR01", "Branch office, Birmingham", "10.255.3.0/24"),
)
# Device types: manufacturer, model, platform, the version the seed installs.
TYPES = {
    "access": ("Cisco", "C9300-48P", "cisco_ios", "17.9.4a"),
    "core": ("Cisco", "C9500-24Y4C", "cisco_ios", "17.9.4a"),
    "firewall": ("Palo Alto Networks", "PA-3440", "paloalto_panos", "11.1.2"),
}
ROLES = {"access": "access-switch", "core": "core-switch", "firewall": "firewall"}
# Devices: name, site, kind, status, serial, whether it has a management address.
DEVICES = (
    ("nyc-dc1-core-01", "NYC-DC1", "core", "Active", "FDO2611A1KZ", True),
    ("fw-nyc-edge-01", "NYC-DC1", "firewall", "Active", "016201004117", True),
    ("fw-nyc-dc-01", "NYC-DC1", "firewall", "Active", "016201004128", True),
    ("lon-hq-core-01", "LON-HQ", "core", "Active", "FDO2611A2QX", True),
    ("lon-hq-acc-01", "LON-HQ", "access", "Active", "FOC2633Y0AB", True),
    ("lon-hq-acc-02", "LON-HQ", "access", "Active", "FOC2633Y0AC", True),
    ("lon-hq-acc-03", "LON-HQ", "access", "Active", "FOC2633Y0AD", True),
    ("lon-hq-acc-04", "LON-HQ", "access", "Active", "FOC2633Y0AE", True),
    ("fw-lon-edge-01", "LON-HQ", "firewall", "Active", "016201004133", True),
    ("bhm-br01-acc-01", "BHM-BR01", "access", "Planned", "FOC2701Z1QA", True),
    # No management address: onboarding's record check fails on it until a person gives it one.
    ("bhm-br01-acc-02", "BHM-BR01", "access", "Planned", "FOC2701Z1QB", False),
)
# One switch already runs the campaign's target version, so the plan shows a device it leaves alone.
ALREADY_CURRENT = {"lon-hq-acc-04": "17.12.4"}
ACCESS_PORTS = 24
PORTS_IN_USE = 12
PREFIXES = (
    ("10.10.0.0/16", "NYC-DC1 servers"),
    ("172.16.0.0/16", "NYC-DC1 DMZ"),
    ("10.20.0.0/16", "LON-HQ campus"),
    ("10.30.0.0/16", "BHM-BR01 branch"),
    ("10.255.0.0/16", "Management"),
)
VLANS = ((10, "users"), (20, "voice"), (30, "wireless-ap"), (40, "cameras"), (100, "servers"), (999, "management"))
GROUPS = {
    "LON-HQ access switches": {"location": ["LON-HQ"], "role": ["access-switch"]},
    "All access switches": {"role": ["access-switch"]},
    "Firewalls": {"role": ["firewall"]},
}
TEAMS = (
    "network-ops",
    "network-change",
    "field-ops",
    "security-architects",
    "change-advisory-board",
    "network-engineering",
    "site-operators",
)
# Two demo people, so one asks and the other approves: NBO doesn't let a person approve their own request.
PEOPLE = {
    "demo-requester": ("network-ops", "field-ops", "site-operators"),
    "demo-approver": (
        "network-change",
        "security-architects",
        "change-advisory-board",
        "network-ops",
        "network-engineering",
    ),
}
# What the teams may do: start and work requests, run the demo jobs, and read the inventory they act on.
PERMISSIONS = {
    "nautobot_orchestrator.nborun": ["view", "run", "complete", "approve", "override", "abandon"],
    "nautobot_orchestrator.nbodefinition": ["view"],
    "nautobot_orchestrator.nbosteprun": ["view"],
    "extras.job": ["view", "run"],
    "extras.jobresult": ["view"],
    "extras.dynamicgroup": ["view"],
    "dcim.device": ["view"],
    "dcim.interface": ["view"],
    "dcim.location": ["view"],
    "ipam.prefix": ["view"],
    "ipam.vlan": ["view"],
}
STATUSES = {
    "Active": (Device, Interface, Location, Prefix, IPAddress, VLAN, SoftwareVersion),
    "Planned": (Device, Interface, Location),
    "Staged": (Device,),
    "Validated": (Device,),
    "Reserved": (Interface,),
}


# The ZTP demo's greenfield offices: name, what it is. Each starts Planned and empty; ZTP creates its devices.
ZTP_SITES = (
    ("SJC-LAB1", "Sunnyvale ZTP lab: the MVP's lab site"),
    ("SJC-OFC2", "Sunnyvale office 2: the MVP's production greenfield site"),
)


class SeedDemoData(Job):
    """Create the demo inventory, teams and permissions; with Reset, put every demo object back as a demo starts."""

    reset = BooleanVar(default=False, label="Reset the demo", description="Put ports, statuses and versions back")
    add_me = BooleanVar(default=True, label="Add me to the teams", description="Put you in every demo team")
    password = StringVar(
        required=False,
        default="",
        label="Demo people's password",
        description="Give one to create demo-requester and demo-approver with it; leave empty to skip them",
    )
    enable_jobs = BooleanVar(default=True, label="Enable the demo jobs", description="Nautobot installs jobs disabled")

    class Meta:
        """Meta."""

        name = "NBO demo: seed data"
        description = "Create the NBO demo's sites, devices, VLANs, groups, teams and permissions. Safe to run again."
        has_sensitive_variables = True

    def run(self, reset, add_me, password, enable_jobs):  # pylint: disable=arguments-differ
        """Seed, then say what there is."""
        with transaction.atomic():
            self.statuses = self.seed_statuses()
            self.seed_inventory(reset)
            self.seed_ztp(reset)
            self.seed_groups()
            self.seed_teams(add_me, password)
            if enable_jobs:
                self.enable_repository_jobs()
        summary = {
            "locations": Location.objects.filter(name__in=[site for site, _, _ in SITES]).count(),
            "devices": Device.objects.filter(name__in=[device[0] for device in DEVICES]).count(),
            "interfaces": Interface.objects.filter(device__name__in=[device[0] for device in DEVICES]).count(),
            "dynamic_groups": {name: len(DynamicGroup.objects.get(name=name).members) for name in GROUPS},
        }
        self.logger.info("The demo is seeded: %s", summary)
        return summary

    # ------------------------------------------------------------------------------------------------- helpers
    def ensure(self, model, lookup, defaults=None, reset_fields=None):
        """Return the `model` object matching `lookup`, creating it with `defaults`; with `reset_fields`, set those."""
        obj = model.objects.filter(**lookup).first()
        if obj is None:
            obj = model(**lookup, **(defaults or {}), **(reset_fields or {}))
            obj.validated_save()
            self.logger.info("Created %s %s.", model._meta.verbose_name, obj)
        elif reset_fields and any(getattr(obj, field) != value for field, value in reset_fields.items()):
            for field, value in reset_fields.items():
                setattr(obj, field, value)
            obj.validated_save()
            self.logger.info("Reset %s %s.", model._meta.verbose_name, obj)
        return obj

    def seed_statuses(self):
        """Return {name: Status}, each usable on the models the demo puts it on."""
        statuses = {}
        for status_name, models in STATUSES.items():
            statuses[status_name], _ = Status.objects.get_or_create(name=status_name)
            statuses[status_name].content_types.add(*ContentType.objects.get_for_models(*models).values())
        return statuses

    # ----------------------------------------------------------------------------------------------- inventory
    def seed_inventory(self, reset):  # pylint: disable=too-many-locals
        """Create the sites, devices, ports, addresses, VLANs and software versions."""
        active = self.statuses["Active"]
        site_type = self.ensure(LocationType, {"name": "Site"})
        site_type.content_types.add(*ContentType.objects.get_for_models(Device, Prefix, VLAN).values())
        sites = {
            site: self.ensure(
                Location, {"name": site}, {"location_type": site_type, "status": active, "description": about}
            )
            for site, about, _ in SITES
        }
        roles = {}
        for kind, role_name in ROLES.items():
            roles[kind] = self.ensure(Role, {"name": role_name})
            roles[kind].content_types.add(ContentType.objects.get_for_model(Device))
        types, versions = {}, {}
        for kind, (maker, model, platform_name, version) in TYPES.items():
            manufacturer = self.ensure(Manufacturer, {"name": maker})
            types[kind] = self.ensure(DeviceType, {"manufacturer": manufacturer, "model": model})
            platform = self.ensure(Platform, {"name": platform_name}, {"manufacturer": manufacturer})
            if not platform.network_driver:
                platform.network_driver = platform_name
                platform.validated_save()
            versions[kind] = self.ensure(
                SoftwareVersion, {"platform": platform, "version": version}, {"status": active}
            )
        cisco_ios = Platform.objects.get(name="cisco_ios")
        for version in ("17.12.4", *ALREADY_CURRENT.values()):
            versions[version] = self.ensure(
                SoftwareVersion, {"platform": cisco_ios, "version": version}, {"status": active}
            )
        namespace, _ = Namespace.objects.get_or_create(name="Global")
        for network, about in PREFIXES:
            self.ensure(
                Prefix,
                {"prefix": network, "namespace": namespace},
                {"status": active, "type": "container", "description": about},
            )
        for site, _, network in SITES:
            self.ensure(
                Prefix,
                {"prefix": network, "namespace": namespace},
                {"status": active, "type": "network", "description": f"{site} management"},
            )
        for vid, vlan_name in VLANS:
            self.ensure(VLAN, {"vid": vid, "name": vlan_name}, {"status": active})
        for number, (device_name, site, kind, status_name, serial, addressed) in enumerate(DEVICES, start=11):
            version = versions[ALREADY_CURRENT[device_name]] if device_name in ALREADY_CURRENT else versions[kind]
            state = {"status": self.statuses[status_name], "software_version": version} if reset else None
            device = self.ensure(
                Device,
                {"name": device_name},
                {
                    "location": sites[site],
                    "role": roles[kind],
                    "device_type": types[kind],
                    "platform": versions[kind].platform,
                    "serial": serial,
                    **({} if reset else {"status": self.statuses[status_name], "software_version": version}),
                },
                state,
            )
            self.seed_interfaces(device, kind, reset)
            self.seed_address(
                device, kind, number, dict((s, n) for s, _, n in SITES)[site], namespace, addressed, reset
            )

    def seed_interfaces(self, device, kind, reset):
        """Give `device` its management port and, for an access switch, 24 access ports: 12 in use, 12 free."""
        ports = [("GigabitEthernet0/0" if kind != "firewall" else "management", True, "Active", "management")]
        if kind == "access":
            for port in range(1, ACCESS_PORTS + 1):
                in_use = port <= PORTS_IN_USE
                ports.append(
                    (
                        f"GigabitEthernet1/0/{port}",
                        False,
                        "Active" if in_use else "Planned",
                        f"desk-{port:02d}" if in_use else "",
                    )
                )
        elif kind == "firewall":
            ports += [
                (f"ethernet1/{port}", False, "Active", f"zone {zone}")
                for port, zone in enumerate(("untrust", "trust", "dmz"), start=1)
            ]
        else:
            ports += [(f"TwentyFiveGigE1/0/{port}", False, "Active", "uplink") for port in (1, 2)]
        users = VLAN.objects.filter(vid=10).first()
        for port_name, mgmt_only, status_name, description in ports:
            in_use = status_name == "Active" and not mgmt_only and kind == "access"
            state = {
                "status": self.statuses[status_name],
                "description": description,
                "enabled": status_name == "Active",
                "mode": "access" if in_use else "",
                "untagged_vlan": users if in_use else None,
            }
            self.ensure(
                Interface,
                {"device": device, "name": port_name},
                {"type": "1000base-t", "mgmt_only": mgmt_only, **({} if reset else state)},
                state if reset else None,
            )

    def seed_address(self, device, kind, number, network, namespace, addressed, reset):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        """Give `device` its management address as its primary IPv4, unless the demo needs it without one."""
        mgmt = device.interfaces.get(name="GigabitEthernet0/0" if kind != "firewall" else "management")
        address = f"{network.rsplit('.', 1)[0]}.{number}/24"
        if not addressed:
            if reset and device.primary_ip4 is not None:
                device.primary_ip4 = None
                device.validated_save()
                IPAddress.objects.filter(host=address.split("/")[0], parent__namespace=namespace).delete()
            return
        ip = IPAddress.objects.filter(host=address.split("/")[0], parent__namespace=namespace).first()
        if ip is None:
            ip = IPAddress(
                address=address,
                namespace=namespace,
                status=self.statuses["Active"],
                dns_name=f"{device.name}.mgmt.example.com",
            )
            ip.validated_save()
        if not mgmt.ip_addresses.filter(pk=ip.pk).exists():
            mgmt.add_ip_addresses(ip)
        if device.primary_ip4_id != ip.pk:
            device.primary_ip4 = ip
            device.validated_save()

    def seed_ztp(self, reset):
        """Create the ZTP offices, the office pool, and the types, platforms and roles ZTP creates devices with.

        With `reset`, empty every office again: what the design and build created goes, and it is Planned.
        """
        site_type = LocationType.objects.get(name="Site")
        planned = self.statuses["Planned"]
        offices = [
            self.ensure(
                Location, {"name": office}, {"location_type": site_type, "status": planned, "description": about}
            )
            for office, about in ZTP_SITES
        ]
        namespace = Namespace.objects.get(name="Global")
        self.ensure(
            Prefix,
            {"prefix": ztp.OFFICE_POOL, "namespace": namespace},
            {
                "status": self.statuses["Active"],
                "type": "container",
                "description": "Office pool, delegated from Device42",
            },
        )
        for role_name, _, maker, model, platform_name, _, _ in (
            *ztp.OFFICE_DEVICES,
            (ztp.ACCESS_POINT[0], "", *ztp.ACCESS_POINT[1:]),
        ):
            manufacturer = self.ensure(Manufacturer, {"name": maker})
            self.ensure(DeviceType, {"manufacturer": manufacturer, "model": model})
            self.ensure(
                Platform, {"name": platform_name}, {"manufacturer": manufacturer, "network_driver": platform_name}
            )
            role = self.ensure(Role, {"name": role_name})
            role.content_types.add(ContentType.objects.get_for_model(Device))
        if not reset:
            return
        for office in offices:
            # Cables before their devices, IPs before their prefixes, prefixes before their VLANs, racks last.
            Cable.objects.filter(
                Q(interfaces__device__location=office)
                | Q(console_ports__device__location=office)
                | Q(console_server_ports__device__location=office)
            ).distinct().delete()
            removed = Device.objects.filter(location=office).delete()[0]
            IPAddress.objects.filter(parent__locations=office).delete()
            Prefix.objects.filter(locations=office, type="network").delete()
            Prefix.objects.filter(locations=office).delete()
            VLAN.objects.filter(locations=office).delete()
            Rack.objects.filter(location=office).delete()
            if office.status != planned:
                office.status = planned
                office.validated_save()
            self.logger.info("Emptied %s: removed %s objects.", office, removed)

    # ------------------------------------------------------------------------------------- groups, teams, jobs
    def seed_groups(self):
        """Create the filter-defined device groups the campaigns run for, and fill their members."""
        device_type = ContentType.objects.get_for_model(Device)
        for group_name, query in GROUPS.items():
            group = self.ensure(
                DynamicGroup,
                {"name": group_name},
                {
                    "content_type": device_type,
                    "group_type": DynamicGroupTypeChoices.TYPE_DYNAMIC_FILTER,
                    "filter": query,
                },
            )
            group.update_cached_members()

    def seed_teams(self, add_me, password):
        """Create the teams, their permission, and the demo people; put the person running this in every team."""
        teams = [Group.objects.get_or_create(name=team)[0] for team in TEAMS]
        permission = self.ensure(
            ObjectPermission,
            {"name": "NBO demo teams"},
            {"actions": sorted({a for actions in PERMISSIONS.values() for a in actions}), "enabled": True},
        )
        permission.object_types.set(ContentType.objects.get_by_natural_key(*label.split(".")) for label in PERMISSIONS)
        permission.groups.add(*teams)
        if add_me and self.user is not None:
            self.user.groups.add(*teams)
            self.logger.info("Added %s to %s.", self.user, ", ".join(TEAMS))
        if not password:
            return
        for username, team_names in PEOPLE.items():
            person = get_user_model().objects.filter(username=username).first()
            if person is None:
                person = get_user_model().objects.create_user(username=username, password=password)
                self.logger.info("Created %s.", username)
            else:
                person.set_password(password)
                person.save()
            person.groups.add(*Group.objects.filter(name__in=team_names))

    def enable_repository_jobs(self):
        """Enable every job of this repository: Nautobot installs a repository's jobs disabled."""
        package = __name__.rsplit(".", 1)[0]
        enabled = JobModel.objects.filter(module_name__startswith=package, enabled=False).update(enabled=True)
        self.logger.info("Enabled %s jobs in %s.", enabled, package)


JOBS = (SeedDemoData,)

register_jobs(*JOBS)
