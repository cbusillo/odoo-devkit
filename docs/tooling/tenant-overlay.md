# Tenant Overlay Guide

Purpose

- Define the thin repo-root shape a tenant repo should keep after extraction.

When

- Before creating a new tenant repo or trimming a pre-split tenant root.

## Principle

- Keep the tenant repo focused on tenant-owned code and tenant-owned docs.
- Keep shared DX/runtime/bootstrap guidance in `odoo-devkit`.
- Keep the workspace root as the generated Every Code cockpit.
- Until runtime ownership is split cleanly, let the tenant repo keep thin
  run-config wrappers that call the sibling `odoo-ai` repo for platform
  lifecycle commands.

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

## Current Bootstrap Assumption

- The scaffold currently points shared addons at `../odoo-ai/addons/shared`.
- The generated run configurations bridge runtime commands through
  `uv --directory ../odoo-ai run platform ...`.
- This is intentional for the current extraction phase because `odoo-ai`
  remains the self-contained runtime owner for fresh checkout and CI.
- For terminal use, extracted tenants should prefer `./scripts/workspace-sync`
  and `./scripts/workspace-status` so the manifest path stays anchored to the
  tenant repo root.

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
