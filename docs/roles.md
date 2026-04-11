---
title: Shared Roles
---

Purpose

- Define role expectations and outputs for Codex work that applies across
  tenant repos and the generated workspace root.

When

- At the start of a task to set expectations and outputs.

## Analyst

Purpose: find patterns, constraints, and authoritative guidance; return concise
evidence.

Inputs -> Outputs

- Inputs: brief, plan handles, repo/file targets
- Outputs: decision, supporting evidence, risks, next implementation slice

Notes

- Prefer handles over pasted excerpts.
- Distinguish shared-contract guidance from tenant-only behavior.

## Engineer

Purpose: apply focused changes that improve the shared workspace/runtime
contract or the tenant overlay without blurring ownership.

Inputs -> Outputs

- Inputs: intended file set, contract boundary, acceptance target
- Outputs: diffs, validation results, remaining risks

Notes

- Fix generators rather than generated output.
- Keep tenant repos thin and shared guidance centralized in `odoo-devkit`.

## Tester

Purpose: validate the changed surface with the fastest credible loop first,
then the broader proof as needed.

Inputs -> Outputs

- Inputs: changed modules, manifests, commands
- Outputs: test results, live validation notes, residual gaps

Notes

- For workspace-surface changes, validate both unit tests and a live
  `workspace sync`.

## Reviewer

Purpose: catch drift between the shared contract, generated workspace surface,
and tenant overlays.

Inputs -> Outputs

- Inputs: changed files, generated outputs, docs touched
- Outputs: findings with file references, resolution notes, follow-up risks

Notes

- Favor ownership and behavioral regressions over style-only commentary.

## Maintainer

Purpose: keep docs, workspace contracts, and worktree hygiene coherent during
the repo pivot.

Checklist

- Shared docs in `odoo-devkit` still match generated workspace output.
- Tenant overlays stay thin and tenant-specific.
- Stale worktrees and abandoned bootstrap experiments are pruned.
