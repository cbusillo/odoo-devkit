from __future__ import annotations

import runpy
from importlib import import_module
from pathlib import Path
from typing import Any


def preserve_lead_time(cr: Any) -> None:
    cr.execute("LOCK TABLE product_template,ir_model_fields,ir_default,res_company IN SHARE ROW EXCLUSIVE MODE")
    cr.execute(
        "SELECT id,ttype,store,company_dependent FROM ir_model_fields WHERE model=%s AND name=%s",
        ("product.template", "sale_delay"),
    )
    metadata = cr.fetchall()
    if len(metadata) != 1 or metadata[0][1:3] != ("integer", True):
        raise RuntimeError("Sale lead time has missing or unsupported source metadata")
    field_id, _type, _stored, company_dependent = metadata[0]
    cr.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='product_template' AND column_name='sale_delay'"
    )
    columns = cr.fetchall()
    if columns == [("jsonb",)] and company_dependent:
        return
    if columns != [("integer",)] or company_dependent:
        raise RuntimeError("Sale lead time has an unsupported source column")
    cr.execute("SELECT 1 FROM ir_default WHERE field_id=%s LIMIT 1", (field_id,))
    if cr.fetchall():
        raise RuntimeError("Sale lead time has source defaults requiring preservation")
    cr.execute("SELECT 1 FROM res_company LIMIT 1")
    if not cr.fetchall():
        raise RuntimeError("Sale lead time has no existing company")
    import_module("odoo.upgrade.util").make_field_company_dependent(
        cr, "product.template", "sale_delay", "integer", company_field=None
    )


def migrate(cr: Any, version: str | None) -> None:
    if not version or version.split(".", 1)[0] != "19":
        return
    root = Path(__file__).resolve().parents[3]
    diagnostics = runpy.run_path(str(root / "migration_diagnostics.py"))["MigrationDiagnostics"]()
    diagnostics.check("Sale company-dependent lead time preservation", lambda: preserve_lead_time(cr), None, cursor=cr)
