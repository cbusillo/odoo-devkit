# odoo-devkit

`odoo-devkit` bootstraps tenant-focused Odoo workspaces from a tracked
`workspace.toml` manifest.

The current workspace flow:

- assembles a long-lived but rebuildable workspace under
  `~/Developer/odoo-workspaces/<tenant>` by default,
- treats the active tenant checkout as the source of truth for handwritten
  code,
- emits a `workspace.lock.toml` file with the exact assembled refs,
- generates a minimal runtime config scaffold under `.generated/`, and
- generates workspace-root `AGENTS.md`, `docs/README.md`, and
  `docs/session-prompt.md` so Every Code can use the assembled workspace as a
  shared cockpit without turning each tenant repo into a copy of the shared
  operating guide, and
- owns the pure PyCharm Odoo-conf helper and the starter templates for thin
  tenant overlays, and
- writes PyCharm-visible shared run configurations for rare-but-important
  commands.

For remote environments, the stable lane model is `testing` plus `prod`.
Launchplane-managed PR previews are a separate control-plane concern rather
than a third durable runtime lane exposed through `platform runtime`.
`odoo-devkit` can publish artifact images for handoff, but remote ship,
promote, gate, restore, bootstrap, update, and preview lifecycle flow belongs
in `launchplane`, not in branch-oriented `odoo-devkit` commands.

## Command surface

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/repo --tenant opw
uv run platform workspace scaffold-cockpit-root \
  --output-dir /path/to/workspace-root --force
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

If `--manifest` is omitted, the CLI looks for `workspace.toml` in the current
directory.

## Scope

This repo is intentionally small. It owns the manifest/runtime contract for
tenant workspaces and the local runtime commands needed to develop against
those workspaces.

- `workspace sync` materializes repo-addressable shared-addons inputs from
  `[repos.shared_addons].url` + `ref` into `sources/shared-addons` when the
  manifest does not point at a pre-existing local path.
- The active tenant checkout remains path-based and is still the source of
  truth for handwritten tenant code.
- Local runtime assets live in `odoo-devkit` itself. Tenant scaffolds keep
  `[repos.runtime]` pointed at the sibling `odoo-devkit` checkout so the same
  tracked tenant manifest can target local runtime work and artifact handoff
  without growing a repo-local remote mutation surface.
- Runtime repo ownership remains explicit. When `[repos.runtime]` is present it
  may be path-based or repo-addressable,
  `workspace sync` materializes repo-addressed runtime inputs into
  `sources/runtime`, and non-local runtime commands fail closed until that
  checkout exists.
- Odoo core is still inherited from the runtime image/tooling chain rather than
  materialized as a separate checkout.

Current runtime ownership is intentionally narrow and explicit:

- local runtime targets run natively inside `odoo-devkit` against the repo
  owned by `odoo-devkit` itself:
  `select`, `build`, `publish`, `up`, `down`, `inspect`, `logs`, `psql`,
  `odoo-shell`, `restore`,
  `workflow bootstrap`, `workflow init`, `workflow update`, and
  `workflow openupgrade`.
- Non-local runtime mutation is not a devkit command surface. Stable remote
  lanes (`testing`, `prod`) route through Launchplane service APIs, operator UI,
  or reusable Launchplane workflows. PR preview lifecycle also stays outside
  `platform runtime`.
- Release actions such as ship, promote, and gate execution belong in
  `launchplane`, not under `platform runtime`.
- non-local `workflow init` and `workflow openupgrade` remain local-only and
  fail closed with an explicit `--instance local` requirement instead of
  falling through to an implicit remote path.

## Runtime Contract Notes

- The shared tenant compose database service stays pinned to `postgres:17`
  while existing tenant DB volumes still use the legacy
  `/var/lib/postgresql/data` layout.
- The shared local compose contract includes the image-owned Launchplane runtime
  addon root `/opt/launchplane/addons` and loads
  `base,web,launchplane_runtime_health` as server-wide modules by default.
  Keep that addon root in the rendered `ODOO_ADDONS_PATH` so startup scripts,
  generated Odoo config, and wrapper-normalized server commands agree.
  Startup shell phases normalize `/opt/launchplane/addons` into their generated
  config before running database updates so server-wide runtime health stays
  loadable even when downstream image layers override `ODOO_ADDONS_PATH`.
  `/web/health` remains the local container liveness check; Launchplane runtime
  identity evidence is exposed by the base image at `/launchplane/health`.
- Public runtimes require `ODOO_ADMIN_PASSWORD`, but startup skips admin
  hardening when the configured `ODOO_ADMIN_LOGIN` is absent in a restored
  tenant database. This preserves boot for tenant databases that renamed or
  removed the default `admin` login while still checking active default admin
  passwords when matching users exist.
- A Postgres major-version bump is not a routine dependency refresh on this
  surface. Treat it as explicit migration work with a documented upgrade path
  for existing tenant data volumes.
- Dependabot should not propose Postgres major upgrades automatically for the
  root `docker-compose.yml`; those changes should be intentional operator work.

## Testing

```bash
uv run python -m unittest discover -s tests
```

For tenant repos that keep `instance = "local"` in the tracked manifest,
`--instance testing` or `--instance prod` is not a shortcut for remote
mutation. Release and non-local data actions should run through `launchplane`.
Local and artifact-handoff examples:

```bash
uv --directory ../odoo-devkit run platform runtime workflow \
  --manifest ./workspace.toml \
  --workflow bootstrap
uv --directory ../odoo-devkit run platform runtime publish \
  --manifest ./workspace.toml \
  --instance testing \
  --image-repository ghcr.io/example/odoo-opw \
  --image-tag opw-20260416-deadbeef \
  --output-file /tmp/opw-artifact.json
uv --directory ../launchplane run launchplane artifacts write \
  --state-dir ./state \
  --input-file /tmp/opw-artifact.json
```

`platform runtime publish` stages the tenant addons plus shared addons into a
real build context, requires clean git worktrees for the repos it captures,
pushes the resulting image, resolves the pushed digest, and emits a
control-plane-compatible artifact manifest JSON file.
When a repo-owned `artifact-inputs.toml` exists beside `workspace.toml`,
runtime and publish treat that file as the repo-owned source-input contract.
Runtime and publish no longer fall back to `stack.toml` source selector
fields.
