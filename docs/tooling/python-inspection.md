# Linked-worktree Python inspection

The repository declares Python preparation in
`.github/github.json` under `qualityGate.inspection.prepare.python`.
Use the `jetbrains-inspection` skill's `agent-inspect` or `inspect-closeout`
command with `--repo` naming the exact linked worktree. The helper performs
preparation before opening the project; no manual SDK selection is required
when the installed plugin supports Python SDK preparation.

Preparation uses Python 3.13 and `uv sync --locked` in that worktree, then
creates ignored `.idea/misc.xml`, `.idea/modules.xml`, and
`.idea/odoo-devkit.iml`. The single root module includes `odoo_devkit/`,
`docker/scripts/`, and `tests/`, with `tests/` marked as a test root. It uses
that worktree's `.venv/bin/python`, without sibling projects or a
primary-checkout SDK name. Shared inspection profiles remain tracked.
After updating an existing checkout that tracked these files, run preparation
there before opening it again; the old tracked project bindings are removed
by the update. Preparation refuses an existing `.venv` built with another
Python minor version, even one `requires-python` allows. If it reports
`existing virtual environment uses Python 3.x, expected 3.13`, run
`uv venv --clear --python 3.13` in that checkout, then run preparation again.
The update also removes the sibling modules and sibling VCS mappings the old
tracked files attached. To work on Launchplane, `odoo-docker`, or another
sibling, open it as its own PyCharm project. Attaching it to this project does
not persist: preparation rewrites the module files, and the attachment adds a
mapping to the tracked `.idea/vcs.xml`.
The environment contains the root project's locked dependencies; runtime-only
packages supplied by the container may still produce unresolved imports locally.
Runtime scripts also import sibling scripts through their execution directory;
that directory is not declared as a separate IDE source root. Inspect those
imports explicitly before attributing unresolved references to container packages.
The generated module does not exclude `tmp/`; keep scratch Python files out of
whole-project evidence or select explicit files for a bounded assessment.

For a preparation/open-only check, use the skill's `open-worktree` command.
Preparation must leave tracked files and the Git index unchanged. Do not copy
IDE files from another checkout, commit generated interpreter bindings, or
repair the global SDK table by hand. Existing ignored IDE state is local;
preparation owns the three generated files named above.

A successful setup is not a clean inspection result. Require current GREEN or
RED evidence covering the requested files and verified helper-owned project
closure. Retrieve and triage every finding when RED; do not report UNKNOWN as
clean. If SDK preparation is unavailable in the installed plugin, retain the
helper's diagnostic and follow the skill's documented setup path rather than
repeating an unchanged failed assessment.

## Container-only startup diagnostics

Keep inspections enabled for `docker/scripts/`; do not exclude the directory
or suppress its import and SQL inspections. For a bounded startup assessment,
select `odoo_devkit/manifest.py`, `docker/scripts/run_odoo_startup.py`, and
`tests/test_odoo_startup.py` explicitly, plus any other changed Python files.

Two diagnostic categories in `run_odoo_startup.py` are known environment noise
in the root project's local interpreter:

- `psycopg2` is imported by the container startup wrapper but is not a root
  project dependency. The selected Odoo base runtime supplies the PostgreSQL
  driver. Startup unit tests substitute a driver that rejects unexpected
  connections, so they do not prove container connectivity.
- `SqlResolveInspection` cannot resolve `ir_module_module` or its `name` and
  `state` columns without the Odoo database schema. The query reads installed
  modules from the container's Odoo database; the local IDE has no attached
  database schema.

Record these findings individually when they appear in a current inspection;
they may leave the raw verdict RED after actionable Python findings are fixed.
This triage does not prove a live container or database healthy. New import or
SQL findings still need investigation; this is not a baseline for all runtime
script diagnostics. An UNKNOWN run supplies no current finding evidence.

## Runtime and workflow findings

For runtime triage, select `odoo_devkit/local_runtime.py`,
`tests/test_runtime.py`, `docker/scripts/run_odoo_data_workflows.py`,
`tests/test_odoo_data_workflows.py`, `docker/scripts/run_odoo_startup.py`, and
`tests/test_odoo_startup.py`; include the README and workspace CLI guide when
triaging their prose. Compare with an unchanged checkout of the current default
branch, and retrieve the complete problem list. A compact assessment may omit
individual findings even when its count covers the complete native run.

Keep each retained diagnostic visible in the issue's inspection receipt and
record its location and reason. These cases are intentional:

- The workflow script's `psycopg2`, `sql`, and `connection` import diagnostics
  have the same container-driver reason as startup's `psycopg2` diagnostic above.
  Root dependencies and the offline test loader do not supply a real driver.
- `_path_exists_safely` catches `OSError` and returns `False`. Its final return
  can be reported as unreachable by the IDE. With the repository's Python 3.13
  inspection interpreter, `Path.exists()` raises `OSError` for an overlong path component. Preserve that fallback.
- `_drop_database_after_failed_restore` deliberately catches `BaseException`
  from the cleanup attempt. It logs a failed drop while allowing the caller to
  propagate the original restore error, including an interruption. Narrowing
  that catch would replace the original error when cleanup is interrupted.
- Bootstrap and restore commit the cached connection after `sanitize_database`
  establishes it. A warning on the optional `db_conn` field at those commits
  does not establish a missing connection on the production path; test fixtures
  must preserve the connection contract too.
- `_normalize_path` accepts raw validator input and converts it to a string;
  `emit_key_value_payload` prints generic scalar values. Their object-to-string
  warnings describe deliberate conversion boundaries.
- Tests intentionally import private CLI handlers to exercise their behavior.
  A protected-member warning is not a reason to expose those handlers publicly.
- Spellchecking flags product names, executable/environment identifiers, Odoo
  schema names, synthetic test inputs, and source-reference tokens. Keep those
  exact values. Grammar findings inside identifiers, command examples, and
  deliberately malformed fixtures need the same location-specific review;
  ordinary prose errors should be corrected.

The workflow test loader still substitutes PostgreSQL imports before executing
container scripts. Its `TYPE_CHECKING` import makes the real module visible to
static analysis without loading a PostgreSQL driver during offline tests.
Mocks used by connection consumers return the same fake connection stored on the runner, matching
`connect_to_db()`'s contract. Callback fixtures bind their subtest values with
`functools.partial`, keeping captures explicit without mutable default arguments.
These fixtures do not qualify a running Odoo or database.
