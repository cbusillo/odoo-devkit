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
- generates a workspace-root `AGENTS.md` plus `docs/README.md` so Every Code
  can use the assembled workspace as a shared cockpit without turning each
  tenant repo into a copy of the shared operating guide, and
- owns the pure PyCharm Odoo-conf helper and the starter templates for thin
  tenant overlays, and
- writes PyCharm-visible shared run configurations for rare-but-important
  commands.

For remote environments, the stable lane model is `testing` plus `prod`.
Harbor-managed PR previews are a separate control-plane concern rather than a
third durable runtime lane exposed through `platform runtime`.

## Command surface

```bash
uv run platform workspace sync --manifest /path/to/workspace.toml
uv run platform workspace status --manifest /path/to/workspace.toml
uv run platform workspace scaffold-tenant-overlay \
  --output-dir /path/to/repo --tenant opw
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
  tracked tenant manifest can target local runtime work and explicit
  Dokploy-managed data workflows.
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
- Dokploy-managed non-local runtime targets also run natively inside
  `odoo-devkit` for `restore`, `workflow bootstrap`, and `workflow update`
  using the runtime repo's generated env plus Dokploy target metadata from the
  control-plane-owned `config/dokploy.toml` and
  `config/dokploy-targets.toml` catalogs resolved through
  `ODOO_CONTROL_PLANE_ROOT`.
- Those non-local targets are the stable remote lanes (`testing`, `prod`). PR
  preview lifecycle and release orchestration stay outside `platform runtime`.
- Release actions such as ship, promote, and gate execution belong in
  `odoo-control-plane`, not under `platform runtime`.
- non-local `workflow init` and `workflow openupgrade` remain local-only and
  fail closed with an explicit `--instance local` requirement instead of
  falling through to an implicit remote path.

## Testing

```bash
uv run python -m unittest discover -s tests
```

For tenant repos that keep `instance = "local"` in the tracked manifest, use
an explicit runtime target override only for Dokploy-managed data workflows.
Release actions should run through `odoo-control-plane`. Data workflow
examples:

```bash
uv --directory ../odoo-devkit run platform runtime restore \
  --manifest ./workspace.toml \
  --instance testing
uv --directory ../odoo-devkit run platform runtime workflow \
  --manifest ./workspace.toml \
  --workflow bootstrap \
  --instance testing
uv --directory ../odoo-devkit run platform runtime publish \
  --manifest ./workspace.toml \
  --instance testing \
  --image-repository ghcr.io/example/odoo-opw \
  --image-tag opw-20260416-deadbeef \
  --output-file /tmp/opw-artifact.json
uv --directory ../odoo-control-plane run control-plane artifacts write \
  --state-dir ./state \
  --input-file /tmp/opw-artifact.json
```

`platform runtime publish` stages the tenant addons plus shared addons into a
real build context, requires clean git worktrees for the repos it captures,
pushes the resulting image, resolves the pushed digest, and emits a
control-plane-compatible artifact manifest JSON file.
