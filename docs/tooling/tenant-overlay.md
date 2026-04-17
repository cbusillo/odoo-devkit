# Tenant Overlay Guide

Purpose

- Define the thin repo-root shape a tenant repo keeps in the current workspace
  model.

When

- Before creating or auditing a tenant repo root.

## Principle

- Keep the tenant repo focused on tenant-owned code and tenant-owned docs.
- Keep shared DX/runtime/bootstrap guidance in `odoo-devkit`.
- Keep the workspace root as the generated Every Code cockpit.
- Let the tenant repo keep thin run configurations and shell helpers that call
  the sibling `odoo-devkit` repo, while shared-addon source stays explicit in
  the manifest.
- Keep tenant-owned addons directly under the tenant repo's `addons/`
  directory. Do not add an extra `addons/<tenant>/` bucket unless tooling grows
  a concrete first-class contract around that directory.

## Tenant Root Should Contain

- a thin `AGENTS.md`
- a thin `docs/README.md`
- tenant-specific docs
- tracked `workspace.toml`
- tenant-owned code

## Tenant Root Should Not Contain

- the full shared operating guide
- shared workspace CLI docs
- shared command patterns
- shared architecture docs that apply equally to all tenants

## Template Files

- `templates/tenant-overlay/AGENTS.md`
- `templates/tenant-overlay/docs/README.md`
- `templates/tenant-overlay/scripts/workspace-sync`
- `templates/tenant-overlay/scripts/workspace-status`
- `templates/tenant-overlay/workspace.toml`

## Scaffold Contract

- The scaffold points shared addons at `../odoo-shared-addons` while local
  runtime assets come from `odoo-devkit` itself.
- The scaffold also keeps `[repos.runtime]` pointed at the sibling
  `../odoo-devkit` checkout so the same tracked manifest can keep
  `instance = "local"` while still running Dokploy-managed data workflows
  through an explicit runtime `--instance` override.
- Release actions for remote environments still belong in
  `odoo-control-plane`, not in tenant-root `platform runtime` commands.
- The generated `Workspace Sync` and `Workspace Status` entrypoints call the
  tenant-root helper scripts so the manifest stays anchored at the tenant repo
  root.
- Runtime run configurations continue to call
  `uv --directory ../odoo-devkit run platform ...` directly.
- For terminal use, tenant repos should prefer `./scripts/workspace-sync`
  and `./scripts/workspace-status` as anchored convenience commands so the
  manifest path stays tied to the tenant repo root.

## Scaffold Command

```bash
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/odoo-tenant-opw \
  --tenant opw
```

## Working Rule

- If you feel pressure to copy a large shared doc into a tenant repo, that is a
  sign it probably belongs in `odoo-devkit` and should be linked from the
  workspace root instead.
