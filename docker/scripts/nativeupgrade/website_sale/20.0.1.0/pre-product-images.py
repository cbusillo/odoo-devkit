from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any


def migrate(cr: Any, version: str | None) -> None:
    if not version or version.split(".", 1)[0] != "19":
        return
    root = Path(__file__).resolve().parents[3]
    diagnostics = runpy.run_path(str(root / "migration_diagnostics.py"))["MigrationDiagnostics"]()
    install = runpy.run_path(str(root / "product_images_20.py"))["install_bounded_image_conversion"]
    diagnostics.check("Website Sale bounded image loader", install, None)
