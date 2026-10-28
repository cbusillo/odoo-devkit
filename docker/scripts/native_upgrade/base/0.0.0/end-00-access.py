import runpy
from pathlib import Path
from typing import Any


def migrate(cr: Any, _version: str | None) -> None:
    owner = Path(__file__).resolve().parents[3] / "prepare_core_references_20.py"
    runpy.run_path(str(owner))["ensure_no_legacy_access_residue"](cr)
