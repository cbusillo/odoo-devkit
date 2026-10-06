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
The runner removes only its own named resources in `finally`. GitHub's fresh
hosted runner supplies an additional cleanup boundary for job cancellation.

`odoo.log` and `result.json` are written to the output directory. The result
records selected modules, external source commits, image, test count and elapsed
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
on Chris-Studio; temporary build contexts stay under that directory. This runner
does not use a live runtime, restore any database, or exercise devkit startup
or restore scripts. Existing browser harnesses can still skip a broken suite;
their failure propagation is tracked in shared-addons #40 and is separate from
this runner's Odoo process/summary gate.
