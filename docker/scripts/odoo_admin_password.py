"""The single-line admin password contract for image-owned consumers."""

from typing import Any

RESTORED_LOGINS_PARAMETER = "odoo_devkit.preserve_restored_logins"


def restored_logins_preserved(cursor: Any) -> bool:
    cursor.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (RESTORED_LOGINS_PARAMETER,))
    return cursor.fetchone() == ("true",)


def validate_admin_password(value: str) -> None:
    if "\x00" in value or (value and value.splitlines() != [value]):
        raise ValueError(
            "ODOO_ADMIN_PASSWORD must not contain line separators or NUL bytes. "
            "Correct the supplied environment value to the intended single-line password; spaces and tabs are literal."
        )
