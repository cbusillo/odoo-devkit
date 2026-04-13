# AGENTS.md — Tenant Overlay Guide

Treat this file as the thin tenant-specific overlay for a tenant repo.

## Start Here

- Use the shared workspace-root `AGENTS.md` as the main Every Code operating
  guide.
- Use `odoo-devkit` docs for shared DX/runtime/bootstrap behavior.
- Keep this file focused on tenant-specific domain notes, repo-local guardrails,
  and links to tenant-owned docs.

## Tenant Scope

- Tenant-specific addons and workflows.
- Tenant-specific docs and domain notes.
- Tracked `workspace.toml` defaults for local DX.
- Tenant-root run configurations and shell helpers that call the sibling
  `odoo-devkit` repo while shared-addon source stays explicit in the manifest.
- Convenience shell commands under `scripts/` for workspace sync/status from
  the tenant repo root.

## Do Not Duplicate Here

- The full shared operating guide.
- Shared workspace/bootstrap instructions.
- Shared command patterns already owned by `odoo-devkit`.
