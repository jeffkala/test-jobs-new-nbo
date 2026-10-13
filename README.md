# Site build demo

An example Nautobot Git repository for trying NBO: four mock jobs and the NBO definitions that run them as a site
build. Nothing here touches the network or changes Nautobot's data.

```text
site-build-demo/
├── __init__.py
├── jobs/
│   ├── __init__.py          # register_jobs(...)
│   └── site_build.py        # the four mock jobs
└── nbo_properties/
    ├── nbo_step_types/
    │   └── site_build_step_types.yml
    └── nbo_workflows/
        └── site_build_workflows.yml
```

## The mock jobs

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

## The workflow

"Site build demo" runs, in order:

1. **Site intake**, an input step. A person fills in the site code (such as `SJC04`), name, device count and image. The values land in the record's `workflow_data` under `site_intake`.
2. **Allocate site prefixes**, an auto step with `retry: safe`, reading the site code and device count.
3. **Render site configs**, an auto step with `retry: safe`.
4. **Push site configs**, an auto step with `retry: confirm`, a 10-minute `timeout` and a rollback.
5. **Site acceptance**, a manual step with `retry: never`, which a person completes.

**Roll back site configs** is a handler: the record gets no step for it, and **Run rollback** on a failed push runs
it. The failure chances are 25% for the first two jobs, 40% for the push and 10% for the rollback; change
`failure_percent` in `site_build_step_types.yml` to see more or fewer failures.

## Importing it

Nautobot syncs a whole repository, so copy this directory into a Git repository of its own:

```shell
cp -r examples/site-build-demo /tmp/site-build-demo
cd /tmp/site-build-demo && git init -b main && git add . && git commit -m "Site build demo"
# push it to a Git server Nautobot can reach, or point Nautobot at a local path it can clone
```

1. In Nautobot, go to **Extensibility › Git Repositories › Add**, give it a name and the URL, and under **Provides**
   tick **jobs**, **NBO Step Types** and **NBO Workflows**. Save; the sync runs.
2. Nautobot creates synced jobs disabled. Under **Jobs › Jobs**, enable the four "Site build demo" jobs. Until then
   the sync log warns that each step's job is disabled.
3. Run **Validate NBO definitions** against the repository to see what NBO loads and how it binds each job.

## Trying it

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

Retry with changes, Run rollback, Mark done by hand and the stuck-step actions need the `override` action in an
object permission on NBO steps; a superuser has it.
