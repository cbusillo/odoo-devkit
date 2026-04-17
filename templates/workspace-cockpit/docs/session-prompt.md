# Session Prompt Template

Use this as a starting prompt for a new multi-repo Every Code session from the
workspace root.

```text
You are working in the shared Odoo cockpit at the workspace root.

Start by reading:
- AGENTS.md in the workspace root
- docs/README.md in the workspace root

Repo map:
- sources/devkit -> odoo-devkit
- sources/shared-addons -> odoo-shared-addons
- sources/tenant-cm -> odoo-tenant-cm
- sources/tenant-opw -> odoo-tenant-opw
- sources/control-plane -> odoo-control-plane

Working rules:
- Treat repos under sources/ as the primary system under construction.
- Use odoo-devkit for shared DX/runtime/workspace behavior and local/data
  workflows.
- Use odoo-control-plane for remote release actions, deployment truth,
  release tuples, and promotion evidence.
- Stable remote lanes are testing and prod.
- Harbor PR previews replace any durable shared dev lane.
- Do not bring odoo-ai into the normal workspace context unless the task is
  explicit archaeology.
- Keep tenant repos thin and tenant-specific; fix shared behavior in devkit.
- When the workspace root and source repos disagree, treat the source repos
  as the source of truth.

When you change behavior, update the relevant source-repo docs in the same
slice.
```
