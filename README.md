# NBO demo

An example Nautobot Git repository for trying NBO. It holds two demos, each a set of mock jobs and the NBO
definitions that run them:

- **Site build**, one record: an input step, three chained jobs, a rollback handler and a manual acceptance.
- **OS upgrade campaign**, two workflows: a campaign record that opens one child record per device, and the
  device's upgrade procedure, from pre-checks to validation.

Nothing here touches the network or changes Nautobot's data, apart from NBO's own records.

```text
site-build-demo/
├── __init__.py
├── jobs/
│   ├── __init__.py          # register_jobs(...)
│   ├── site_build.py        # the site build's four mock jobs
│   └── os_upgrade.py        # the OS upgrade's eleven mock jobs and its mock wait check
└── nbo_properties/
    ├── nbo_step_types/
    │   ├── site_build_step_types.yml
    │   └── os_upgrade_step_types.yml
    └── nbo_workflows/
        ├── site_build_workflows.yml
        └── os_upgrade_workflows.yml
```

## Importing it

Nautobot syncs a whole repository, so copy this directory into a Git repository of its own:

```shell
cp -r examples/site-build-demo /tmp/nbo-demo
cd /tmp/nbo-demo && git init -b main && git add . && git commit -m "NBO demo"
# push it to a Git server Nautobot can reach, or point Nautobot at a local path it can clone
```

1. In Nautobot, go to **Extensibility › Git Repositories › Add**.
    - Name it **NBO demo**, so that its slug is `nbo_demo`. The OS upgrade's wait names its check by class path,
      `nbo_demo.jobs.os_upgrade.DeviceBackAfterReboot`, which starts with the slug. With another slug, change
      `wait.check` in `os_upgrade_step_types.yml` to match.
    - Under **Provides**, tick **jobs**, **NBO Step Types** and **NBO Workflows**. Save; the sync runs.
