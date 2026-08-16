# Build-Tool Upgrades

## Ownership

`docker/runtime-python/pyproject.toml` is the devkit-owned catalog for Python
build tools that must be available at exact versions during tenant artifact
assembly. Its independent `uv.lock` records the approved artifacts. Tenant and
shared-addon projects retain exact `[build-system].requires` declarations so
the publish provenance check can prove that every requested backend is present
in either the support/runtime catalog or the tenant catalog.

Dependabot monitors `docker/runtime-python` separately from the devkit root
project. The `runtime-python-lock` CI job rejects catalog changes whose lock is
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
centrally managed addon pin drift, or `--apply` to update the tenant's
`workspace.toml` devkit/runtime refs and matching addon build-tool pins as one
transaction. Apply mode reparses every changed TOML file, verifies the exact
catalog contract, and runs the tenant's offline uv lock check. Any failure
restores every touched file.

The command owns deterministic file transformation and validation only.
Launchplane owns repository inventory, credentials, branch creation, pull
requests, merge ordering, retries, and audited rollout state.

## Rollout Order

1. Pin each tenant to its current known-good devkit commit.
2. Open and validate the devkit catalog/lock update.
3. Run the synchronization command against the devkit candidate commit to
   preview and validate the downstream changes.
4. Merge the devkit update and capture the resulting commit on `main`.
5. Rerun synchronization with that final commit, then merge the green tenant
   updates. Do not permanently pin a pull-request head that may be replaced by
   squash or rebase merge.

Tenant Dependabot may ignore centrally owned build tools only after the
runtime-python Dependabot lane, lock validation, and synchronization path are
operational. Security updates then originate from the central catalog and use
the same downstream rollout. Tenants should also run a scheduled sync check
against devkit `main` so a centrally available update cannot remain silent.

## Recovery

A failed tenant rollout leaves its tracked files unchanged. A merged tenant can
roll back by restoring its prior devkit commit and addon build-tool pins through
the same synchronization command. Never weaken the exact catalog check or use
a floating branch ref as a transition mechanism.
