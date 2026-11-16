from __future__ import annotations

import os
import re

RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR = "ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON"
ENVIRONMENT_VARIABLE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_single_line_environment_value(value: str) -> bool:
    return "\0" not in value and (not value or value.splitlines() == [value])


def sanitized_subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop(RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, None)
    return environment