2. Nautobot creates synced jobs disabled. Under **Jobs › Jobs**, enable:
    - the four "Site build demo" jobs;
    - the eleven "OS upgrade demo" jobs (the check, "OS upgrade demo - Device back after reboot", needs no enabling:
      NBO's check runner calls it directly);
    - NBO's own **NBO Workflow - Complete Workflow** and **Create NBO campaign**, if they aren't enabled yet.
3. Run **Validate NBO definitions** against the repository to see what NBO loads and how it binds each job. It
   should refuse nothing and warn of nothing.
4. For the OS upgrade's wait, make sure a Celery worker serves the `nbo-steps` queue, where NBO's check runner runs:
   `nautobot-server celery worker --queues nbo-steps`.

## Site build

### The mock jobs

| Job | What it pretends to do |
| --- | --- |
| Site build demo - Allocate site prefixes | Allocate the site's prefixes |
| Site build demo - Render site configs | Render device configurations |
| Site build demo - Push site configs | Push the configurations |
| Site build demo - Roll back site configs | Roll back a push |

Each job runs for a random time between `min_seconds` (60) and `max_seconds` (300), logging every 30 seconds. With
the chance in `failure_percent`, it stops part-way with an error such as:

```text
Provoked stop: Site build demo - Push site configs was stopped on purpose after 112s (failure chance 40%), to show how NBO handles a failed step.
```

The jobs don't use `NBOStepStatusMixin`, so NBO finishes their steps from their JobResult. Their soft time limit is
600 seconds, above Nautobot's default 300, so a 5-minute run isn't cut short.

### The workflow

"Site build demo" runs, in order:

1. **Site intake**, an input step. A person fills in the site code (such as `SJC04`), name, device count and image. The values land in the record's `workflow_data` under `site_intake`.
2. **Allocate site prefixes**, an auto step with `retry: safe`, reading the site code and device count.
3. **Render site configs**, an auto step with `retry: safe`.
4. **Push site configs**, an auto step with `retry: confirm`, a 10-minute `timeout` and a rollback.
5. **Site acceptance**, a manual step with `retry: never`, which a person completes.

**Roll back site configs** is a handler: the record gets no step for it, and **Run rollback** on a failed push runs
it. The failure chances are 25% for the first two jobs, 40% for the push and 10% for the rollback; change
`failure_percent` in `site_build_step_types.yml` to see more or fewer failures.

### Trying it

1. Run **Orchestrator › Create NBO Record** with the workflow "Site build demo". A device is optional.
2. On the record, click **Fill in** on Site intake, enter the values and **Submit**. Allocate site prefixes starts,
   and each job that succeeds starts the next.
3. When a job stops on purpose, its step is Failed with the "Provoked stop" reason:
    - **Retry** on Allocate or Render runs it again at one click.
    - **Retry…** on Push asks first, and lets you change the image for that attempt, with a reason.
    - **Run rollback…** on a failed Push runs the handler; the step stays Failed, so retry it afterwards.
4. Kill a worker during the push to see it under **Orchestrator › Operations › Stuck steps** after 10 minutes, then
   use **Mark failed** or **Sync from result**.
5. The **Attempts** card on the record shows every run, retry and rollback, with who started it and why.

## OS upgrade campaign

### The two workflows

**OS upgrade demo - Campaign** is the campaign record:

1. **Campaign intake**, an input step: the target version (such as `17.12.4`), the image size in MB and the
   maintenance window, under `campaign` in the record's workflow data.
2. **Wait for children**, NBO's built-in wait. It completes when every device record is Completed or Skipped, and at
   least one is Completed; then NBO sets the campaign record Completed.

**OS upgrade demo - Device** is each device's record, one per device:

| # | Step | Kind | What happens |
| --- | --- | --- | --- |
| 1 | Start | manual | A person sets it Completed to start the device. |
| 2–6 | Pre-check interfaces, routing, neighbors, processor, uptime | auto | Each records one set of numbers under `pre_check_<name>`. |
| 7 | Check free space | auto | Fails when the image wouldn't fit on flash, so the upload never starts. |
| 8 | Upload OS | auto | Copies the campaign's target version; `retry: confirm`, a 15-minute `timeout`. |
| 9 | Install OS | manual | A person runs it from the step's **Job Form**. |
| 10 | Reboot device | manual | A person runs it from the **Job Form**; it records the reload time. `retry: never`. |
| 11 | Wait for the device | wait | Checks every minute, for up to an hour, until the device answers again. |
| 12 | Post-check | auto | Records the same numbers again, under `post_check`. |
| 13 | Validate upgrade | auto | Compares the pre-checks with the post-check, and fails on a difference. |
| 14 | Complete device record | auto | NBO's "Complete Workflow" sets the record Completed, which the campaign counts. |

The upload, the install and the free space check read the campaign's values through
`nbo_record_parent_workflow_data`, so each device uses the version entered once, on the campaign record.

### How "only if there is free space" works

NBO 2.2 has no step conditions; `when` arrives in 3.0. The upload runs only after free space because chaining stops
at a Failed step: when the free space check fails, the upload stays Not Started until someone frees space and
retries the check.

### The mock jobs

The jobs make up each device's numbers from its ID, so a device reports the same numbers every run. They take 10 to
45 seconds, the upload 1 to 3 minutes and the install 30 to 90 seconds, and they fail now and then:

| Chance | Default | Where to change it |
| --- | --- | --- |
| A job stops on purpose ("Provoked stop") | 5%; 10% for the upload and the install | `failure_percent` |
| Too little free space | 20% | `short_percent` on Check free space |
| A BGP neighbor is lost after the reload, so validation fails | 15% | `mismatch_percent` on Post-check |
| The device never answers, so the wait fails at its one-hour deadline | about 1 device in 10 | fixed, by device ID |

A device comes back 2 to 8 minutes after its reload. The validation fails when interfaces, routes or neighbors
differ, or when the uptime didn't reset; a change in processor load is only logged.

The jobs use `NBOStepStatusMixin`, unlike the site build's. The install and the reboot run from Nautobot's job form,
outside NBO's own start, and the mixin's hooks record such a run as the step's attempt and set the step's status.

### Trying it

1. Run **Orchestrator › Create NBO Campaign** with:
    - **Workflow**: "OS upgrade demo - Device";
    - **Parent workflow**: "OS upgrade demo - Campaign";
    - **Devices**: a few devices, or a **Dynamic group** of them;
    - a **Campaign name**, such as `upg`, and a **Change Request ID**, such as `CHG100`.

   It creates the campaign record `upg-CHG100` and one child per device, all Not Started.
2. On the campaign record, click **Fill in** on Campaign intake, enter the target version and **Submit**. Its
   "Wait for children" starts, and the record page shows the roll-up of its devices.
3. Start the devices: on each device record, set **Start** to Completed. To start many at once, filter the NBO Steps
   list to the Start steps, select them, and bulk edit their status to Completed. The pre-checks start on their own.
4. When a device reaches **Install OS**, open its **Job Form** and run it; then do the same for **Reboot device**.
   The wait starts, and the step page shows its checks.
5. Watch the failures:
    - A failed check or job shows **Retry** or **Retry…**. Retry the free space check after "freeing space"; with
      the 20% chance it will usually pass.
    - A failed validation names what changed, for example `neighbors changed from {'bgp': 3, 'ospf': 2} to {'bgp': 2, 'ospf': 2}`.
      The record's workflow data keeps the pre-checks, the post-check and the validation side by side.
    - A device that never answers fails its wait after an hour. Retry the wait, or **Abandon** the device record:
      the campaign then counts it as Skipped.
6. When the last device record is Completed, "Wait for children" completes within about a minute, and the campaign
   record is Completed.

Retry with changes, Run rollback, Mark done by hand and the stuck-step actions need the `override` action in an
object permission on NBO steps; a superuser has it.
