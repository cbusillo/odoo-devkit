# Tenant Docs

This docs index is intentionally thin.

## Start Here

- Use the workspace-root `docs/README.md` for the shared Every Code cockpit.
- Use `sources/devkit/docs/README.md` for shared DX/runtime/bootstrap docs.
- Use the generated tenant-root run configurations when you need to call the
  current runtime commands in the sibling `odoo-devkit` repo.
- Use `odoo-control-plane` for remote release actions such as ship, promote,
  and gate execution. Stable remote lanes are `testing` and `prod`; PR
  previews belong to Harbor preview workflows instead of a durable `dev` lane.
- Keep this tenant docs tree focused on tenant-owned domain workflows,
  architecture notes, and operational quirks.

## Typical Contents

- tenant architecture or domain notes
- tenant-specific local workflows
- tenant-specific integration notes
- tenant-specific release or rollout notes

## Useful Commands

- `./scripts/workspace-sync`
- `./scripts/workspace-status`
