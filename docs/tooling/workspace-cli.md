# Workspace CLI

`odoo-devkit` owns the manifest-driven workspace command surface used to build
the coding-agent workspace consumed by Codex and Claude Code, plus the local
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
  local-only and fails with a clear `--instance local` requirement once the
  runtime repo resolves.
- Release actions such as ship, promote, and gate execution belong in
  `launchplane`, not under `platform runtime`.

## Commands

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace prepare-ide --manifest /path/to/tenant/workspace.toml \
  --odoo-source /path/to/odoo-19 --odoo-series 19.0 --odoo-commit <full-source-sha>
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml --check
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/repo --tenant opw
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform dependencies inspect --manifest /path/to/workspace.toml
uv run platform dependencies check --manifest /path/to/workspace.toml
uv run platform dependencies normalize --manifest /path/to/workspace.toml
uv run platform dependencies normalize --manifest /path/to/workspace.toml \
  --output-dir /path/to/normalization-output
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

Optional IDE run configurations use `[[ide.run_configurations]]` tables.
Omitting them or setting `run_configurations = []` under `[ide]` defines none;
other value types and non-table entries are rejected when the manifest loads.

Non-local `platform runtime publish` is invoked by the product's own build
workflow, which supplies the required runtime-environment payload.

Hosted startup honors `ODOO_PROXY_MODE` in its generated Odoo configuration.
Set it to `True` through the lane's managed runtime settings when Odoo sits
behind a trusted reverse proxy that supplies forwarded headers. Set it to
`False` to disable proxy mode explicitly. An absent or blank value preserves
the base configuration; without a base setting, Odoo's disabled default applies.
Changing `web.base.url` alone does not change the scheme of incoming requests
used to build OAuth callbacks.

## `workspace sync`

Purpose

- Materialize the tenant, devkit, and optional shared-addons sources under the
  workspace root.
- When `[repos.shared_addons]` declares `url` + `ref` instead of `path`, clone
  or refresh a managed checkout at `sources/shared-addons`.
- When `[repos.runtime]` declares `url` + `ref` instead of `path`, clone or
  refresh a managed checkout at `sources/runtime`; a `path` is linked there
  instead.
- Generate runtime config under `.generated/`.
- Generate the canonical workspace-root coding-agent surface:
  - `AGENTS.md`
  - `docs/README.md`
  - `docs/session-prompt.md`
- Generate PyCharm metadata plus run configurations.
- Emit `workspace.lock.toml` with the exact assembled local state.

## `workspace prepare-ide`

Prepare the exact tenant checkout before opening it in PyCharm or running a
JetBrains inspection. The Odoo plugin needs the matching community core and
addons visible as project content roots to resolve references such as
`website.snippets`. Listing paths in the workspace metadata alone does not
attach them to the IDE.

