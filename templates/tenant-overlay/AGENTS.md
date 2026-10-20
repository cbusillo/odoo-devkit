# AGENTS.md — Tenant Overlay Guide

Treat this file as the thin tenant-specific overlay for a tenant repo.

Client: (fill in the person whose business this product serves)

## Start Here

- Read the owner's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
  first.
- Use the shared workspace-root `AGENTS.md` as the canonical coding-agent guide
  for Codex and Claude Code.
- Use `odoo-devkit` docs for shared DX/runtime/bootstrap behavior.
- Write Python and Odoo JavaScript in the shared
  [Python and Odoo style](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/python-odoo-style.md).
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
- Remote release actions belong in `launchplane`; keep this overlay
  focused on tenant-specific guidance and local/data-workflow entrypoints.

## Do Not Duplicate Here

- The full shared operating guide.
- Shared workspace/bootstrap instructions.
- Shared command patterns already owned by `odoo-devkit`.
