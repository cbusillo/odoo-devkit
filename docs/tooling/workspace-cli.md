# Workspace CLI

`odoo-devkit` owns the manifest-driven workspace command surface used to build
the Every Code cockpit and the local runtime assembly.

Runtime ownership is split by target type:

- manifest-local runtime targets run natively in `odoo-devkit` for
  `platform runtime select`, `build`, `publish`, `up`, `down`, `inspect`,
  `logs`, `psql`, `odoo-shell`,
  `restore`, and
  `platform runtime workflow --workflow bootstrap|init|update|openupgrade`.
- Dokploy-managed non-local data workflows run natively in `odoo-devkit`
  for `platform runtime restore` and
  `platform runtime workflow --workflow bootstrap|update`.
- Those non-local targets are the stable remote lanes (`testing`, `prod`). PR
  previews belong to Launchplane preview workflows in `launchplane`, not to
  `platform runtime` as another durable lane.
- non-local `platform runtime workflow --workflow init|openupgrade` remains
  local-only and fail early with a clear `--instance local` requirement.
- Release actions such as ship, promote, and gate execution belong in
  `launchplane`, not under `platform runtime`.

## Commands

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/repo --tenant opw
uv run platform workspace scaffold-cockpit-root \
  --output-dir /path/to/workspace-root
uv run platform workspace sync-cockpit-root \
  --config /path/to/workspace-root/workspace-cockpit.toml
uv run platform workspace status-cockpit-root \
  --config /path/to/workspace-root/workspace-cockpit.toml
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform runtime select --manifest /path/to/workspace.toml
uv run platform runtime build --manifest /path/to/workspace.toml --no-cache
uv run platform runtime publish --manifest /path/to/workspace.toml \
  --instance testing \
  --image-repository ghcr.io/example/odoo-opw \
  --image-tag opw-20260416-deadbeef \
  --output-file /tmp/opw-artifact.json
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
  - `docs/session-prompt.md`
- Generate PyCharm metadata plus run configurations.
- Emit `workspace.lock.toml` with the exact assembled local state.

## `workspace status`

Purpose

- Report whether the workspace exists.
- Report whether the lock file and workspace-root docs surface exist.
- Report the tenant/devkit/shared-addons source paths and attached IDE roots.

## `workspace scaffold-cockpit-root`

Purpose

- Copy the shared manual multi-repo cockpit-root starter into a target
  directory.
- Write `workspace-cockpit.toml` as the source of truth for that root.
- Keep the repo map and section-level cockpit guidance in that config instead
  of hand-maintaining root markdown files.
- Generate `AGENTS.md`, `docs/README.md`, and `docs/session-prompt.md` from
  that config.
- Keep non-repo workspace roots thin, link-heavy, and synced from
  `odoo-devkit` instead of hand-maintaining the same entrypoint docs.

## `workspace sync-cockpit-root`

Purpose

- Regenerate a manual multi-repo cockpit root from `workspace-cockpit.toml`.
- Keep root entrypoint docs manifest-driven even when the cockpit is not a
  tenant `workspace sync` surface.
- Re-render both repo listings and section-level guidance bullets from the
  tracked cockpit config.

## `workspace status-cockpit-root`

Purpose

- Report whether the generated cockpit entrypoint files exist.
- Report whether those files still match the current `workspace-cockpit.toml`
  render output.
- Give manual cockpit roots a native drift check before or after sync.

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
- Give a tenant repo a repeatable thin-root starting point.
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
- Shared-addons inputs may be path-based or repo-addressable. Managed
  shared-addons checkouts fail closed if the workspace copy is dirty or points
  at a different `origin` than the manifest declares.
- Keep the runtime repo explicit in the manifest. Tenant scaffolds point
  `[repos.runtime]` at the sibling `odoo-devkit` checkout so the same tracked
  manifest can keep `instance = "local"` by default while still targeting
  Dokploy-managed data restore/bootstrap/update flows through an explicit
  runtime `--instance` override.
- Keep the runtime repo explicit in the manifest for non-local targets because
  `odoo-devkit` may still need external runtime metadata from that repo or its
  managed `sources/runtime` checkout. Dokploy target definitions prefer the
  control-plane-owned `config/dokploy.toml` route catalog and
  `config/dokploy-targets.toml` target-id catalog resolved through
  `ODOO_CONTROL_PLANE_ROOT`.
