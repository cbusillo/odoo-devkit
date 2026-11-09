# Shared style checks

`quality/ruff.toml` implements the default selection in the
[Python and Odoo style guide](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/python-odoo-style.md#tooling).
It also selects Ruff's return and simplification families for the guide's shape
rules, following the recorded adoption decision on the owning issue.
It is an explicit, opt-in configuration. Devkit's existing `pyproject.toml`
configuration still governs its default commands. Adoption and baseline triage
are tracked in [odoo-devkit#302](https://github.com/cbusillo/odoo-devkit/issues/302).

Use the file from the consumer's exact pinned devkit checkout, without copying
its rule selection into another configuration. An adopted consumer's root
`ruff.toml` extends it through the existing `ODOO_DEVKIT_REPO` override, with the
normal sibling checkout as its default. Keep existing generated-source scopes
in the owning nested configuration; normal Ruff discovery preserves them.
From the consumer checkout:

```bash
uv run --project "${ODOO_DEVKIT_REPO:-../odoo-devkit}" --locked ruff check .
uv run --project "${ODOO_DEVKIT_REPO:-../odoo-devkit}" --locked ruff format --check .
```

Ruff is supplied by devkit's locked development dependencies. The consumer's
pinned devkit checkout owns both its version and the shared configuration.
The shared configuration leaves `target-version` unset so normal Ruff discovery
uses the consumer's `pyproject.toml` `requires-python`: Python 3.14 for adopted
Odoo 20 consumers and Python 3.13 for supported Odoo 19 consumers. Devkit's root
tools retain their own Python 3.13 target. Consumers without Python project
metadata use Ruff's conservative default; an explicit consumer target still
takes precedence. Use normal configuration discovery, since passing a file with
`--config` disables version inference. See
[Ruff's version inference](https://docs.astral.sh/ruff/configuration/#inferring-the-python-version).

[`.idea/inspectionProfiles/Project_Default.xml`](../../.idea/inspectionProfiles/Project_Default.xml)
is the shared inspection asset. Keep it at that path as a regular
tracked file: the native inspection helper does not accept tracked symlinks.
The profile keeps the enabled OPW/CM checks and their options. The unused-CSS
inspection remains disabled, as in both tenant profiles, following the amended
[Director decision](https://github.com/cbusillo/odoo-devkit/issues/302#issuecomment-6609065384).
There is no Odoo SCSS scope: Odoo bundles assets across addons and uses QWeb
selectors the IDE does not index reliably.
Its Python compatibility
inspection checks 3.13 and 3.14 for the supported consumers, following the
[Director decision](https://github.com/cbusillo/odoo-devkit/issues/302#issuecomment-6607466966).
Both checks remain necessary while Odoo 19 and root tools support 3.13;
the profile has no separate Odoo 20 temporary target to retire. The
[Odoo 20 image qualification and preview proof](https://github.com/cbusillo/odoo-devkit/issues/373)
precede the consumer's 3.14 lint target. Tenant selection and pointer adoption
remain on the owning issue. Consumers declare the external profile path in
`qualityGate.inspection.profile`; task worktrees use the inspection helper's
`--profile` option when their devkit is outside the sibling layout. The
canonical file remains in devkit rather than in each consumer.
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
The addon-test action runs declared Ruff lint and format commands before Odoo
tests, as described in [addon CI](addon-ci.md). Both must pass for that CI job
to pass. PyCharm remains the local pre-push gate; weekly project-wide inspection
is separate from this adoption.
