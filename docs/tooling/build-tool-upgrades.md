# Build-Tool Upgrades

## Ownership

`docker/runtime-python/pyproject.toml` is the devkit-owned catalog for Python
build tools that must be available at exact versions during tenant artifact
assembly. Its independent `uv.lock` records the approved artifacts. Tenant and
shared-addon projects retain exact `[build-system].requires` declarations so
the publish provenance check can prove that every requested backend is present
in either the support/runtime catalog or the tenant catalog.

Renovate updates `docker/runtime-python` along with the devkit root project
([Renovate](renovate.md)). The `runtime-python-lock` CI job rejects catalog changes whose lock is
not current.

## Tenant Synchronization

Plan a tenant update from a devkit checkout and an exact candidate commit:

```bash
uv run python -m odoo_devkit.build_tool_sync \
  --tenant-root ../odoo-tenant-opw \
  --devkit-root . \
  --devkit-ref <40-character-devkit-commit>
```

Add `--check` to exit nonzero when any synchronized value differs,
`--check-build-tools` to ignore unrelated devkit-ref movement and fail only on
centrally managed pin drift, or `--apply` to update the tenant's
`workspace.toml` devkit/runtime refs, matching addon build-tool pins and
support-owned lock versions as one transaction. Apply mode reparses every
changed TOML file, verifies the exact catalog contract, and runs the tenant's
offline uv lock check. Any failure restores every touched file.

The support lock and the tenant lock install into one interpreter, so a package
both install must have the support lock's exact requirement or the base runtime
dependency preflight rejects the build. The plan compares the same frozen
exports the preflight installs (no default groups, workspace members omitted),
so development-only packages are ignored and a different source at the same
version still counts. It reports each difference as a
`support-lock:<package>` change in `uv.lock`, and `--check-build-tools` fails
on it, so a runtime-python update a tenant has not taken fails that tenant's
CI on the Renovate pull request that moves its pin. `--apply` relocks only those packages to the
support versions and verifies that no difference remains. Regenerate any
tenant file exported from its lock, such as `tenant-requirements.txt`, with the
tenant's own export command before committing.

The command owns deterministic file transformation and validation only. It
opens no pull request.

## Moving The Pin

Each tenant's `workspace.toml` `[repos.devkit]` ref is its one devkit pin;
`[repos.runtime]` names the same commit. Renovate moves it: the
[devkit-pin preset](renovate.md), which each tenant's `renovate.json` extends,
tracks the head commit of devkit `main` and opens one pull request per tenant that
rewrites both refs to that exact commit. Until someone else pushes to its
branch, Renovate updates the same pull request when `main` moves again. Tenant
CI is the gate, and the merge train lands the pull request after an agent's
review, like any engineering change.

Renovate only rewrites the refs. Tenant CI runs `--check-build-tools` against
the pinned devkit, so a pin move that also needs addon build-tool or support
lock changes fails there; the reviewing agent runs `--apply` with the same
commit on that branch. Renovate then stops updating that branch, so the agent
owns it until it lands: land it at that commit, and Renovate proposes any later
`main` commit in a new pull request.

## Rollout Order

1. Open and validate the devkit catalog/lock update.
2. Run the synchronization command against the devkit candidate commit to
   preview and validate the downstream changes.
3. Merge the devkit update. Renovate then proposes the resulting `main` commit
   to each tenant; never pin a pull-request head that may be replaced by
   squash or rebase merge.

Tenant Renovate configs ignore centrally owned build tools such as Hatchling.
Security updates for them originate from the central catalog and use the same
downstream rollout.

## Recovery

A failed tenant rollout leaves its tracked files unchanged. A merged tenant can
roll back by restoring its prior devkit commit and addon build-tool pins through
the same synchronization command. Never weaken the exact catalog check or use
a floating branch ref as a transition mechanism.
