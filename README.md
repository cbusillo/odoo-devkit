# odoo-devkit

`odoo-devkit` bootstraps tenant-focused Odoo workspaces from a tracked
`workspace.toml` manifest.

The first implementation target is a conservative `workspace sync` flow that:

- assembles a long-lived but rebuildable workspace under
  `~/Developer/odoo-workspaces/<tenant>` by default,
- treats the active tenant checkout as the source of truth for handwritten
  code,
- emits a `workspace.lock.toml` file with the exact assembled refs,
- generates a minimal runtime config scaffold under `.generated/`, and
- generates a workspace-root `AGENTS.md` plus `docs/README.md` so Every Code
  can use the assembled workspace as a shared cockpit without turning each
  tenant repo into a copy of the shared operating guide, and
- owns the pure PyCharm Odoo-conf helper and the starter templates for thin
  tenant overlays, and
- writes PyCharm-visible shared run configurations for rare-but-important
  commands.

## Command surface

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace scaffold-tenant-overlay --output-dir /path/to/repo --tenant opw
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform runtime select --manifest /path/to/workspace.toml
uv run platform runtime up --manifest /path/to/workspace.toml --build
uv run platform runtime workflow --manifest /path/to/workspace.toml --workflow update
uv run platform runtime restore --manifest /path/to/workspace.toml
uv run platform runtime inspect --manifest /path/to/workspace.toml
```

If `--manifest` is omitted, the CLI looks for `workspace.toml` in the current
directory.

## Current bootstrap scope

This repo is intentionally small. The first pass focuses on the current
pre-split `odoo-ai` tree and does not yet try to materialize separate shared
addon or Odoo core checkouts. The tenant manifest can still declare those repo
relationships so the lock file and generated metadata stay honest.

Current runtime ownership is intentionally narrow and explicit:

- local runtime targets run natively inside `odoo-devkit` against the repo
  declared in `[repos.runtime]`: `select`, `up`, `inspect`, `restore`,
  `workflow bootstrap`, `workflow init`, `workflow update`, and
  `workflow openupgrade`.
- Dokploy-managed non-local runtime targets now also run natively inside
  `odoo-devkit` for `restore`, `workflow bootstrap`, and `workflow update`
  using the runtime repo's generated env plus `platform/dokploy.toml` target
  metadata.
- non-local `workflow init` and `workflow openupgrade` remain local-only and
  fail closed with an explicit `--instance local` requirement instead of
  falling through to an implicit remote path.

## Testing

```bash
uv run python -m unittest discover -s tests
```
