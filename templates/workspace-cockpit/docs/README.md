# Workspace Docs

Use this workspace root when the session needs to reason about or edit
multiple durable repos at once.

## Primary repos

- Devkit: [../sources/devkit](../sources/devkit)
- Shared addons: [../sources/shared-addons](../sources/shared-addons)
- CM tenant: [../sources/tenant-cm](../sources/tenant-cm)
- OPW tenant: [../sources/tenant-opw](../sources/tenant-opw)
- Control plane: [../sources/control-plane](../sources/control-plane)

## Shared guides

- Shared operating guide: [../sources/devkit/AGENTS.md](../sources/devkit/AGENTS.md)
- Shared docs index: [../sources/devkit/docs/README.md](../sources/devkit/docs/README.md)
- Shared workspace CLI guide: [../sources/devkit/docs/tooling/workspace-cli.md](../sources/devkit/docs/tooling/workspace-cli.md)
- Shared command patterns: [../sources/devkit/docs/tooling/command-patterns.md](../sources/devkit/docs/tooling/command-patterns.md)

## Upstream image repos

- Public base image: [../sources/odoo-docker](../sources/odoo-docker)
- Private enterprise image layer: [../sources/odoo-enterprise-docker](../sources/odoo-enterprise-docker)

Use these when a slice touches image contracts, enterprise layering, `/venv`
ownership, addon path shaping, browser/devtools tooling, or image publish and
promotion behavior. They support the main system, but they are not the center
of gravity for normal tenant/control-plane/devkit work.

## External reference boundary

- Keep the main working set in the repos above.
- Do not include `odoo-ai` in the normal workspace flow. If an explicit
  archaeology task still needs it, treat that checkout as an external
  reference rather than part of the live repo map.

## Working split

- Use `odoo-devkit` for shared DX/runtime/workspace behavior and for local
  runtime plus explicit data workflows.
- Use `odoo-control-plane` for remote release actions, deployment truth,
  release tuples, and promotion evidence.
- Stable remote lanes are `testing` and `prod`.
- Harbor PR previews replace any durable shared `dev` lane.

## Operational notes

- This cockpit is manual today. If it should become a generated product
  surface, move that support into `odoo-devkit` rather than maintaining a
  parallel hand-built layer.
- Historical plans remain available under `/Users/cbusillo/.codex/plans/`
  when you need rationale or prior sequencing.

## Session prompt helper

- Use [session-prompt.md](session-prompt.md) as the starting prompt template
  for a new multi-repo Every Code session.
