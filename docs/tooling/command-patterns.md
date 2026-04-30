# Workspace Command Patterns

Purpose

- Provide common `uv run platform workspace ...` invocation patterns without
  mixing them into the stable contract doc.

When

- When you know you need the workspace CLI and want concrete examples.

## Quick Start

These examples are workspace and local/data-workflow patterns. Remote release
actions such as ship, promote, and Launchplane preview lifecycle belong in
`launchplane`.

- Sync the current tenant workspace:

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
```

- Inspect the materialized workspace:

```bash
uv run platform workspace status --manifest /path/to/workspace.toml
```

- Run a command from the workspace root:

```bash
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform workspace run --manifest /path/to/workspace.toml -- ls sources
```

- Rebuild from scratch:

```bash
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace sync --manifest /path/to/workspace.toml
```

- Scaffold the thin overlay into a new tenant repo:

```bash
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/odoo-tenant-opw \
  --tenant opw
```

## Typical Flow

1. Edit code in the tenant repo or `odoo-devkit`.
2. Re-run `workspace sync` when the workspace contract or generated surface
   changes.
3. Start Every Code from the workspace root.
4. Keep PyCharm opened on the tenant repo.

## What To Check After `workspace sync`

- `sources/tenant/` points at the expected tenant checkout.
- `sources/devkit/` points at the expected devkit checkout.
- `workspace.lock.toml` reflects the assembled state.
- `AGENTS.md`, `docs/README.md`, and `docs/session-prompt.md` exist at
  workspace root.
- `.generated/pycharm/project-metadata.json` reflects the attached roots.

## Guardrails

- Do not hand-edit generated workspace-root cockpit files.
- If the workspace surface is wrong, fix `odoo-devkit` and re-sync.
- Keep implementation-specific, non-secret local facts in an untracked
  `AGENTS.override.md`; keep credentials in `.env`.
- Keep tenant repo docs thin; use the generated workspace docs index for shared
  guidance.
