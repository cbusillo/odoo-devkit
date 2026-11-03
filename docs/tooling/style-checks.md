# Shared style checks

`quality/ruff.toml` implements the default selection in the
[Python and Odoo style guide](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/python-odoo-style.md#tooling).
It is an explicit, opt-in configuration. Devkit's existing `pyproject.toml`
configuration still governs its default commands. Adoption and baseline triage
are tracked in [odoo-devkit#302](https://github.com/cbusillo/odoo-devkit/issues/302).

Use the file from the consumer's exact pinned devkit checkout, without copying
its rule selection into another configuration. From a tenant checkout with its
manifest-declared devkit checkout at `../odoo-devkit`:

```bash
uv tool run --from ruff==0.17.2 ruff check --config ../odoo-devkit/quality/ruff.toml .
uv tool run --from ruff==0.17.2 ruff format --check --config ../odoo-devkit/quality/ruff.toml .
```

`quality/Project_Default.xml` is devkit's existing inspection profile. Its
`.idea/inspectionProfiles/Project_Default.xml` points to that same file. The
move changes no inspections, severity, scope, or compatibility targets. It does
not select this profile for tenants or reconcile their existing settings.
Tenant adoption must preserve or explicitly resolve those differences first.
Native inspection continues through the catalog's `jetbrains-inspection` skill;
see [Python inspection](python-inspection.md).

A nonzero baseline is work to triage before making these commands required.
Do not hide it with a blanket suppression, an ignored directory, or a
success exit. Manifest dictionaries are consumed by Odoo and currently trigger
Ruff's `B018`; the proposed manifest-only exception needs the Director's
answer on the owning issue before adoption. Other rule changes use the same
question path. The shared files do not add a CI job or alter required checks.