1. Obtain an official [Odoo community checkout](https://github.com/odoo/odoo)
   outside the tenant repository, using the series required by the runtime.
   Keep it in the host's shared dependency cache and pin a full commit SHA.
2. Close only the exact tenant project being prepared, or prepare it before its
   first open. Do not run the generator while that project has an active IDE
   writer or inspection. Other worktrees can keep their own projects open.
3. Run `workspace prepare-ide` with that tenant worktree's `workspace.toml`,
   `--odoo-source`, `--odoo-series`, and `--odoo-commit`. Keep machine-local
   paths in local invocation/configuration, outside tracked tenant files.
4. Open and inspect the tenant worktree through the normal inspection helper.
   Core sources supply resolution context; keep the inspection scope on the
   tenant files. A successful preparation is setup evidence, not an inspection
   verdict or a runtime update.

The command checks the source checkout's commit, clean Git state, core/addon
layout, and release series without importing Odoo or changing the dependency.
It adds the content root to the Python module rooted at the exact tenant, or
creates a minimal project using the tenant's `pyproject.toml` project name when
no module exists. Existing SDK assignments, content roots, module
dependencies, inspection profiles, and other project state remain in place.
This implements the plugin author's supported
[content-root setup](https://github.com/odoo-ide/pycharm-odoo/wiki/Structure-Odoo-Projects).

Only already ignored, untracked IDE metadata may be changed. Tracked metadata,
ambiguous tenant modules, malformed paths/XML, a different existing Odoo content
root on the tenant module,
or symlinked project metadata require local reconciliation first. The generator
does not edit ignore policy or overwrite another project's configuration.
Paths may use `$PROJECT_DIR$`, `$MODULE_DIR$`, or `$USER_HOME$`. Custom IDE path
variables are resolved only when
`--ide-path-variables /path/to/selected-ide-config/options/path.macros.xml` is
supplied. Use the configuration directory of the IDE instance that will open
this exact project (Help > Diagnostic Tools > Special Files and Folders). The
file is JetBrains' native `application` / `PathMacrosImpl` macro table, saved
from Settings > Appearance & Behavior > Path Variables. Path variables apply to
every project in that IDE instance; see
[JetBrains' path-variable documentation](https://www.jetbrains.com/help/pycharm/absolute-path-variables.html).
The command reads this explicit file without discovering global configuration,
changing IDE settings, or taking definitions from environment variables. Do not
commit it or its machine-local values. Save variable changes in the selected IDE
before preparing a closed project. Names must be unique and must not override
the three built-ins; values must be absolute paths without nested macros.
Missing definitions (including in unrelated local modules), duplicate definitions,
malformed tables, and conflicting tenant Odoo sources fail before any metadata
write. Existing custom macro URLs are preserved, so preparing a saved
`file://$ODOO_SRC$` root again adds no duplicate. Interpreter libraries and other
modules' content roots remain outside this command's source-attachment check.
Repeated preparation with the same source and supported paths is a no-op. JSON output records the
exact project/module paths, source path, commit, series, and changed files.

This command works without workspace materialization, Docker, runtime secrets,
an installed Odoo interpreter, or tenant code changes. It can serve as the
repository's configured inspection preparation command when its caller supplies
the local devkit/source paths and pin. Normal workspace sync continues to own
the generated cockpit and run configurations; it does not change IDE roots.

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
- Evaluate member patterns against the real tenant/shared addon directory
  shape, not only copied dependency metadata. A pattern may not match an addon
  directory without `pyproject.toml`; use explicit member paths instead of a
  broad glob over mixed Python-project and ordinary Odoo addon directories.
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

## `dependencies normalize`

Purpose

- Regenerate the canonical tenant root `uv.lock` inside the same staged
  tenant/shared-addon workspace used by dependency inspection, then atomically
  copy only the verified lock back to the tenant repo.
- Preserve strict tenant CI: normalization proceeds only when the workspace is
  already publishable or stale lock state is its sole finding, and the written
  lock must pass the existing offline, no-config publishability check.
- Run `uv lock --python <manifest-python> --no-config` without broad upgrade
  flags. Pinning the manifest's Python version keeps canonical lock generation
  stable across hosts while the existing lock continues to constrain
  unaffected packages.
- Produce the frozen export shape used by tenant CI with `uv export --frozen
  --all-packages --no-emit-workspace --no-default-groups --no-config`. The
  export is verified and hashed on every run; pass `--output-dir` to retain it
  as `tenant-requirements.txt`.
- Emit sorted JSON with source input hashes, tenant/shared source commits and
  dirty-state flags, uv version and arguments, final artifact hashes, strict
  post-check results, and `changed = false` for a measured no-change run.
  Output omits credentials, repository URLs, and machine-specific source paths.
- Restore the original tenant `uv.lock` if the strict post-check or retained
  export write fails. Tenant manifests, addon metadata, shared-addon sources,
  and CI workflows remain owned by their existing repositories.

### Image-owned Python versions

When `[build].base_runtime_image` is configured, normalization also resolves
that published image to an immutable digest and reads its baked
`/opt/launchplane/base-python-constraints.txt` for both supported Linux
architectures. It never reconstructs this catalog from a tenant image.
Missing catalogs and conflicting tenant requirements fail with the base digest
and resolver's package details.

The shared staging resolver merges all base entries into root
`tool.uv.constraint-dependencies`, retaining tenant constraints and addon
requirements. It starts from each source lock and runs ordinary `uv lock`
without broad upgrades. Generated constraints stay in staging. The canonical
source lock still describes the source pyproject; it need not change when a
compatible base package moves. Frozen exports and installations use the
derived lock, including support/runtime and addon-test catalogs.

Canonical `dependencies normalize`, including `--output-dir`, stays registry
independent and retains its canonical frozen export and hash contract. It does
not qualify the base or add generated constraints to source metadata.

For a host environment, use `dependencies prepare --output-dir <empty-dir>`
with the workspace manifest, then install the staged workspace:

```bash
uv sync --frozen --all-packages --no-default-groups --project <empty-dir>
```

Preparation reads the published base and retains the complete derived metadata
workspace and frozen export. `--all-packages` includes addon member requirements
when the root itself declares none. Constraints apply to declared host overlaps;
platform markers retain Linux-only requirements outside a macOS environment.
Source-only `dependencies inspect/check` remain offline source contract checks.

Local `runtime build` uses artifact staging in working-tree mode for
`docker/Dockerfile`. Its ignored `.platform/dependencies` state and per-runtime
Compose overlay keep four derived metadata files mounted during local use. Full
build contexts are temporary and a failed build preserves the current overlay.
Local edits to tracked metadata remain allowed; add new metadata to Git and
normalize before staging. Nested addon repositories use declared shared/external
source routes. The reserved top-level `addons/shared` comes only from the
declared shared checkout; nested directories named `shared` stay included.
Ignored files stay excluded, and `local/<role>` Git
markers identify the baseline without requiring a remote or claiming a release
identity. Addon CI derives temporary locks from the exact published image it
builds FROM. Artifact builds use the same resolver with immutable committed
source snapshots and the exact resolved base digest for every target platform.
Different architecture catalogs receive architecture markers in the universal
lock. Catalog extraction may download both platform images, even for a single
platform build; it uses the existing Docker cache. No checked-in base package
list is authoritative. Local builds require manifest `[build]` base inputs,
just as artifact builds do.

The local build generates its Compose build definition, so a fresh devkit
worktree needs no operator-local `docker-compose.override.yml`. External addon
and OpenUpgrade selectors resolve to exact commits through the publish resolver
before Docker runs. Resolution and BuildKit source fetching use the same
transient source token: configured `GITHUB_TOKEN`, then the source-token
environment variables described below, then shell `GITHUB_TOKEN`/`GH_TOKEN`,
then the existing local `gh auth token` lookup. Registry credentials are not
source credentials. The token goes to BuildKit as a secret, outside build arguments and
generated build metadata. Tenant Python dependencies use the staged derived
locks above; no separate local installer or hand-pinned source file is needed.
Local bootstrap, restore, update, init and OpenUpgrade use the existing image
and derived-lock overlay without rebuilding, so retrying a failed operation does not
require source or registry access. Before first use or after dependency/image
changes, run `platform runtime build` or `platform runtime up --build` with
the same manifest. A missing image fails instead of triggering an implicit
build from an operator override.

Local OpenUpgrade builds use the same strict frozen dependency install. Declare
`openupgradelib` in owned addon metadata (including an exact VCS source when
needed), run canonical normalization, and rebuild. OpenUpgrade source fetching
and execution remain available. Remove explicit `OPENUPGRADELIB_INSTALL_SPEC`
and `ODOO_PYTHON_SYNC_SKIP_ADDONS` inputs: strict local builds reject these legacy
overrides instead of running an installer after dependency verification.

The layered installer's inherited-package refusal and final environment
validation remain independent checks. Generated constraints are ordinary
constraints, never overrides; they cannot make an explicit incompatible
tenant or addon requirement acceptable.

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

- Before any local `platform runtime` command (`select`, `inspect`, `build`,
  `up`, `down`, `restore`, `logs`, `psql`, `odoo-shell`, or a workflow), set
  `ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON` from an operator-owned shell, password
  manager, or mode-`0600` file outside the repository.
- The payload must be a JSON object with the exact selected `context`, the exact
  selected `instance`, and a non-empty `environment` object containing only
  string keys and values. The checked-in stack declares which environment keys
  are required for the selected command.
- Generated runtime and Compose env files encode literal values, including dollar
  signs, comment markers, quotes, backslashes and surrounding whitespace. Values
  are not shell expressions; do not source these files. Re-run `runtime select`
  to regenerate an environment file written by an older devkit. Password values
  remain in the generated files, not command arguments or diagnostic output.
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
- Startup carries its resolved addon paths into the image wrapper environment,
  keeping packaged shared addon groups visible to initialization, web and the scheduler.
  After initialization it reads back the requested installed modules and stops
  before serving if Odoo ignored a missing addon. Correct the declared addon
  inputs or paths, then run the same startup command again.
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
- When a tenant repo contains `website-bootstrap.toml` (at the tenant repo root,
  or beside `workspace.toml`),
  runtime selection also folds that non-secret website intent into the same
  typed payload. The bootstrap contract can add install modules, provide the
  local canonical URL, identify a homepage page or controller route, and point
  at a repo-local logo asset. Keep only the local canonical URL there;
  testing/prod canonical URLs are Launchplane-owned runtime records, though the
  file format does not reject other instance keys. Data workflows and startup apply bootstrap
  state idempotently after modules are installed, verify required public website
  identity fields before reporting success, and avoid hard-coded tenant
  defaults. The shared settings consumer supports native string parameters on
  Odoo 19 and 20, preserving empty values and literal boolean strings. Bootstrap
  reuses the native default website in either version and compares encoded logo
  content through the native attachment interface. Nonproduction analytics and
  indexing exclusion still runs without an optional settings payload, including
  while the loaded website addon is scheduled for upgrade or removal. A base-only
  website model does not enable website settings or bootstrap on its own.
  Page-backed bootstrap also binds discovered `website.page` records,
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
- Local restore, bootstrap, and update forward opaque values literally from
  that input, including passwords, `ODOO_KEY`, and `ENV_OVERRIDE_*` values.
  Dollar expressions, tilde prefixes, quotes, backslashes, and surrounding
  spaces are preserved. Supply actual values; a non-path alias such as
  `ENV_OVERRIDE_COPY=${ODOO_DB_PASSWORD}` stays that literal string.
  Opaque values travel through the process environment, keeping them out of command
  arguments and command-failure diagnostics.
- Only these local data-workflow path fields expand home prefixes and
  `$NAME`, `${NAME}`, and `${NAME:-default}` references:
  `DATA_WORKFLOW_SSH_DIR`, `DATA_WORKFLOW_SSH_KEY`, `ODOO_FILESTORE_PATH`,
  `ODOO_UPSTREAM_FILESTORE_PATH`, `ODOO_DATA_WORKFLOW_LOCK_FILE`,
  `OPENUPGRADE_SCRIPTS_PATH`, `ODOO_ADDONS_PATH`, and `LOCAL_ADDONS_DIRS`.
  References prefer the supplied input,
  then the host environment, then the default for an absent variable.
  References to other path fields resolve recursively; cycles fail with a
  value-free error. Host forwarding never expands substituted values again.
  Unknown references without defaults remain literal. Do not place credentials
  in paths: paths may appear in filesystem commands. This contract covers host
  forwarding; downstream whitespace and path normalization are separate.
  `DATA_WORKFLOW_SSH_DIR` also supplies the two read-only Compose SSH bind mounts.
  Devkit resolves it with the same path rules before Compose starts, so home,
  variable, default, and recursive path references agree with workflow forwarding.
  It must resolve to an absolute host path; empty or relative paths fail before
  any Compose command. The original runtime env file and opaque values remain
  literal. A failed script-runner start stops the workflow before filestore
  preparation or stopping web; it cannot continue using an older runner.
  This input check applies to all devkit runtime Compose commands, including
  build, down, and logs. Correct the supplied SSH directory and rerun runtime
  select to refresh an existing env file before retrying those commands.
- Non-local `restore`, `workflow bootstrap`, and `workflow update` now fail
  closed with Launchplane handoff guidance. Devkit should not grow arbitrary
  checkout remote mutation flows; add or use a Launchplane service route first.
- Odoo runtimes whose `PLATFORM_INSTANCE` is not `local`, `dev`, or
  `development` (an empty or unset value included) fail closed on unsafe
  startup credentials:
  the master password must be present and non-default, and an explicit admin
  password must be configured before the startup wrapper marks the runtime
  usable. Those three developer instance names may omit the admin password,
  but previews, testing, and prod must not expose an Odoo database with default credentials.
- A restore onto any non-production instance commits a login-preservation
  marker before the first registry load, including with `--no-sanitize`.
  Restore, subsequent startup and maintenance preserve the copied login
  passwords, including the configured administrator, even when the lane's
  isolated bootstrap password differs. Keep `ODOO_ADMIN_PASSWORD` configured:
  public preflight still requires it, and fresh databases still use it.
  The active default-password refusal still runs; an unsafe restored login
  fails preparation instead of being rotated. Production ignores the marker.
  Infrastructure credentials, user API keys and outbound integrations still
  follow the existing sanitization and lane-secret rules below.
- Devkit-managed startup Odoo launches and data workflow Odoo shell subprocesses prepend
  `/volumes/scripts` to `PYTHONPATH` so shipped runtime helpers remain
  importable from generated shell snippets.
- Data workflows close their module-state metadata transaction before launching
  an Odoo install/update subprocess. This prevents the parent workflow from
  retaining a read lock on `ir_module_module` while the child process performs
  schema-changing module upgrades.
- Startup reports `launchplane_settings_applied=false reason=no_payload` when
  there is no override payload, or `reason=no_managed_settings` when the supplied
  payload has no managed settings. A successful managed settings apply reports
  `launchplane_settings_applied=true` after commit.
- Local restore, bootstrap, update, init, and OpenUpgrade workflows require a successful
  web stop before proceeding and restart web only after the operation succeeds.
  A failed stop, operation (including exit code 10), or restart fails the command.
  After a failed operation, web stays stopped for recovery; correct the failure
  and rerun the same local workflow to restart it on success.
  If the operation completed and only the web restart failed, correct the startup
  problem and use `platform runtime up` with the same manifest; the data workflow
  does not need to run again. Interrupting an operation also leaves web stopped.
- Upstream restores capture the custom-format database dump in a private
  `.<database>-upstream-restore` directory beside the workflow lock file
  (on the mounted data volume by default). Keep that lock directory on persistent
  storage when overriding its path. The complete archive is read with `pg_restore`
  before changing the target database or filestore. Shell pipelines fail when
  any component fails, including SSH. Incomplete captures use `database.partial`;
  only a validated capture replaces `database.dump`. The last verified dump
  survives failed recaptures and container recreation. Later failures keep that
  one dump at the logged path for operator recovery; a fully successful restore,
  migration, and sanitization removes it. The retained dump contains unsanitized
  source data and credentials; restrict access and remove it after operator
  recovery if no successful retry follows. This is temporary recovery retention,
  not a backup of the previous target or protection against data-volume removal.
  An early capture failure leaves target data unchanged. Once the old target
  database has been dropped, any later failure (a partial `pg_restore`, the
  filestore copy, OpenUpgrade, sanitize, addon install or update, or the
  Launchplane settings apply) drops the restored database. A failure while blocking
  outgoing mail right after that apply also drops it. Web never boots
  an unsanitized production copy. Only later failures (the core schema check and
  GPT user provisioning) keep the sanitized database. Filestore capacity is checked again after
  capture so the dump's space is reflected before replacement begins.
  Capture and validation now finish before
  filestore copying begins, increasing the time web is stopped for large restores.
- `ODOO_UPSTREAM_USER` selects the SSH login. With `root`, the dump retains
  the existing `sudo -u ODOO_UPSTREAM_DB_USER pg_dump -Fc` behavior. Other
  logins run `pg_dump -U ODOO_UPSTREAM_DB_USER -Fc` directly, without sudo.
  Configure that database role with read-only privileges and server-side
  authentication for the SSH user. Devkit does not create users, grant privileges,
  or deliver database passwords to the upstream host.
  Restrict the upstream SSH key on the server to the exact dump and read-only
  rsync of the configured filestore. Filestore capacity estimation uses an
  rsync dry-run with statistics through that same read-only source, so the
  key needs no remote `du` or general shell access. The real transfer pulls
  from upstream; its `--delete` applies only to the local destination.
  A forced-command wrapper must admit both rsync sender forms, the dry-run
  and the transfer, while refusing receiver commands. With `rrsync -ro DIR`,
  `ODOO_UPSTREAM_FILESTORE_PATH` names a path within that restricted directory;
  use `/` when `DIR` is the exact source database's filestore. Do not reuse
  its absolute host path unless the installed rrsync supports that mapping.
  The existing best-effort behavior when size estimation is unavailable stays.
  Use `DATA_WORKFLOW_SSH_KEY` for a dedicated identity. Provisioning upstream
  access and changing hosted settings remain outside the local devkit commands.
- Every upstream restore onto an instance that is not explicitly production
  (`PLATFORM_INSTANCE` `prod` or `production`) clears the production
  integration credentials and signing keys that the copy brought with it.
  An empty or unknown instance counts as non-production, and the container
  workflow's `--no-sanitize` / `NO_SANITIZE` setting (not exposed by
  `platform runtime restore`) does not skip this step. It deletes the Shopify store credentials, the store
  URL and test-store flags, and the import cursors. It cancels open Shopify
  sync jobs and turns off the Shopify crons. It preserves external IDs,
  external-system bindings, record relationships and product export history.
  Only a managed Shopify development-store apply on `testing` clears Shopify
  IDs and export state for the existing Reset Shopify flow. It
  clears secret-shaped `ebay.*` parameters (these are never kept on a copy),
  the PrintNode key and the Mapbox, Unsplash and Tenor tokens. It
  deletes the Fishbowl, RepairShopr sync and cm_data database connections
  (host, database, user and password), so import crons that install hooks turn
  back on cannot reach the production databases. It disables every payment
  provider that reaches a remote service (anything but `none`, `custom` and
  `demo`). Odoo 19 providers enter the disabled state, including providers in
  test mode; Odoo 20 providers become inactive. Copies retaining both schema
  fields have both disabled, and readback rejects either enabled field.
  It blanks the providers'
  credential fields. It deactivates every incoming mail server and blanks its
  password and its Gmail or Outlook OAuth tokens. It also deletes the Gmail
  and Outlook OAuth app client secrets (`google_gmail_client_secret` and
  `microsoft_outlook_client_secret`). Outgoing servers lose their OAuth tokens
  too. It replaces each IAP account token with a new one, so the copy
  cannot spend production's credits. It deletes every user API key
  (`res_users_apikeys`); GPT users get theirs again after the restore. It
  deletes both web push VAPID keys along with every push device and queued
  push; Odoo generates new VAPID keys on demand. It regenerates
  `database.secret`. `database.uuid` is kept: it identifies the database,
  authenticates nothing, and Odoo's own neutralize keeps it too.
- For an OpenUpgrade target in major 20, selected from the explicit target
  or `ODOO_VERSION`, the existing packaged runner
  uses native `base,web` loading and selects its SQL-only
  `prepare_core_references_20.py` with native `--pre-upgrade-scripts`.
  Explicit `ODOO_INSTALL_MODULES` (comma- or colon-separated) selects initial
  installs with `--init` in the same invocation as `--update all`, so their
  native post-init hooks run before module cleanup. Unset, empty and `AUTO`
  declarations add no initial installs. Other targets do not consume this
  selection during OpenUpgrade. The later restore install phase cannot
  substitute for an initial migration ownership handoff.
  It retains the discovered upgrade roots; it does not load the Odoo 19
  OpenUpgrade framework or reset module states or versions. Subsequent addon
  maintenance and release-plan shells also skip that framework. Other targets
  retain their existing OpenUpgrade selection. Native 20 rejects
  `--reset-versions` before asset discovery or database access; run it without
  that flag to preserve the installed versions.
  Before native security data loads, the bridge releases registered modules'
  legacy `ir.model.access` and `ir.rule` XMLID names using the
  [upstream-derived transition already used by OPW](https://github.com/hbrunn/OpenUpgrade/blob/2d9eb6bab7e7e579953e38d7ad3b96aad6fcc44d/openupgrade_scripts/scripts/base/20.0.1.3/pre-migration.py).
  It appends the legacy model suffix, retaining each alias's ID, record binding,
  ownership and `noupdate`. Custom/export/Studio aliases, aliases without a registered
  module owner and all legacy policy rows remain untouched. Already released
  names and native `ir.access` bindings survive replay unchanged. Any destination
  collision refuses during the shared preflight, before any bridge effect.
  Native 20 loads its own stock defaults; this step neither converts legacy
  rights nor settles own-addon or data-only policy disposition.
  The legacy model suffix is reserved for released names; an original alias
  already using it needs product-owned disposition before native loading.
  When adopting the shared step, remove the equivalent tenant pre-migration
  rename so the later native module migration cannot append the suffix again.
  The bridge resolves `base.state_in_or` from the source database and adds
  `base.state_in_od` on that same state row, checking the running target's
  own state CSV and the source country binding. With an installed Website,
  it also binds `base.default_website` to the existing
  `website.default_website` row. Those source aliases, other owners, record data
  and incoming links are untouched. Replay keeps existing target metadata;
  missing, dangling, conflicting or unsupported custom identities refuse
  before either alias is inserted. An updateable custom state name refuses
  rather than being overwritten by target data; a protected name retains
  its protection. An updateable source website or unprotected existing
  target website alias refuses rather than accepting fresh base defaults.
  Before the base bank form updates, an installed Account's surviving primary
  bank form retargets its `acc_number` XPath to `account_number`. An installed
  HR Calendar's primary partner search view likewise retargets its stock
  `type_company` filter locator to `filter_vat_set`, before the base search view
  reloads. Both transitions use the same preflight and update every stored
  language. Only the selectors change; view and alias identities, custom content,
  incoming references, history and metadata stay intact. Protected ownership,
  unsupported inheritance and absent, duplicate or mixed selectors refuse before
  any bridge write. Replay is unchanged. Each owning module's later native update
  owns its full target architecture and validation; this step does not migrate
  bank or partner data.
  Before native Discuss initializes its unique direct-chat audience, the same
  preflight checks every duplicate audience and every stored channel/member
  column. Only redundant empty partner chats with default state and matching
  sidebar visibility qualify. Current partner names must explain their generated
  chat names; read state, customization, history,
  aliases or incoming references refuse before any bridge write. Unknown populated
  columns refuse. The stored livechat weekday and start hour may be populated only
  when they match the native projections of that chat's UTC creation date; NULL
  values remain supported before computation. Livechat configuration, help state,
  history, notes, ratings and relations retain their state and reference guards.
  Physical references without foreign keys, materialized reference
  storage, company-dependent
  references, and custom deletion triggers also refuse. The reference scan covers
  channel and member foreign keys, indirect model references, field defaults,
  aliases, exact stored Reference values and JSON references. JSON Properties
  definitions naming either Discuss model refuse as unsupported storage. The scan
  reads physical base tables; ordinary views and inline-query reports hold no
  rows of their own. Relational metadata checks physical columns without guessing
  a table name from its model. Many-to-many storage retains its explicit table
  ownership and integer-type requirement. Ownerless same-name text columns may
  hold native JSON notification tuples: a database and topic, or a database,
  known model and record ID with an optional string topic. Candidate model/ID
  pairs and ambiguous model/Reference/ID prefixes or topics refuse; unrelated tuples stay
  unchanged. Unknown text encodings, other unsupported types and uncertain
  populated matches without foreign keys refuse. Pinning is compared by
  the native visible-state rule, without comparing application and database clocks. After all candidates pass, the lowest
  channel ID survives unchanged and the redundant chats and their members retire.
  Their old links stop resolving, as permitted by Director decision Q1113; this
  creates no redirects or general chat-merging path. Replay leaves the survivor
  unchanged. A historical partner rename or unqualified stored state can therefore
  require further source investigation rather than automatic retirement.
  For installed Resource, the same preflight recognizes legacy nonworking lunch
  rows before native recomputation. Only zero-duration breaks on single-week,
  undated calendars without sections or resource-specific intervals qualify.
  Within calendars containing lunch rows, overlaps with work (including native
  full-day work), unsupported periods or clock ranges, and populated custom
  break fields refuse. Calendars without lunch rows stay outside this treatment. The shared reference checks also cover attendance
  foreign keys, indirect and Reference links, XMLIDs, defaults, JSON/Properties,
  physical relations without foreign keys, materialized storage and deletion
  triggers. Referenced breaks refuse rather than losing their links. After the
  whole bridge passes preflight, only the qualified break rows are removed;
  work intervals and stored calendar totals stay unchanged, so native 20 keeps
  the nonworking gap. Replay has no remaining lunch rows to change. This does
  not split overlapping work or translate two-week, section or date semantics.
  Same-major 20 updates do nothing in this bridge. Its qualification ends at
  the reference/base-data boundary; security conversion, later cleanup,
  tenant hooks and a completed major-version migration need their own proof.
  This selection does not replace the restored-copy credential boundary or
  authorize a Client data run.
- Before post-deploy maintenance, update-only work or any restored-copy Odoo
  process (including OpenUpgrade), the runner commits a SQL-only preparation
  transaction. Restored copies reuse the credential-clearing rules below;
  ordinary maintenance preserves lane-owned integration IDs, API keys, signing
  keys and push devices. The transaction applies managed
  `config_parameters` and Shopify credential values from the existing typed
  payload and fences non-production outgoing mail. Crons are disabled only
  when requested restore sanitization calls for it, preserving `--no-sanitize`.
  Requested restore sanitization is in this transaction too, before registry
  loading rather than after OpenUpgrade. An independent database connection
  checks that the stripping, overrides and fences committed; a failed or
  rolled-back commit stops all later Odoo processes. A fresh transaction witness
  also proves a commit when all settings were already correct. Explicit production lanes
  preserve their integration credentials except for supplied managed overrides.
  Both production aliases are exported to the installed settings addon as its
  canonical production lane, so a `production` lane does not get non-production
  Shopify clearing. The non-secret commit witness is visible in System Parameters.
  Non-production retained-integration exceptions keep their existing scope.
  Addon override targets unused by the installed settings consumer stay unused.
  Credential parameters use the supported typed `config_parameters` surface;
  new credential-capable adapter behavior must extend the SQL boundary too.
  On post-deploy and restore runs, Authentik's client identifiers and presentation settings, Shopify's
  URL/dispatcher bookkeeping and website bootstrap still use their installed
  ORM adapters after the credential transaction. No addon code or Odoo shell is
  used to strip credentials. Callers must keep other workers stopped until
  preparation and maintenance finish, including the scheduler during a delayed
  testing store change. Use the existing stopped-runtime maintenance procedure
  before starting the web process with changed store settings; an environment-only
  plan stages values and does not authorize a running scheduler to overlap the
  transition. On readback failure keep those workers stopped and use the supported
  restore/reset procedure before resuming; this is not a traffic-switch mechanism.
  Restore reasserts the boundary after module work before running further
  settings/admin hooks, because install data can re-enable integrations.
  OpenUpgrade also reasserts requested sanitization before addon installation,
  because migrations can reactivate crons. Update-only stages the credential
  values and updates modules; it does not activate integrations or complete ORM
  bookkeeping. Use `--post-deploy-maintenance` for that complete operation.
- Addon maintenance starts Odoo only when the computed install or update list
  contains work. In the legacy install-before-update path, already installed
  addons skip the install pass without loading a registry; the subsequent
  upgrade still runs. This lets upgrade migrations repair tenant state before
  ordinary registry guards run, while preserving the guards and completion
  evidence requirements.
- `ODOO_RESTORE_KEPT_INTEGRATIONS` (comma-separated) names integrations whose
  restored settings stay, using the integration names of Launchplane's
  read-back: `shopify`, `printnode`, `fishbowl`, `repairshopr`, `cm_data`,
  `payment`, `incoming_mail`, `iap`, `mapbox`, `unsplash`, `tenor`. Keeping
  `incoming_mail` preserves incoming-server settings, passwords and OAuth
  tokens, along with the Gmail and Outlook OAuth app client secrets. Outgoing
  server OAuth tokens are cleared regardless of that allowance.
  Launchplane should set it from a lane's `pre_live` and `read_only_source`
  allowances, the two kinds that permit the lane to hold production's own
  values. A `dev_store` integration is not listed: the restored production
  values are cleared and Launchplane applies the development account after
  the clearing. Unset keeps nothing, so until Launchplane passes it, a
  restore clears every integration, including allowed import sources. Web push,
  user API keys and `database.secret` are cleared whatever it says, and other
  or unknown names are logged and ignored.
- The step runs right after `pg_restore`, before any Odoo code, and again after
  the addon install and update. A read-back then fails the restore if any
  restored value of a cleared integration survived, by parameter or by
  table column. It also fails the restore if a remote payment provider is
  still enabled or an incoming mail server is still active. Launchplane's
  settings apply runs after that, so the instance's own settings are kept.
  The key list lives in one place, at the top of
  `docker/scripts/run_odoo_data_workflows.py`.
- Release/deploy ownership for remote environments stays in
  `launchplane`, even when the same tenant manifest is used to anchor
  local runtime context.
- Every runtime subcommand accepts `--instance <name>`, but it is not a remote
  mutation hook: only `publish` accepts a non-local instance, and every other
  subcommand requires `--instance local`. The stable remote lane model is
  `testing` plus `prod`; those mutations belong in Launchplane service routes
  and reusable workflows.
- `platform runtime logs` and `platform runtime psql` are intentionally
  local-only helpers for manifest-backed debugging. They require
  `--instance local` and fail closed for non-local targets instead of falling
  through to an implicit remote path. psql reads `POSTGRES_PASSWORD` inside
  the database container into `PGPASSWORD`, keeping it out of Compose and psql
  arguments and command-failure diagnostics.
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
  and verifies their OCI index/platform source/revision annotations
  (or config labels on unannotated legacy bases); dry-runs the exact exported
  support and tenant requirements against each selected base-runtime platform's
  installed package constraints; then builds and pushes the requested artifact
  tag. Base-package overlap fails before Buildx starts instead of surfacing only
  inside the artifact image build.
- After the push succeeds, publish reads the immutable artifact index digest
  from Buildx metadata, extracts each target platform's dependency sidecar from
  that digest, verifies the sidecars against the staged lock hashes and source
  commits, and writes Launchplane artifact-manifest schema v2. The manifest
  includes base-image/build-tool provenance, both uv locks, per-platform exact
  Python package inventories, and external compatibility descriptors.
- The artifact path uses `docker/artifact.Dockerfile`; local Compose continues
  to use `docker/Dockerfile`. In dependency provenance, each lock's repository,
  source ref and path identify its source inputs (`docker/runtime-python/uv.lock`
  or tenant `uv.lock`); its hash attests the exact derived bytes installed in the
  image, not the original committed blob. Existing immutable base-image
  provenance identifies the base used for derivation; no additional receipt or
  artifact schema is introduced. Nested shared-addon source markers keep
  installed shared distributions attributed to the shared repository commit.
- Publish does not mutate `/venv` after dependency evidence is written. An
  OpenUpgrade or other Python dependency must be represented by the locked
  support/tenant catalogs or by exact external compatibility evidence; an ad
  hoc post-sync install is not part of the artifact contract.
- `ODOO_PYTHON_SYNC_SKIP_ADDONS` is legacy-layout behavior and is rejected for
  schema-v2 publish because exporting the full workspace lock while skipping a
  member would make the evidence false. Remove the project from the tenant
  workspace instead.
- Non-local publish requires the product's build to supply
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
  It reads the database password from the existing container environment through
  Odoo's native `PGPASSWORD` input. The password is absent from process arguments
  and the command shown by `--dry-run`. Local init and administrator-password
  helpers use the same container-side database password forwarding. Init refreshes
  the private runtime environment from the current payload before Compose
  starts the script-runner, so password changes do not use stale selected values. Admin
  password updates read `ODOO_ADMIN_PASSWORD` inside the container; scripts
  and command-failure diagnostics contain no configured password values.

## Isolated migration and saved copies

`ODOO_COPY_PURPOSE=migration` selects the existing SQL credential boundary for
an isolated restored or saved copy. Put it in the Odoo container environment of
the existing heavy-run manifest, carrying that manifest's input/stage identities
and receipt forward with the saved DB and filestore. Every copy loader must bind
the purpose before its first Odoo process, including a saved-stage clone; a
production-like instance label and a database name are not purpose evidence.
There is no new snapshot catalog or queue. Do not pass this setting on a real
Client-accepted production restore; that existing runtime path retains its
production behavior. The operational caller must select migration purpose for an isolated copy.

A source caller loading an already restored database can use the SQL-only API:

```python
runner.prepare_isolated_copy_before_registry(purpose="migration")
```

An SQL-only stage check reuses that final prepared runner and its existing
connection:

```python
runner.verify_isolated_copy_before_registry(database_connection=connection)
```

This readback performs only reads on the supplied connection. It neither
prepares the copy nor opens a connection, starts Odoo, commits or rolls back.
The runner must retain its successful current migration preparation pass;
a fresh runner, failed later preparation, changed transaction witness, kept
integration or service key refuses. The same readback is used by preparation's
independent observer. It checks credential rotation/clearing, outgoing and
incoming mail, cron and copied runnable Shopify jobs, while preserving channel
identities, cursors and pending export intent. Missing optional addon tables
remain supported. Present credential surfaces must be fully readable without
active PostgreSQL row filtering; inaccessible columns or missing required
core tables fail rather than appearing empty. The caller retains its existing
database/stage identity, writer exclusion and transaction ownership checks.

Preparation requires `migration` purpose and commits and independently reads
back the same boundary before returning. Missing or unsupported purpose refuses
this isolated-copy API; ordinary runtime maintenance and accepted production
restores keep their existing path without that setting. The method exports the
purpose to child processes. The normal restore and standalone OpenUpgrade paths
also accept it through `LocalServerSettings`, including env files; local exec
forwards it. Keep it on every subsequent saved-copy invocation. The
receipt/manifest is caller-owned provenance, not permission to read private data
or to mutate production.

This boundary clears all copied integration secrets regardless of instance
label, including secret-shaped `ebay.*` parameters if present. It preserves
Shopify/eBay external IDs, external-system rows, relationships, product export
flags/timestamps and Shopify import cursors for reconciliation. Copied Shopify
jobs are canceled; every cron is disabled, outgoing mail is blocked, API keys
and push devices are removed, payment providers and incoming mail are disabled.
This is mandatory even with `--no-sanitize` or `ENV_OVERRIDE_DISABLE_CRON=false`.
Managed credential parameters, kept-integration exceptions and service-key
provisioning refuse before registry loading. Non-credential addon settings are
validated and left unapplied. SQL
non-credential config overrides retain their existing support. ORM integration
settings are skipped on the copy. The startup wrapper repeats the boundary
before initialization, each shell and the server; it disables server cron
threads and refuses a separate scheduler. Other workers must remain stopped
through preparation. Standalone Odoo commands must use the API first.

For the separately selected testing copy, the existing managed Shopify apply
selects its development store with `test_store=true`, a store key, API token,
webhook key and API version. With no migration purpose, the SQL boundary
clears that copy's Shopify IDs/export state and cancels old jobs before applying
the selected store on a restore or when the stored store key differs from the
incoming key. A delayed apply follows the same path because quarantine removed
the copied store key. Reapplying or rotating credentials for the same store
preserves its IDs and jobs. Startup prepares a testing managed-settings apply
before the ORM shell, after base initialization; data-workflow callers prepare
before their settings apply. Independent readback verifies identities, export
state and open jobs for each reset. Store changes leave cron activation to the
existing settings consumer, preserving offline-check cron state. The existing store
verification and Reset Shopify workflow then run normally. Testing without that
explicit selection and all migration copies preserve identity links. No eBay reset is added. Existing store-separation and release
boundaries still govern reconnection; this setting grants no new authority.

The migration lead owns the tenant hook/runbook integration handback on the
source issue. Shared devkit docs retain the generic caller contract only.

## Ownership Rules

- PyCharm should still open the tenant repo directly.
- Codex and Claude Code should start from the assembled workspace root.
  For Claude Code, start with `docs/session-prompt.md` and explicitly read the
  generated `AGENTS.md`; it is not loaded automatically.
- Generated workspace-root files are a cockpit layer; they are not the
  source-of-truth repo.
- If a generated file is wrong, change the generator in `odoo-devkit`.

### Literal passwords at local consumers

Local admin application and startup preserve surrounding spaces and tabs in
`ODOO_ADMIN_PASSWORD`. Startup and data-workflow hardening also preserve it
when checking whether the configured password already matches.
Blank/omitted admin passwords do not request an update.
Devkit's typed runtime input rejects line separators and NUL bytes. The shared
image startup and data-workflow settings enforce the same single-line contract
for directly supplied `ODOO_ADMIN_PASSWORD`, before config writes, database
work, or password application. Rejected input is never trimmed into a different
password, and errors omit the supplied value. Correct the operator-supplied
environment value to the intended single-line password and rerun the command
or restart through the lane's normal control plane. Preserve intentional spaces
and tabs; remove an accidental trailing newline at the input source. Existing
passwords stay unchanged when input is rejected. Hosted environment values
and secret bindings remain Launchplane-owned.

Odoo 19 trims surrounding whitespace in config-file values. Local Odoo shell
and initialization commands forward `ODOO_DB_PASSWORD` as `PGPASSWORD` inside
the container; Odoo's environment input overrides `db_password` from the
file and preserves the supplied value. Use the devkit commands for this path;
loading the generated conf alone is not equivalent.

## External tenant worktrees

The tenant overlay's `scripts/workspace-sync` and `scripts/workspace-status`
keep using the active checkout's `workspace.toml` and tenant source. In a
linked Git worktree they discover the primary checkout through Git's common
repository directory, find its sibling `odoo-devkit`, and resolve relative
non-tenant repository paths against that primary tenant checkout. Absolute
paths and repo-addressed managed inputs keep their existing meaning.

The underlying manifest commands accept `--related-repo-root /path/to/tenant`
for this explicit path resolution. The tracked manifest is never rewritten;
source paths are resolved in memory. Without that option relative paths keep
resolving against the manifest directory. A workspace synced with the option
writes it into the generated devkit commands, such as the dependency check, so
they resolve the same related repositories.

Linked-worktree wrappers default `ODOO_WORKSPACE_ROOT` to `<worktree>/.workspace`,
so generated guidance lives at `.workspace/<workspace.name>/` and sync does not
replace the primary workspace. This generated directory is ignored by the
starter overlay. An explicitly supplied `ODOO_WORKSPACE_ROOT` is honored.
Primary checkouts retain their manifest-defined workspace location.

Set `ODOO_DEVKIT_REPO` to an existing devkit checkout when it is elsewhere or
when testing a devkit task branch. That override selects the executable code;
related manifest inputs still resolve against the primary tenant checkout.
Run status with `--check` before using guidance and sync when it reports stale
or missing materialization. A missing devkit checkout reports the override
instead of attempting uv in a nonexistent directory. No primary workspace
files or credentials are copied into the task workspace.

## Shared file stores in queued offline runs

For an isolated migration or test that leaves its frozen file-store base unchanged,
put `"overlay": true` on that directory's existing heavy-manifest copy entry:

```json
{
  "source": "/private/frozen-stage/filestore",
  "destination": "/volumes/data/filestore/migration_copy",
  "overlay": true
}
```

This uses the Q861 Docker local overlay mechanism: the verified staged directory
is the lower, with private upper/work directories in a run-owned backing volume.
The container reads the lower through the overlay; writes and deletions stay in
its upper. Containers in one run using the same source and destination share
that upper. Different destinations and different runs get separate writable
state. Lower bytes and filesystem identity are checked again after containers
stop. Failure keeps the original cleanup custody and stops successor admission.
There is no fallback to a copy when overlay mounting fails.

The private receiver can reuse the same retained lower across runs. Use an
original admitted with `overlay: true` after the matching helper/receiver pair
is adopted, so its new input has container-copy permissions and its returned
evidence includes `run.json`. After that run completes and its inputs are
independently qualified, keep
that original request's guest inputs in place. Obtain its UUID from the existing
`request.json` and its exact input name from the returned `run.json`, alongside
`image-provenance.json`. A subsequent copy entry keeps the same frozen local
source and adds the original input reference:

```json
{
  "source": "/private/frozen-stage/filestore",
  "destination": "/volumes/data/filestore/migration_copy",
  "overlay": true,
  "retained_from": {
    "job": "00000000-0000-4000-8000-000000000001",
    "input": "inputs/0"
  }
}
```

Use the actual original UUID and input name. The receiver checks the original's
cleanup receipt, frozen transfer inventory, current bytes and the requesting
local source's hashes before execution. A missing, changed, aliased or retired
lower refuses admission. The reference cannot select an arbitrary host path or
chain through another reference. The lower stays owned by its original request;
retiring a borrowing run removes only that run's own inputs/images. Keep the
base while future runs need it. Its owner uses the original request's existing
retirement path. If the original has already completed retirement, a borrower
can still retire its own copies against the preserved original receipts; it
cannot execute again using that retired lower.

Keep `ODOO_COPY_PURPOSE=migration`, source/image/stage bindings, disconnected
networking, resource caps, credential stripping and the normal heavy entrypoint.
The flag replaces the container copy for this destination, so remove any job
command that copies/extracts a file store over the same mounted destination.
Retain database restoration and the SQL boundary before the first registry.
For an already restored database, use the existing
`runner.prepare_isolated_copy_before_registry(purpose="migration")` API above.
Do not run restore/bootstrap file replacement or recursive `chown`/`chmod`
over the overlay: metadata changes can copy the entire lower into its upper.
The receiver has already prepared container-copy permissions. Use the
independent-copy case if a workflow requires that whole-store rewrite.
When the base itself must change, omit both `overlay` and `retained_from`; the
existing ordinary copy is the independent writable case. An overlay never
publishes its private writes as a new saved stage automatically.

Adoption requires the matching catalog heavy helper and private receiver source
installed together through the receiver's documented drained-queue procedure.
Do not run these examples against an older installed pair. The lead owns that
adoption and the single Q1096 real-size timing/disk comparison in the Q1097
batched real-copy check. Source checks do not establish receiver mount support
or a migration result. Measure file-store input transfer, verification, overlay
preparation, database restore, and cleanup separately; a shared lower avoids its
bulk retransfer on subsequent runs but still requires integrity reads. Preserve
any full-copy headroom refusal rather than inventing a before measurement.
