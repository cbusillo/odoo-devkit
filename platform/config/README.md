# Platform Config Layering

This directory holds fallback defaults for devkit-owned local runtime compose
commands. Required tenant runtime values come from the typed
`ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON` operator input, not from checked-in or
devkit-local secrets files.

## Platform Runtime Generation

`platform runtime ...` writes the selected generated runtime env file from:

```text
ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON
+ selected stack local configuration
= selected generated runtime env file
```

Compose then loads `platform/config/base.env` as fallback defaults, followed by
that generated file (`PLATFORM_RUNTIME_ENV_FILE`), so generated values override
`base.env`.

The payload context and instance must exactly match the manifest-selected
runtime. Missing or mismatched input fails closed.

## Raw Compose

Raw `docker compose` bypasses the typed runtime resolver and is not a supported
tenant runtime input path. Use `platform runtime ...`; the compose fragments now
require its generated `PLATFORM_RUNTIME_ENV_FILE` instead of falling back to a
repo-local `.env` file.

## Rules

- Keep local tenant secrets in operator-local secret storage and inject them
  through the typed runtime payload.
- Keep shared/testing/prod values in Launchplane-managed runtime records and
  secrets.
- Keep `platform/config/base.env` limited to shared fallback defaults.
- Generated values override `base.env`; remove any `base.env` entry that
  duplicates a canonical value instead of relying on the override.
- `ODOO_LIST_DB` must remain `False` for managed stacks.
