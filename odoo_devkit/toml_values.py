"""String readers shared by the workspace and cockpit manifests."""


def read_required_string(source: dict[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected {key} to be a non-empty string")
    return value


def read_optional_string(source: dict[str, object], key: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected {key} to be a string when present")
    return value