- Runtime ownership remains fail-closed and explicit for non-local targets.
  `odoo-devkit` does not guess a runtime repo from `[repos.shared_addons]`,
  even if that path points at a sibling `odoo-shared-addons` checkout.
- Repo-addressable non-local runtime definitions fail closed until
  `platform workspace sync` has materialized `sources/runtime`.
- `platform runtime select` and `inspect` generate the PyCharm Odoo config from
  the manifest-backed tenant/shared addon sources.
- Local `platform runtime up` emits manifest-backed host addon mount
  paths for compose, so tenant checkouts can bind-mount `sources/tenant/addons`
  plus `sources/shared-addons` into the devkit-owned local runtime bundle.
- Local runtime selection reads typed `odoo_overrides` tables from the stack and
  renders the `ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64` payload consumed by
  `launchplane_settings`. `config_parameters` tables write Odoo
  `ir.config_parameter` keys, while `addon_settings.<addon>` tables write
  supported addon settings such as `authentik_sso` values.
- Legacy setting-shaped inputs such as `ENV_OVERRIDE_CONFIG_PARAM__*`,
  `ENV_OVERRIDE_AUTHENTIK__*`, and `ENV_OVERRIDE_SHOPIFY__*` are still accepted
  as a compatibility input and converted into the same typed payload, but they
  cannot be mixed with stack `odoo_overrides`. The checked-in sample stack uses
  typed `odoo_overrides` instead. Unrelated devkit control keys such as
  `ENV_OVERRIDE_DISABLE_CRON` remain available until they get their own typed
  local contract.
- When `ODOO_CONTROL_PLANE_ROOT` points at a valid `launchplane`
  checkout, local runtime env resolution comes from the control-plane-owned
  environment contract. Devkit-local `.env` / `platform/secrets.toml` runtime
  authority is unsupported. Leftover devkit-local env/secrets files are
  treated as a hard conflict so environment authority stays single-source, and
  build/restore requirements are expected to live in `launchplane`'s
  `config/runtime-environments.toml` surface.
- Native non-local ownership currently covers Dokploy-backed `restore`,
  `workflow bootstrap`, and `workflow update`; anything else should fail closed
  unless `odoo-devkit` grows an explicit remote contract for it.
- Release/deploy ownership for remote environments stays in
  `launchplane`, even when the same tenant manifest is used to anchor
  local runtime context.
- The runtime CLI accepts `--instance <name>` so a tenant repo can keep one
  tracked local-first manifest and still run remote data workflows like
  `platform runtime restore --manifest ./workspace.toml --instance testing`.
- Do not treat `--instance` as a general environment-expansion hook. The
  stable remote lane model is `testing` plus `prod`; preview runtime belongs
  in Launchplane preview records and generation workflows instead.
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
- `platform runtime publish` is the release-handoff path. It stages tenant and
  shared addon sources into a clean build context, resolves configured addon
  repository selectors to exact git SHAs before build and artifact minting,
  pushes the requested image tag, reads the pushed image digest from Buildx's
  build metadata output, and writes a control-plane-compatible artifact manifest
  JSON file.
- Publish-time GHCR credentials can be split by purpose. Private base image
  reads prefer `GHCR_READ_TOKEN`, artifact image pushes prefer `GHCR_TOKEN`,
  and private source checkout secrets still belong in the transient runtime
  payload as `GITHUB_TOKEN`. This lets CI use a repo-scoped package-write token
  for the tenant artifact while using a separate read token for shared private
  base images.
- When a repo-owned `artifact-inputs.toml` lives beside `workspace.toml`,
  `platform runtime` commands use it as the repo-owned source-input contract.
  Runtime and publish do not fall back to `stack.toml` source selector fields.
- Use [artifact-inputs.md](artifact-inputs.md) for the file schema and example
  shapes, including a non-Odoo example that keeps the contract repo-owned
  instead of runtime-specific.
- Runtime stack config should not declare source repository selectors.
  Repo-owned source selection belongs in the dedicated artifact-input manifest.
- Artifact manifests preserve selector intent in `addon_selectors` while
  keeping `addon_sources` as the resolved exact-SHA runtime truth consumed by
  control-plane release and deploy flows.
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
