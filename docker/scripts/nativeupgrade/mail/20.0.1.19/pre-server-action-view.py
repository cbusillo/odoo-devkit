import runpy
from importlib import import_module
from pathlib import Path
from typing import Any


def migrate(cr: Any, version: str | None) -> None:
    get_module_path = import_module("odoo.modules.module").get_module_path

    helpers = runpy.run_path(str(Path(__file__).resolve().parents[3] / "mail_view_20.py"))
    target = Path(get_module_path("mail")) / "views/ir_actions_server_views.xml"
    helpers["migrate_mail"](cr, version, target)
