# AGENTS.md — odoo-devkit Operating Guide

Treat this repo as the canonical home for the shared DX/runtime/bootstrap
contract. It owns the rules that should be shared across tenants and surfaced
into the generated workspace root.

## Start Here

- Read the owner's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
  first for priorities, stop boundaries, and retired concepts. This repository
  has no `DIRECTION.md` of its own.
- Use [docs/README.md](docs/README.md) as the shared docs index.
- Read [README.md](README.md) for the current bootstrap scope and command
  surface.
- Keep agent instructions in `AGENTS.md`, including tenant overlays and nested
  addon guides. Shared docs and the generated session prompt are references,
  not separate harness-specific instruction files.
- Keep the human-facing split clear:
  - PyCharm opens the tenant repo.
  - Codex and Claude Code start from the materialized workspace root.
  For Claude Code, start with `docs/session-prompt.md` and explicitly read the
  generated `AGENTS.md`; it is not loaded automatically.
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
- `odoo_devkit/pycharm_sources.py` owns pinned Odoo source attachment in the
  exact tenant's local PyCharm project.
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

## Test Rules

- A test stays only if it fails when the product is broken and passes when
  someone makes an intended change.
- No test may assert a literal defined elsewhere (versions, toolchain pins,
  hashes, image tags, generated guidance prose, template wording). Check
  agreement with the one source of truth, or test the behaviour that uses it.
- No test may assert workflow or config *text* (CI YAML, compose files,
  Dockerfiles, script source). Enforce the rule where it executes.
- Verification and loading code must not depend on the state of the working
  tree; check live state only on the path that acts on it. Tests must not skip
  or change outcome based on untracked local files.
- Byte-exact and hash checks are for real artifacts and immutable evidence only.

## Validation

- Use [`.github/github.json`](.github/github.json)
  for validation commands and quality gates.
- For workspace-surface changes, also run a live `workspace sync` against the
  current proof manifest and inspect the generated root files, including
  `docs/session-prompt.md`.

## Repository Work

These rules govern changes to `odoo-devkit` itself. When editing a tenant or
shared-addon repo, read that repo's `AGENTS.md` and direction for its own
ownership and landing path.

- Follow the shared [executing loop](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/executing-loop.md)
  with `github-plan` for issue ownership and claims and `github` for PRs and
  landing. Work in a linked task worktree, not the primary checkout.
- Use [reviews by another model](https://github.com/cbusillo/codex-skills/blob/HEAD/skills/references/model-review.md)
  for shared execution-guidance changes, including this file. Record and weigh
  findings under that reference.
- `.github/github.json` does not enable the Launchplane merge train for this
  repository. Land authorized changes through a PR with the required checks and
  code-scanning protection satisfied and a normal merge commit, using the
  configured automation identity.
