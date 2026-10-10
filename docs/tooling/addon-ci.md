# Odoo addon CI

The `addon-tests` composite action in this repository runs the caller's addon
suites on a fresh database. Callers pin the action to an exact devkit commit.
Tenant callers provide their checkout and a shared-addons checkout; the runner
reads the tenant's devtools image and Odoo version from `workspace.toml` and
common and context-default external addon sources from `artifact-inputs.toml`
(local test selection; hosted instance overrides are excluded). Shared-addons callers
provide the community devtools image and derive the Odoo series from an addon
manifest.

The runner installs all installable owned addons, including ones without tests,
and selects tests by the owned module names. Shared addons are installed as
tenant dependencies without selecting their tests again. Test discovery remains
Odoo's responsibility. Browser suites use the image's Chromium plus the locked
CI-only `websocket-client` dependency catalog in `docker/addon-tests`.

Runtime-support, CI-only and tenant locks are checked offline and exported with
frozen semantics. The test image extends the
base Python environment additively, constraining every installed base package
to its existing version, and checks the final environment. A dependency conflict
fails the build instead of replacing a base-owned package. External source
selectors are resolved to commits, recorded in the result, and copied into the
throwaway test image. Branch, tag and exact source references are accepted;
private sources use the caller's existing source-read binding through the
`source-token` input and fail without one. Auth is ephemeral to the Git child;
it is never written into clone configuration, arguments, images or test containers.
No image is published. Browser preflight requires Odoo's core executable finder
to locate Chromium, launch its version check, and import `websocket`.

Each invocation creates uniquely named Docker resources: an internal network,
Postgres with temporary database storage, a test container and a test image.
Addon checkouts are mounted read-only. The containers expose no host ports;
the test network has no external connectivity and receives no credentials.
The build context is a temporary sibling of the evidence directory, so a
cancelled job cannot upload private source as test evidence; Git history is
excluded from the build context. Docker's normal build cache may retain source
layers, under its existing host cache policy. The runner removes only its own named resources in `finally`. GitHub's fresh
hosted runner supplies an additional cleanup boundary for job cancellation.

`odoo.log` and `result.json` are written to the output directory. The result
records selected modules, external source commits, requested image, the actual
built test image's immutable ID, test count and elapsed
seconds; the elapsed time is also reported in the Actions job summary. A missing
summary, zero tests, reported failures/errors, or nonzero Odoo exit fails the job.
Callers upload the evidence even when the run fails and apply path filters to
addon and test-input changes, so docs-only changes do not run the suites.

Local example, from an isolated devkit checkout:

```bash
uv run python -m odoo_devkit.addon_ci \
  --tenant /path/to/tenant \
  --addons-root /path/to/tenant/addons \
  --support-root /path/to/shared-addons \
  --output /path/to/task-evidence/addon-tests
```

Docker and uv are required. Choose an output directory on Developer-Artifacts
on Chris-Studio; temporary build contexts stay beside that directory. The catalog's
Python requirement is installed before offline lock checks. This runner
uses only throwaway containers, never an installed runtime or Client data.
Existing browser harnesses can still skip a broken suite;
their failure propagation is tracked in shared-addons #40 and is separate from
this runner's Odoo process/summary gate.

## Devkit live checks

The `Devkit live Odoo checks` workflow runs the same runner with
`--devkit-checks` and the test addon in `tests/live_odoo/addons`. It mounts
the checked-out devkit scripts read-only and uses the real `launchplane_settings`
addon from the workflow's pinned shared-addons revision. The source commit is
included in the uploaded evidence. It runs when runtime scripts, test inputs,
the runner or its workflow change, including on merge-train branches.

The suite creates an Odoo source database containing only synthetic records and
clones a fresh target for each check. It verifies public startup rejects an
active `admin`/`admin` before the server exec boundary, unchanged startup
passwords produce no second write, both startup and data-workflow settings
payloads apply through the installed addon, startup parses real environment
settings, bootstrap initializes and hardens
the administrator, and restores replace database and filestore contents, harden
the administrator and drop a failed partial restore. A synthetic `ir.attachment`
is created in the source and read back through Odoo after restore, proving that
the copied filestore is the one Odoo serves. The devkit-check mode records the
base image ID and repository digests and builds from that resolved digest when
available; a cached local-only image remains supported. Synthetic community and
Enterprise repository fixtures prove that AUTO updates reload the community
addon while leaving the Enterprise addon unchanged.

Each behavior check first passes normally, then runs with a deliberate fault
in the product path and must fail its specific assertion. Unexpected setup or Odoo
exceptions do not count as fault detection. `DEVKIT_FAULT_DETECTED` lines in
`odoo.log` identify the proofs. The runner's normal nonzero exit, failure and
nonempty-test-summary checks still decide the lane result.

Credential checks exercise real registry, install and XML update hooks with
inert production and lane sentinels. An independent connection records commit
and hook order without key values. Outbound sinks refuse before opening a
socket. A planted late boundary must reach those sinks and fail the behavior
check; failed and rolled-back commits must start no hook. The restore proof
keeps its audit outside the disposable target so rollback cannot erase the
unsafe-hook observation.

The restore test replaces SSH transport with execution inside the throwaway
container. The production capture, dump validation, rsync, database replacement,
preparation and rollback code executes with real Postgres and Odoo; this does
not test remote SSH authentication or connectivity. No SSH credentials or
real backups are used. Startup observes the final exec boundary instead of
starting a long-running web server.

Local command, with a shared-addons checkout containing `launchplane_settings`
and `test_support`:

```bash
uv run python -m odoo_devkit.addon_ci \
  --image <community-devtools-image> \
  --addons-root tests/live_odoo/addons \
  --support-root /path/to/shared-addons \
  --devkit-checks \
  --output /path/to/task-evidence/devkit-live-checks
```

Use the workflow's image selection. A sparse support checkout keeps unrelated
addon dependencies out of this lane. The normal addon-CI path continues to run
without mounting the devkit scripts or synthetic Enterprise fixtures.
