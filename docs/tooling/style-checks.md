# Shared style checks

`quality/ruff.toml` implements the default selection in the
[Python and Odoo style guide](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/python-odoo-style.md#tooling).
It also selects Ruff's return and simplification families for the guide's shape
rules, following the recorded adoption decision on the owning issue.
It is an explicit, opt-in configuration. Devkit's existing `pyproject.toml`
configuration still governs its default commands. Adoption and baseline triage
are tracked in [odoo-devkit#302](https://github.com/cbusillo/odoo-devkit/issues/302).

Use the file from the consumer's exact pinned devkit checkout, without copying
its rule selection into another configuration. From a tenant checkout with its
manifest-declared devkit checkout at `../odoo-devkit`:

```bash
uv run --project ../odoo-devkit --locked ruff check --config ../odoo-devkit/quality/ruff.toml .
uv run --project ../odoo-devkit --locked ruff format --check --config ../odoo-devkit/quality/ruff.toml .
```

Ruff is supplied by devkit's locked development dependencies. The consumer's
pinned devkit checkout owns both its version and the shared configuration.

[`.idea/inspectionProfiles/Project_Default.xml`](../../.idea/inspectionProfiles/Project_Default.xml)
is the existing shared inspection asset. Keep it at that path as a regular
tracked file: the native inspection helper does not accept tracked symlinks.
This preparation changes no inspection settings. It does not select the profile
for tenants or reconcile their existing settings. Tenant adoption must preserve
or explicitly resolve those differences first.
Native inspection continues through the catalog's `jetbrains-inspection` skill;
see [Python inspection](python-inspection.md).

A nonzero baseline is work to triage before making these commands required.
Do not hide it with a blanket suppression, an ignored directory, or a
success exit. Odoo consumes manifest dictionary expressions, so the shared
configuration ignores `B018` only in files named `__manifest__.py`. Every other
selected rule still applies there; `B018` remains enabled in other files.
Other rule changes go through the owning issue's Director question path.
The shared files do not add a CI job or alter required checks.
