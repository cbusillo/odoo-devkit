# Workspace CLI

`odoo-devkit` owns the manifest-driven workspace command surface used to build
the coding-agent workspace consumed by Every Code and Codex Lab, plus the local
runtime assembly.

Runtime ownership is split by target type:

- manifest-local runtime targets run natively in `odoo-devkit` for
  `platform runtime select`, `build`, `publish`, `up`, `down`, `inspect`,
  `logs`, `psql`, `odoo-shell`,
  `restore`, and
  `platform runtime workflow --workflow bootstrap|init|update|openupgrade`.
- Non-local restore/bootstrap/update, release, and preview lifecycle flow
  belongs in Launchplane. `platform runtime` fails closed for shared/testing/prod
  mutation instead of shelling into a sibling runtime checkout.
- non-local `platform runtime workflow --workflow init|openupgrade` remains
  local-only and fail early with a clear `--instance local` requirement.
- Release actions such as ship, promote, and gate execution belong in
  `launchplane`, not under `platform runtime`.

## Commands

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml --check
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
uv run platform dependencies inspect --manifest /path/to/workspace.toml
uv run platform dependencies check --manifest /path/to/workspace.toml
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

Non-local `platform runtime publish` is invoked by Launchplane's reusable
artifact workflow, which supplies the required runtime-environment payload.

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
- Generate the canonical workspace-root coding-agent surface:
  - `AGENTS.md`
  - `docs/README.md`
  - `docs/session-prompt.md`
- Generate PyCharm metadata plus run configurations.
- Emit `workspace.lock.toml` with the exact assembled local state.

## `workspace status`

Purpose

- Compare `AGENTS.md`, `docs/README.md`, and `docs/session-prompt.md` against the
  current deterministic render, reporting each surface as current, stale,
  missing, or disabled.
- Compare the manifest hash and typed source map against `workspace.lock.toml`.
- Report tenant, devkit, shared-addons, and distinct runtime source roles with
  workspace-relative entrypoints, resolved paths, materialization type, and
  editability.
- Report each source repository kind as `git` or `directory`; non-Git path edit
  roots omit unavailable Git baseline fields instead of producing false drift.
- Keep managed repository URLs out of generated files and status output;
  `workspace.lock.toml` records only their SHA-256 for contract comparison.
- Report source commit/branch/dirty changes as baseline drift. Drift on editable
  path-linked sources remains informational and does not make generated
  guidance stale; drift on managed read-only checkouts fails `--check`.
- Detect missing or repointed source links, invalid managed checkouts, and a
  reserved root `AGENTS.override.md` that would shadow the canonical guide.
- Reject a supplemental `workspace.local.md` that is a symlink or non-file so
  the normal agent flow cannot be redirected outside the workspace notes file.
- With `--check`, exit nonzero when the workspace, lock contract, manifest,
  generated guidance, source materialization, or override state is not current.
  Baseline drift on editable path-linked sources alone does not fail the check.

## `dependencies inspect` and `dependencies check`

Purpose

- Inspect tenant and shared-addon `pyproject.toml` files as one staged owned
  dependency workspace without moving shared-addon ownership into the tenant
  repository.
- Require every owned addon project to set `tool.uv.package = false`, retain an
  explicit exactly pinned build backend, and avoid `tool.uv.managed = false`,
  mutable VCS refs, local/archive references, or `requirements*.txt` fallback.
- When a tenant root `pyproject.toml` and `uv.lock` exist, require them as a
  complete pair, expand workspace members against the combined staged layout,
  and require the expanded members to exactly match all tenant and shared-addon
  projects. An explicit empty `members = []` is the exact valid set when the
  tenant and shared-addon trees contain no Python project metadata; pure-addon
  tenants do not need to invent a fake workspace member.
- Run `uv lock --check --offline --no-config` against that combined staged
  layout with operator `UV_*`/`PIP_*` overrides removed. Devkit does not parse
  uv's lock internals as a substitute for uv's own currentness decision.
- Treat publishable dependency metadata as Git-attributed input: root/member
  pyprojects and the tenant lock must be tracked regular files, symlinks and
  operator-local paths are rejected, and custom uv indexes/find-links cannot
  enter the staged contract.
- Allow a pure-addon workspace with no runtime Python dependency declarations
  to remain lockless and current for local development. Such a workspace is
  reported as `publishable = false` because Launchplane artifact schema v2
  requires both support/runtime and tenant lock evidence; devkit never invents
  a tenant lock that is absent from the tenant repository.
- A pure-addon tenant that publishes schema-v2 artifacts instead tracks a
  minimal root `pyproject.toml` with `tool.uv.package = false`, explicit empty
  workspace members, and its generated `uv.lock`. Those files provide exact
  tenant evidence without claiming runtime dependencies that do not exist.
- `inspect` prints structured JSON. `check` prints the same report and exits
  nonzero when `current` is false.

## `workspace scaffold-cockpit-root`

