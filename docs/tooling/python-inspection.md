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
