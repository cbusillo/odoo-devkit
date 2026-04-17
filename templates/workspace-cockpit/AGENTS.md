# Workspace Cockpit

This workspace is the shared Every Code cockpit for multi-repo Odoo work.

- Start Every Code from this workspace root when the task spans multiple new
  repos.
- Treat the repos under `sources/` as the primary system under construction.

## Repo map

- `sources/devkit` -> `odoo-devkit`
- `sources/shared-addons` -> `odoo-shared-addons`
- `sources/tenant-cm` -> `odoo-tenant-cm`
- `sources/tenant-opw` -> `odoo-tenant-opw`
- `sources/control-plane` -> `odoo-control-plane`

## Upstream image repos

- `sources/odoo-docker` -> `odoo-docker`
- `sources/odoo-enterprise-docker` -> `odoo-enterprise-docker`

These are upstream runtime-contract repos, not new primary work centers.
Bring them into scope when a slice touches base image behavior, enterprise
layering, `/venv` ownership, addon path shaping, browser/devtools tooling, or
image publish/promotion mechanics.

## First reads

- Open [docs/README.md](docs/README.md) in this workspace root first.
- Use [sources/devkit/AGENTS.md](sources/devkit/AGENTS.md) for the canonical
  shared operating guide.
- Use [sources/devkit/docs/README.md](sources/devkit/docs/README.md) for the
  canonical shared docs index.
- Use the tenant-specific `workspace.toml` manifests when you need to run
  current local runtime commands through `odoo-devkit`.

## Ownership split

- `odoo-devkit` owns shared DX/runtime/workspace behavior plus local runtime
  and explicit data workflows.
- `odoo-control-plane` owns remote release actions, deployment truth, release
  tuples, and promotion evidence.
- Stable remote lanes are `testing` and `prod`.
- Harbor PR previews replace any durable shared `dev` lane.

## Notes

- This cockpit is intentionally manual today. It is not yet a generated
  `odoo-devkit` workspace shape.
- If this cockpit layout becomes the long-term answer, move the
  generator/schema support into `odoo-devkit` rather than hand-maintaining
  parallel behavior.
- Do not bring `odoo-ai` into the normal workspace context. If an explicit
  archaeology task still needs it, treat that as an external reference rather
  than part of the active repo map.
- Commit as you go when a coherent slice is verified; prefer small,
  reviewable commits over batching unrelated work until the end of the
  session.
