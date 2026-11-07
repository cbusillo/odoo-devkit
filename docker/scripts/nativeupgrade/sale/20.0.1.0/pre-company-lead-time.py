from __future__ import annotations

import runpy
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
        "SELECT data_type,column_default FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='product_template' AND column_name='sale_delay'"
    )
    columns = cr.fetchall()
    if len(columns) == 1 and columns[0][0] == "jsonb" and company_dependent:
        return
    if columns != [("integer", None)] or company_dependent:
        raise RuntimeError("Sale lead time has an unsupported source column")
    cr.execute("SELECT 1 FROM ir_default WHERE field_id=%s LIMIT 1", (field_id,))
    if cr.fetchall():
        raise RuntimeError("Sale lead time has source defaults requiring preservation")
    cr.execute("SELECT id FROM res_company ORDER BY id")
    companies = cr.fetchall()
    if not companies:
        raise RuntimeError("Sale lead time has no existing company")
    # The upstream bulk converter commits, which would destroy the diagnostic savepoint.
    values = " || ".join("jsonb_build_object(%s,sale_delay)" for _ in companies)
    cr.execute(
        "ALTER TABLE product_template ALTER COLUMN sale_delay TYPE jsonb "
        f"USING CASE WHEN sale_delay IS NULL THEN NULL ELSE {values} END",
        tuple(str(company_id) for (company_id,) in companies),
    )
    cr.execute("UPDATE ir_model_fields SET company_dependent=TRUE WHERE id=%s", (field_id,))


def migrate(cr: Any, version: str | None) -> None:
    if not version or version.split(".", 1)[0] != "19":
        return
    root = Path(__file__).resolve().parents[3]
    diagnostics = runpy.run_path(str(root / "migration_diagnostics.py"))["MigrationDiagnostics"]()
    diagnostics.check("Sale company-dependent lead time preservation", lambda: preserve_lead_time(cr), None, cursor=cr)