Purpose

- Copy the shared manual multi-repo cockpit-root starter into a target
  directory.
- Write `workspace-cockpit.toml` as the source of truth for that root.
- Keep the repo map and section-level cockpit guidance in that config instead
  of hand-maintaining root markdown files.
- Generate `AGENTS.md`, `docs/README.md`, and `docs/session-prompt.md` from
  that config.
- Point operators to optional `workspace.local.md` for supplemental non-secret
  facts that must not be baked into generated shared docs.
- Document `AGENTS.override.md` as a deliberate full replacement in Codex Lab,
  never the normal additive-notes path.
- Keep non-repo workspace roots thin, link-heavy, and synced from
  `odoo-devkit` instead of hand-maintaining the same entrypoint docs.

## `workspace sync-cockpit-root`

Purpose

- Regenerate a manual multi-repo cockpit root from `workspace-cockpit.toml`.
- Keep root entrypoint docs manifest-driven even when the cockpit is not a
  tenant `workspace sync` surface.
- Re-render both repo listings and section-level guidance bullets from the
  tracked cockpit config.
- Preserve local-only notes by linking to `workspace.local.md` instead of
  copying implementation details into generated markdown.

## `workspace status-cockpit-root`

Purpose

- Report whether the generated cockpit entrypoint files exist.
- Report whether those files still match the current `workspace-cockpit.toml`
  render output.
- Report the reserved root `AGENTS.override.md` and mark the cockpit non-current
  when it would replace the canonical generated guide.
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
- Resolve the runtime target from `workspace.toml` and execute supported local
  workflows natively against `odoo-devkit`, while leaving non-local mutation to
  Launchplane service routes and reusable workflows.

Local runtime input

- Before `select`, `inspect`, `build`, `up`, `down`, or a local workflow, set
  `ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON` from an operator-owned shell, password
  manager, or mode-`0600` file outside the repository.
- The payload must be a JSON object with the exact selected `context`, the exact
  selected `instance`, and a non-empty `environment` object containing only
  string keys and values. The checked-in stack declares which environment keys
  are required for the selected command.
- This is the only supported runtime-environment input path. Do not put runtime
  values in `workspace.toml`, generated workspace docs, checked-in config,
  `.env`, `platform/.env`, or `platform/secrets.toml`.
- `runtime inspect` reports selected runtime metadata and generated config paths;
  it does not print the payload or environment values.

Notes

- The tenant repo remains path-based and user-owned. `workspace sync` does not
  clone the active tenant checkout for you.
- Shared-addons inputs may be path-based or repo-addressable. Managed
  shared-addons checkouts fail closed if the workspace copy is dirty or points
  at a different `origin` than the manifest declares.
- Keep the runtime repo explicit in the manifest. Tenant scaffolds point
  `[repos.runtime]` at the sibling `odoo-devkit` checkout so the same tracked
  manifest can keep `instance = "local"` by default while artifact publish can
  still stage runtime inputs for Launchplane handoff.
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
- Checked-in stack `runtime_env` and `odoo_overrides` values are local-only.
  Active dev, testing, and production values or domains belong to Launchplane
  runtime-environment records.
- Non-local Launchplane-managed instances (`dev`, `testing`, and `prod`) always
  prepend `launchplane_settings` and `disable_odoo_online` to the resolved Odoo
  install module list. Artifact inputs or base images make addon files
  available, but this install list is what activates those modules in each
  database.
- When a tenant repo contains `website-bootstrap.toml` beside `workspace.toml`,
  runtime selection also folds that non-secret website intent into the same
  typed payload. The bootstrap contract can add install modules, provide the
  local canonical URL, identify a homepage page or controller route, and point
  at a repo-local logo asset. Shared/testing/prod canonical URLs are
  Launchplane-owned runtime records. Data workflows and startup apply bootstrap
  state idempotently after modules are installed, verify required public website
  identity fields before reporting success, and avoid hard-coded tenant
  defaults. Page-backed bootstrap also binds discovered `website.page` records,
  their website-specific views when available, and route readback markers to the
  selected website so post-deploy proof can distinguish payload rendering from
  public website identity persistence.
- Non-local Launchplane-managed runtimes can set
  `LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED=true` to require a valid typed
  override payload with managed settings before startup or data workflows
  continue. `LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED=true` additionally requires
  a non-empty `website_bootstrap` object in that payload. These flags are
  runtime assertions supplied by Launchplane-managed records or operator input;
  local runtimes remain optional unless a caller explicitly sets them.
- Legacy setting-shaped inputs such as `ENV_OVERRIDE_CONFIG_PARAM__*`,
  `ENV_OVERRIDE_AUTHENTIK__*`, and `ENV_OVERRIDE_SHOPIFY__*` are still accepted
  as a compatibility input and converted into the same typed payload, but they
  cannot be mixed with stack `odoo_overrides`. The checked-in sample stack uses
  typed `odoo_overrides` instead. Unrelated devkit control keys such as
  `ENV_OVERRIDE_DISABLE_CRON` remain available until they get their own typed
  local contract.
