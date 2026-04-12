# Platform Config Layering (Local Dev)

This directory holds shared platform runtime defaults used by `uv run platform
...` and ad-hoc `docker compose` commands. Dokploy remote deployments use
Dokploy target settings (`composePath`) instead of these local overlays.

## Layer order

Platform runtime generation (`uv run platform ...`):

```text
odoo-control-plane config/runtime-environments.toml
→ selected runtime env file (.platform/env/<context>.<instance>.env)
→ platform/config/base.env fallback defaults
```

Local runtime selection requires `ODOO_CONTROL_PLANE_ROOT` and resolves tenant
environment truth from `odoo-control-plane`. Legacy devkit-local `.env` and
`platform/secrets.toml` runtime authority is no longer supported.

Raw Compose env-file loading (without platform runtime generation):

```text
.env (optional)
→ platform/config/base.env
```

Compose overlays (most generic → most specific):

```text
docker-compose.yml
→ platform/compose/base.yaml
→ docker-compose.override.yml (local-only)
```

## Local stacks

- `opw-local`
- `cm-local`

## Quick start

1. Copy the templates you need:
   - Recommended: populate `odoo-control-plane/config/runtime-environments.toml`
     and export `ODOO_CONTROL_PLANE_ROOT`.
   - For local image builds, keep private `ODOO_BASE_RUNTIME_IMAGE` and
     `ODOO_BASE_DEVTOOLS_IMAGE` in the same runtime authority surface.

2. Run the stack:

   ```bash
   uv run platform up --context opw --instance local --build
   ```

## Notes

- Repo-local `.env` is optional. When `platform runtime select` generated a
  runtime env file, compose uses that file plus `platform/config/base.env`
  and does not require a checked-out `.env` to exist.
- `ODOO_MASTER_PASSWORD` is required for all stacks; in control-plane mode keep
  it in `config/runtime-environments.toml`.
- `ODOO_LIST_DB` must be `False` to disable the database manager UI.
- `platform/config/base.env` is fallback-only for platform runtime generation
  (`uv run platform ...`). If a canonical stack value conflicts, tooling fails
  closed and asks you to align or remove the duplicate from `base.env`.
- Raw `docker compose` still loads `.env` then `base.env` for service
  `env_file` entries, so later values in `base.env` win in that path.
- Copy `docker-compose.override.example.yml` to `docker-compose.override.yml`
  when you need local port bindings or live code mounts. Keep
  `./addons:/opt/project/addons` in the shared section so `web` and
  `script-runner` execute the same addon source tree (see
  `docs/workflows/multi-project.md`).
- Restore runs (`uv run platform restore --context <target> --instance local`)
  rely on `DATA_WORKFLOW_SSH_DIR` being set so the base compose mounts the SSH
  directory for upstream access. Ensure that directory includes both the
  private key and a trusted `known_hosts` entry for the upstream host when
  strict host checking is enabled.
