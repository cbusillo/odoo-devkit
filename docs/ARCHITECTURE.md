---
title: Workspace Architecture
---

Purpose

- Capture the workspace-first architecture for extracted tenant workspaces.
- Make the ownership split explicit between the tenant repo, `odoo-devkit`,
  the materialized workspace root, and the control plane.

When

- When onboarding or deciding where a workspace/runtime change belongs.

## Core Shape

- PyCharm opens the tenant repo directly.
- Every Code starts from the materialized workspace root.
- `odoo-devkit` owns the shared DX/runtime/bootstrap contract.
- The control plane owns canonical deploy/build tuples and release-sensitive
  behavior.

## Ownership Boundaries

### Tenant repo

- Hand-edited tenant code.
- Tenant-specific docs and domain notes.
- Tracked `workspace.toml` input for local DX defaults.
- Thin repo-root instructions only.
- Use `templates/tenant-overlay/` as the starting shape for extracted tenant
  repos.

### `odoo-devkit`

- Manifest parsing and workspace assembly.
- Generated workspace-root surface for Every Code.
- Shared AGENTS/docs/runtime guidance.
- Generated PyCharm metadata and run configurations for the tenant repo.
- Devkit-owned local runtime bundle (`docker-compose.yml`, `platform/stack.toml`,
  Dockerfile, and local runtime scripts).

### Materialized workspace root

- Generated cockpit for Every Code.
- Materialized `sources/tenant`, `sources/devkit`, and optional
  `sources/shared-addons`.
- Generated runtime output under `.generated/`.
- Disposable local state under `state/`.

### Control plane

- Exact compatible refs and artifact identity.
- Ship/promote/gate workflows and fail-closed release rules.
- Operator-facing deployment state.

## Working Rule

- If the issue is about shared guidance, workspace generation, or local DX
  contract, it belongs in `odoo-devkit`.
- If the issue is tenant business logic or tenant-specific workflow guidance,
  it belongs in the tenant repo.
- If the issue is release-sensitive tuple resolution or promotion safety, it
  belongs in the control plane.

## Materialized Workspace Layout

```text
~/Developer/odoo-workspaces/<tenant>/
  AGENTS.md
  docs/
    README.md
  workspace.lock.toml
  .generated/
  sources/
    tenant/
    devkit/
    shared-addons/
  state/
```

## Design Goal

- Keep the IDE focused.
- Keep the Every Code cockpit shared.
- Keep source-of-truth ownership explicit.
- Keep the workspace rebuildable.
