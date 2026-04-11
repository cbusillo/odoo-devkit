# odoo-devkit Docs

This repo owns the shared DX/runtime contract used to assemble tenant
workspaces.

## Start Here

- [../AGENTS.md](../AGENTS.md) for the shared operating guide.
- [../README.md](../README.md) for the current bootstrap scope and command
  surface.
- [ARCHITECTURE.md](ARCHITECTURE.md) for the workspace-first ownership model.
- [roles.md](roles.md) for shared Codex role expectations.
- [tooling/workspace-cli.md](tooling/workspace-cli.md) for the workspace
  command surface and generated-output contract.
- [tooling/command-patterns.md](tooling/command-patterns.md) for concrete
  workspace command examples.
- [tooling/tenant-overlay.md](tooling/tenant-overlay.md) for the thin tenant
  repo shape used by the split.

## Shared Responsibilities

- Define how `workspace.toml` is interpreted.
- Generate the workspace-root Every Code surface.
- Generate PyCharm metadata and run configurations while keeping the IDE
  tenant-focused.
- Own the pure PyCharm Odoo-conf rendering helper shared by tenant repos.
- Keep the assembled workspace rebuildable and explicit about source-of-truth
  ownership.

## Current Outputs

- workspace-root `AGENTS.md`
- workspace-root `docs/README.md`
- `.generated/odoo.conf`
- `.generated/runtime.env`
- `.generated/pycharm/project-metadata.json`
- `workspace.lock.toml`

## Notes

- Tenant repos should link back here for shared operating guidance instead of
  copying the full shared docs tree.
- The generated workspace root is the shared Every Code cockpit, but the source
  of truth for these docs remains in `odoo-devkit`.
- Tenant overlay starter files live under `templates/tenant-overlay/`.
