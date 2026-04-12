# Workspace CLI

`odoo-devkit` owns the manifest-driven workspace command surface used to build
the Every Code cockpit and the local runtime assembly.

Native runtime ownership is now split by target type instead of by command
name:

- manifest-local runtime targets run natively in `odoo-devkit` for
  `platform runtime select`, `build`, `up`, `down`, `inspect`, `logs`, `psql`, `odoo-shell`,
  `restore`, and
  `platform runtime workflow --workflow bootstrap|init|update|openupgrade`.
- Dokploy-managed non-local runtime targets now run natively in
  `odoo-devkit` for `platform runtime restore` and
  `platform runtime workflow --workflow bootstrap|update`.
- non-local `platform runtime workflow --workflow init|openupgrade` remain
  local-only and fail early with a clear `--instance local` requirement.

## Commands

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/repo --tenant opw
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform runtime select --manifest /path/to/workspace.toml
uv run platform runtime build --manifest /path/to/workspace.toml --no-cache
uv run platform runtime up --manifest /path/to/workspace.toml --build
uv run platform runtime down --manifest /path/to/workspace.toml --volumes
uv run platform runtime workflow --manifest /path/to/workspace.toml --workflow update
uv run platform runtime restore --manifest /path/to/workspace.toml
uv run platform runtime inspect --manifest /path/to/workspace.toml
uv run platform runtime logs --manifest /path/to/workspace.toml --service web --no-follow
uv run platform runtime psql --manifest /path/to/workspace.toml -- -c 'select 1'
uv run platform runtime odoo-shell --manifest /path/to/workspace.toml \
  --script tmp/scripts/example.py
```

If `--manifest` is omitted, the command looks for `workspace.toml` in the
current directory.

## `workspace sync`

Purpose

- Materialize the tenant, devkit, and optional shared-addons sources under the
  workspace root.
- When `[repos.shared_addons]` declares `url` + `ref` instead of `path`, clone
  or refresh a managed checkout at `sources/shared-addons`.
- When `[repos.runtime]` declares `url` + `ref` instead of `path`, clone or
  refresh a managed checkout at `sources/runtime` for non-local runtime
  targets.
- Generate runtime config under `.generated/`.
- Generate the workspace-root Every Code surface:
  - `AGENTS.md`
  - `docs/README.md`
- Generate PyCharm metadata plus run configurations.
- Emit `workspace.lock.toml` with the exact assembled local state.

## `workspace status`

Purpose

- Report whether the workspace exists.
- Report whether the lock file and workspace-root docs surface exist.
- Report the tenant/devkit/shared-addons source paths and attached IDE roots.

## `workspace clean`

Purpose

- Remove the assembled workspace so it can be recreated from source repos and
  trusted local inputs.

## `workspace run`

Purpose

- Run an arbitrary command with the workspace root as the current directory.

## `workspace scaffold-tenant-overlay`

Purpose

- Copy the thin tenant-overlay starter files into a target repo directory.
- Stamp the tenant slug into the starter `workspace.toml`.
- Give the first extracted tenant repo a repeatable thin-root starting point.
- Keep the starter aligned with the flat tenant-addon rule: tenant-owned addons
  live directly under `addons/`, while shared addons stay external via
  `[repos.shared_addons]`.

## `runtime ...`

Purpose

- Expose the local runtime command surface through `odoo-devkit` so tenant
  overlays and generated PyCharm run configurations do not need to call a
  sibling runtime repo directly.
- Resolve the runtime target from `workspace.toml` and execute the supported
  local or Dokploy-backed workflow natively against the repo declared in
  `[repos.runtime]`.

Notes

- The tenant repo remains path-based and user-owned. `workspace sync` does not
  clone the active tenant checkout for you.
- Shared-addons inputs may now be path-based or repo-addressable. Managed
  shared-addons checkouts fail closed if the workspace copy is dirty or points
  at a different `origin` than the manifest declares.
- Keep the runtime repo explicit in the manifest. Extracted tenant scaffolds
  now point `[repos.runtime]` at the sibling `odoo-devkit` checkout so the
  same tracked manifest can keep `instance = "local"` by default while still
  targeting Dokploy-managed restore/bootstrap/update flows through an explicit
  runtime `--instance` override.
- Keep the runtime repo explicit in the manifest for non-local targets because
  `odoo-devkit` may still need external runtime metadata from that repo or its
  managed `sources/runtime` checkout. Dokploy target definitions now prefer the
  control-plane-owned `config/dokploy.toml` catalog when
  `ODOO_CONTROL_PLANE_ROOT` is set, with the runtime repo's
  `platform/dokploy.toml` used only when the control-plane catalog is absent.
- Runtime ownership remains fail-closed and explicit for non-local targets.
  `odoo-devkit` no longer guesses a runtime repo from `[repos.shared_addons]`,
  even if that path points at a sibling `odoo-shared-addons` checkout.
- Repo-addressable non-local runtime definitions fail closed until
  `platform workspace sync` has materialized `sources/runtime`.
- For extracted tenant overlays, `platform runtime select` and `inspect`
  generate the PyCharm Odoo config from the manifest-backed tenant/shared addon
  sources rather than from the runtime repo's older project-addon layout.
- Local `platform runtime up` now also emits manifest-backed host addon mount
  paths for compose, so tenant checkouts can bind-mount `sources/tenant/addons`
  plus `sources/shared-addons` into the devkit-owned local runtime bundle.
- When `ODOO_CONTROL_PLANE_ROOT` points at a valid `odoo-control-plane`
  checkout, local runtime env resolution comes from the control-plane-owned
  environment contract. Devkit-local `.env` / `platform/secrets.toml` runtime
  authority is no longer supported. Leftover devkit-local env/secrets files are
  treated as a hard conflict so environment authority stays single-source, and
  build/restore requirements are expected to live in `odoo-control-plane`'s
  `config/runtime-environments.toml` surface.
- Native non-local ownership currently covers Dokploy-backed `restore`,
  `workflow bootstrap`, and `workflow update`; anything else should fail closed
  unless `odoo-devkit` grows an explicit remote contract for it.
- The runtime CLI accepts `--instance <name>` so a tenant repo can keep one
  tracked local-first manifest and still run remote data workflows like
  `platform runtime restore --manifest ./workspace.toml --instance testing`.
- `platform runtime logs` and `platform runtime psql` are intentionally
  local-only helpers for manifest-backed debugging. They require
  `--instance local` and fail closed for non-local targets instead of falling
  through to an implicit remote path.
- `platform runtime down` follows the same local-only rule and gives tenant
  manifests a native way to stop the local compose stack without routing
  through another repo.
- `platform runtime build` follows the same local-only rule and gives tenant
  manifests a native build-only entry point when operators want image prep
  without starting the stack.
- `platform runtime odoo-shell` follows the same local-only rule. It can run
  interactively, consume a `--script` file, and optionally tee output into a
  `--log-file`, but it is still a manifest-backed local helper rather than a
  generic remote exec path.

## Ownership Rules

- PyCharm should still open the tenant repo directly.
- Every Code should start from the assembled workspace root.
- Generated workspace-root files are a cockpit layer; they are not the
  source-of-truth repo.
- If a generated file is wrong, change the generator in `odoo-devkit`.
