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
is the shared inspection asset. Keep it at that path as a regular
tracked file: the native inspection helper does not accept tracked symlinks.
The profile keeps the enabled OPW/CM checks and their options. The unused-CSS
inspection remains disabled, as in both tenant profiles, following the amended
[Director decision](https://github.com/cbusillo/odoo-devkit/issues/302#issuecomment-6609065384).
There is no Odoo SCSS scope: Odoo bundles assets across addons and uses QWeb
selectors the IDE does not index reliably.
Its Python compatibility
inspection checks 3.13 and 3.14, following the
[Director decision](https://github.com/cbusillo/odoo-devkit/issues/302#issuecomment-6607466966).
Ruff continues to target the current 3.13 runtime. The
[image upgrade](https://github.com/cbusillo/odoo-devkit/issues/373) owns raising that
target after the images move. Tenant selection and pointer adoption remain on
the owning issue.
Native inspection continues through the catalog's `jetbrains-inspection` skill;
see [Python inspection](python-inspection.md). Resolving quoted Odoo model/value
types also requires the Odoo IDE plugin, the matching Odoo source roots and the
correct interpreter. The
[worker setup report](https://github.com/cbusillo/codex-skills/issues/3363)
covers SDK/plugin setup and measured checker limits; its Pydantic/Click sample
does not itself prove Odoo model resolution.

A nonzero baseline is work to triage before making these commands required.
Do not hide it with a blanket suppression, an ignored directory, or a
success exit. Odoo consumes manifest dictionary expressions, so the shared
configuration ignores `B018` only in files named `__manifest__.py`. Every other
selected rule still applies there; `B018` remains enabled in other files.
Other rule changes go through the owning issue's Director question path.
The shared files do not add a CI job or alter required checks.