- Local runtime environment input comes only from
  `ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON`. Leftover devkit-local `.env`,
  `platform/.env`, or `platform/secrets.toml` files are a hard conflict so the
  runtime boundary stays single-source and fail-closed.
- Non-local `restore`, `workflow bootstrap`, and `workflow update` now fail
  closed with Launchplane handoff guidance. Devkit should not grow arbitrary
  checkout remote mutation flows; add or use a Launchplane service route first.
- Public and non-local Odoo runtimes fail closed on unsafe startup credentials:
  the master password must be present and non-default, and an explicit admin
  password must be configured before the startup wrapper marks the runtime
  usable. Local developer runtimes may omit the admin password, but previews,
  testing, and prod must not expose an Odoo database with default credentials.
- Devkit-managed startup and data workflow Odoo shell subprocesses prepend
  `/volumes/scripts` to `PYTHONPATH` so shipped runtime helpers remain
  importable from generated shell snippets.
- Release/deploy ownership for remote environments stays in
  `launchplane`, even when the same tenant manifest is used to anchor
  local runtime context.
- The runtime CLI accepts `--instance <name>` for selection and artifact
  context, but it is not a remote mutation hook. The stable remote lane model is
  `testing` plus `prod`; those mutations belong in Launchplane service routes
  and reusable workflows.
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
- `platform runtime publish` is the release-handoff path. It requires clean
  tenant/devkit/shared Git commits; stages only tracked regular files; hashes
  the exact support/runtime and tenant lock bytes; resolves configured addon
  selectors to exact Git SHAs; resolves both base images to immutable digests
  and verifies their OCI source/revision labels; then builds and pushes the
  requested artifact tag.
- After the push succeeds, publish reads the immutable artifact index digest
  from Buildx metadata, extracts each target platform's dependency sidecar from
  that digest, verifies the sidecars against the staged lock hashes and source
  commits, and writes Launchplane artifact-manifest schema v2. The manifest
  includes base-image/build-tool provenance, both uv locks, per-platform exact
  Python package inventories, and external compatibility descriptors.
- The artifact path uses `docker/artifact.Dockerfile`; local Compose continues
  to use `docker/Dockerfile`. Support lock evidence identifies
  `docker/runtime-python/uv.lock`, and nested shared-addon source markers keep
  installed shared distributions attributed to the shared repository commit.
- Publish does not mutate `/venv` after dependency evidence is written. An
  OpenUpgrade or other Python dependency must be represented by the locked
  support/tenant catalogs or by exact external compatibility evidence; an ad
  hoc post-sync install is not part of the artifact contract.
- `ODOO_PYTHON_SYNC_SKIP_ADDONS` is legacy-layout behavior and is rejected for
  schema-v2 publish because exporting the full workspace lock while skipping a
  member would make the evidence false. Remove the project from the tenant
  workspace instead.
- Non-local publish requires Launchplane to supply
  `ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON`. The payload is authoritative for
  artifact build runtime keys and deliberately excludes deployment-only
  database/master secrets. Publish enforces stack-required values only when
  those keys are part of the artifact-build input contract; local and remote
  runtime mutation commands retain the full stack required-key gate. The
  payload can synthesize a missing context or instance instead of requiring
  hosted lanes in the shared devkit stack. Synthesized contexts do not inherit
  stack-level install-module lists; their artifact install intent comes from
  managed-instance required modules plus any repo-owned
  `website-bootstrap.toml` modules. Unknown contexts and non-local instances
  fail closed without the explicit payload.
- Publish-time GHCR credentials can be split by purpose. Private base image
  reads prefer `GHCR_READ_TOKEN`, artifact image pushes prefer `GHCR_TOKEN`,
  and private source checkout secrets still belong in the transient runtime
  payload as `GITHUB_TOKEN`. For selector resolution before the build context
  exists, CI can also provide `ODOO_DEVKIT_SOURCE_GITHUB_TOKEN` or
  `ODOO_SOURCE_GITHUB_TOKEN`. This lets CI use a repo-scoped package-write
  token for the tenant artifact while using separate credentials for shared
  private source repositories and private base images.
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
  control-plane release and deploy flows. They also include the resolved
  `odoo_install_modules` list so promotion/deploy orchestration can preserve
  tenant module activation intent when it rewrites a live target environment.
- `platform runtime odoo-shell` follows the same local-only rule. It can run
  interactively, consume a `--script` file, and optionally tee output into a
  `--log-file`, but it is still a manifest-backed local helper rather than a
  generic remote exec path.

## Ownership Rules

- PyCharm should still open the tenant repo directly.
- Every Code and Codex Lab should start from the assembled workspace root.
- Generated workspace-root files are a cockpit layer; they are not the
  source-of-truth repo.
- If a generated file is wrong, change the generator in `odoo-devkit`.
