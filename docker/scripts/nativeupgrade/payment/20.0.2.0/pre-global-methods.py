from __future__ import annotations

import runpy
from importlib import import_module
from pathlib import Path
from typing import Any


def migrate(cr: Any, version: str | None) -> None:
    if not version or version.split(".", 1)[0] == "20":
        return
    root = Path(__file__).resolve().parents[3]
    module = import_module("odoo.modules.module")
    config = import_module("odoo.tools").config
    views = runpy.run_path(str(root / "mail_view_20.py"))
    target = Path(module.get_module_path("mail")) / "views/ir_actions_server_views.xml"
    profile = views["load_reference"](target)
    helpers = runpy.run_path(str(root / "payment_20.py"))
    references = runpy.run_path(str(root / "prepare_core_references_20.py"))
    diagnostics = runpy.run_path(str(root / "migration_diagnostics.py"))["MigrationDiagnostics"]()
    diagnostics.check(
        "Payment migration preservation",
        lambda: helpers["retire"](cr, profile, version, Path(config.filestore(cr.dbname)), references["_check_references"]),
        None,
        cursor=cr,
    )
