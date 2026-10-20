"""Lint the declared addon test entrypoint without executing repository commands."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path


def check_command(command: str) -> None:
    words = shlex.split(command)
    if len(words) < 4 or words[:2] != ["uv", "--directory"]:
        raise ValueError("addonOdoo must use uv --directory <devkit-checkout> run python -m odoo_devkit.addon_ci")
    words = words[3:]
    if not words or words.pop(0) != "run":
        raise ValueError("addonOdoo must invoke uv run")
    if words and words[0] == "--locked":
        words.pop(0)
    if words[:3] != ["python", "-m", "odoo_devkit.addon_ci"]:
        raise ValueError("addonOdoo must invoke odoo_devkit.addon_ci; a runtime update does not run tests")
    words = words[3:]
    options = {"--tenant", "--addons-root", "--support-root", "--output", "--image", "--odoo-version", "--timeout"}
    supplied: dict[str, str] = {}
    while words:
        option = words.pop(0)
        if option not in options or option in supplied or not words:
            raise ValueError(f"Unsupported, repeated or incomplete addonOdoo option: {option}")
        value = words.pop(0)
        if not value or value.startswith("--") or value in {";", "&&", "||", "|", "&"}:
            raise ValueError(f"Missing addonOdoo value for {option}")
        supplied[option] = value
    if not {"--addons-root", "--output"} <= supplied.keys() or not ({"--tenant", "--image"} & supplied.keys()):
        raise ValueError("addonOdoo requires --addons-root, --output and either --tenant or --image")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    arguments = parser.parse_args()
    metadata = json.loads(arguments.metadata.read_text(encoding="utf-8"))
    command = metadata.get("qualityGate", {}).get("test", {}).get("addonOdoo")
    if not isinstance(command, str) or not command.strip():
        parser.error("qualityGate.test.addonOdoo must declare the addon_ci command")
    try:
        check_command(command)
    except ValueError as error:
        parser.error(str(error))
    print("addonOdoo invokes the disposable addon_ci test runner")


if __name__ == "__main__":
    main()
