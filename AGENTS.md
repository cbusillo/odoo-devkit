# AGENTS.md — odoo-devkit Operating Guide

Treat this repo as the canonical home for the shared DX/runtime/bootstrap
contract. It owns the rules that should be shared across tenants and surfaced
into the generated workspace root.

## Start Here

- Use [docs/README.md](docs/README.md) as the shared docs index.
- Read [README.md](README.md) for the current bootstrap scope and command
  surface.
- Keep the human-facing split clear:
  - PyCharm opens the tenant repo.
  - Every Code starts from the materialized workspace root.
  - `odoo-devkit` owns the shared instructions and generators that make that
    split coherent.

## Scope

- `odoo_devkit/manifest.py` owns the tracked workspace manifest contract.
- `odoo_devkit/workspace.py` owns workspace materialization and status/clean/run
  behavior.
- `odoo_devkit/workspace_surface.py` owns the generated workspace-root
  `AGENTS.md` and `docs/README.md` surface.
- `odoo_devkit/pycharm.py` owns PyCharm metadata and run configuration
  generation.
- `odoo_devkit/ide_support.py` owns the pure PyCharm Odoo-conf rendering logic
  shared with tenant repos.
- `tests/` should validate the workspace contract as a user-facing system, not
  just file existence.

## Shared Contract

- Keep tenant repos thin: tenant-specific `workspace.toml`, tenant-specific
  docs, and brief local instructions.
- Keep the shared operating guide, shared docs routing, and workspace generator
  behavior here instead of duplicating them into every tenant repo.
- Prefer explicit generated files over implicit conventions. If the workspace
  root needs a guide or index, generate it here.
- Keep source-of-truth ownership explicit:
  - tenant code belongs in the tenant repo
  - shared addons belong in the shared-addons repo
  - shared DX/runtime guidance belongs here
  - workspace root files are generated cockpit files, not the canonical repo

## Guardrails

- Fix generators, not generated output.
- Keep the assembled workspace rebuildable and safe to delete.
- Do not let secrets migrate into tracked manifests, checked-in templates, or
  generated docs examples.
- Keep the private Enterprise layer generic in public docs, templates, and
  examples.
- When behavior changes, update the shared docs here in the same change so the
  workspace-root surface stays honest.

## Validation

- Run `uv run python -m unittest discover -s tests` for functional coverage.
- Run `uv run ruff format --check .` and `uv run ruff check .` before closing a
  substantial change.
- For workspace-surface changes, also run a live `workspace sync` against the
  current proof manifest and inspect the generated root files, including
  `docs/session-prompt.md`.
