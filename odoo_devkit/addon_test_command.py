"""Lint the declared addon test entrypoint without executing repository commands."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
from pathlib import Path

from .addon_ci import argument_parser


def check_command(command: str) -> None:
    # This is a restricted single-line invocation, not a general shell script.
    if any(character in command for character in "\r\n\0`") or "$(" in command:
        raise ValueError("addonOdoo must be one line without command substitution or control characters")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = list(lexer)
    if any(word and all(character in lexer.punctuation_chars for character in word) for word in words):
        raise ValueError("addonOdoo must not chain shell commands or redirect its result")
    if len(words) < 4 or words[:2] != ["uv", "--directory"]:
        raise ValueError("addonOdoo must use uv --directory <devkit-checkout> run python -m odoo_devkit.addon_ci")
    words = words[3:]
    if not words or words.pop(0) != "run":
        raise ValueError("addonOdoo must invoke uv run")
    if words and words[0] == "--locked":
        words.pop(0)
    if words[:3] != ["python", "-m", "odoo_devkit.addon_ci"]:
        raise ValueError("addonOdoo must invoke odoo_devkit.addon_ci; a runtime update does not run tests")
    # Use the runner's parser so its supported arguments have one source of truth.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            arguments = argument_parser().parse_args(words[3:])
        except SystemExit as error:
            raise ValueError("addonOdoo must supply runnable addon_ci arguments, not help or an incomplete command") from error
    if not (arguments.tenant or arguments.image):
        raise ValueError("addonOdoo requires either --tenant or --image")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    arguments = parser.parse_args()
    try:
        metadata = json.loads(arguments.metadata.read_text(encoding="utf-8"))
        command = metadata.get("qualityGate", {}).get("test", {}).get("addonOdoo")
    except (OSError, ValueError, AttributeError) as error:
        parser.error(f"Cannot read addonOdoo metadata: {error}")
    if not isinstance(command, str) or not command.strip():
        parser.error("qualityGate.test.addonOdoo must declare the addon_ci command")
    try:
        check_command(command)
    except ValueError as error:
        parser.error(str(error))
    print("addonOdoo invokes the disposable addon_ci test runner")


if __name__ == "__main__":
    main()
