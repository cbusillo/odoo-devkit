# Platform Config Layering

This directory holds fallback defaults for devkit-owned local runtime compose
commands. Canonical tenant environment values come from `odoo-control-plane`,
not from devkit-local secrets files.

## Platform Runtime Generation

`platform runtime ...` resolves configuration in this order:

```text
odoo-control-plane config/runtime-environments.toml
-> selected generated runtime env file
-> platform/config/base.env fallback defaults
```

`ODOO_CONTROL_PLANE_ROOT` must point at the control-plane checkout when runtime
commands need tenant environment truth.

## Raw Compose Loading

Raw `docker compose` commands that bypass `platform runtime ...` still use the
compose `env_file` entries directly:

```text
.env (optional)
-> platform/config/base.env
```

Prefer `platform runtime ...` for tenant work so environment authority remains
single-source.

## Rules

- Keep tenant secrets and canonical stack values in control-plane environment
  contracts.
- Keep `platform/config/base.env` limited to shared fallback defaults.
- If a canonical value conflicts with `base.env`, tooling fails closed and asks
  for the duplicate to be aligned or removed.
- `ODOO_LIST_DB` must remain `False` for managed stacks.
