from __future__ import annotations

import os

RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR = "ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON"


def sanitized_subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop(RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, None)
    return environment
