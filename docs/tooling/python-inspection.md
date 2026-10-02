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
The environment contains the root project's locked dependencies; runtime-only
packages supplied by the container may still produce unresolved imports locally.

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
