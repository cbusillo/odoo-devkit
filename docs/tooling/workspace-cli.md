# Workspace CLI

`odoo-devkit` owns the manifest-driven workspace command surface used to build
the Every Code cockpit and the local runtime assembly.

Native runtime ownership is now split by target type instead of by command
name:

- manifest-local runtime targets run natively in `odoo-devkit` for
  `platform runtime select`, `up`, `inspect`, `restore`, and
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
uv run platform workspace scaffold-tenant-overlay --output-dir /path/to/repo --tenant opw
uv run platform workspace clean --manifest /path/to/workspace.toml
uv run platform workspace run --manifest /path/to/workspace.toml -- pwd
uv run platform runtime select --manifest /path/to/workspace.toml
uv run platform runtime up --manifest /path/to/workspace.toml --build
uv run platform runtime workflow --manifest /path/to/workspace.toml --workflow update
uv run platform runtime restore --manifest /path/to/workspace.toml
uv run platform runtime inspect --manifest /path/to/workspace.toml
```

If `--manifest` is omitted, the command looks for `workspace.toml` in the
current directory.

## `workspace sync`

Purpose

- Materialize the tenant, devkit, and optional shared-addons sources under the
  workspace root.
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

## `runtime ...`

Purpose

- Expose the local runtime command surface through `odoo-devkit` so tenant
  overlays and generated PyCharm run configurations do not need to call a
  sibling runtime repo directly.
- Resolve the runtime target from `workspace.toml` and execute the supported
  local or Dokploy-backed workflow natively against the repo declared in
  `[repos.runtime]`.

Notes

- Keep the runtime repo explicit in the manifest because `odoo-devkit` still
  reads `platform/stack.toml`, layered env files, and `platform/dokploy.toml`
  from that repo.
- Native non-local ownership currently covers Dokploy-backed `restore`,
  `workflow bootstrap`, and `workflow update`; anything else should fail closed
  unless `odoo-devkit` grows an explicit remote contract for it.

## Ownership Rules

- PyCharm should still open the tenant repo directly.
- Every Code should start from the assembled workspace root.
- Generated workspace-root files are a cockpit layer; they are not the
  source-of-truth repo.
- If a generated file is wrong, change the generator in `odoo-devkit`.
