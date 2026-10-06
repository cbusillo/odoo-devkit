"""The single-line admin password contract for image-owned consumers."""


def validate_admin_password(value: str) -> None:
    if "\x00" in value or (value and value.splitlines() != [value]):
        raise ValueError(
            "ODOO_ADMIN_PASSWORD must not contain line separators or NUL bytes. "
            "Correct the supplied environment value to the intended single-line password; spaces and tabs are literal."
        )
