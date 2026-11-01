from __future__ import annotations

from typing import Any

TABLES = {
    "ir_module_module": ("name", "state", "latest_version"),
    "ir_module_module_dependency": ("module_id", "name"),
    "ir_model": ("model", "state", "modules"),
    "ir_model_fields": (
        "model",
        "name",
        "state",
        "ttype",
        "relation",
        "related",
        "depends",
        "compute",
        "store",
        "required",
        "readonly",
        "index",
    ),
    "ir_model_data": ("module", "name", "model", "res_id", "noupdate"),
    "ir_model_fields_selection": ("field_id", "value", "sequence"),
    "ir_ui_view": (
        "model",
        "type",
        "mode",
        "inherit_id",
        "priority",
        "active",
        "arch_db",
        "arch_prev",
        "arch_fs",
        "arch_updated",
        "key",
    ),
    "ir_ui_view_custom": ("ref_id",),
    "ir_ui_view_group_rel": ("view_id", "group_id"),
    "ir_actions": ("type", "path", "binding_model_id", "binding_type", "binding_view_types"),
    "ir_act_window": ("res_model", "view_id", "search_view_id", "view_mode", "context", "domain", "res_id", "path"),
    "ir_act_window_view": ("act_window_id", "view_id", "view_mode", "sequence"),
    "ir_act_server": ("model_id", "state", "code", "crud_model_id", "update_path", "path"),
    "ir_act_report_xml": ("model", "report_type", "report_name", "report_file", "path"),
    "ir_act_client": ("tag", "context", "path"),
    "ir_act_url": ("url", "target", "path"),
    "ir_ui_menu": ("parent_id", "action", "active", "sequence"),
    "ir_filters": ("model_id", "domain", "context", "action_id", "active"),
    "ir_default": ("field_id", "condition"),
    "base_automation": ("model_id", "trigger", "filter_pre_domain", "filter_domain", "active"),
    "mail_template": ("model_id", "report_template_ids", "lang"),
}


def extract_private_technical(cursor: Any, binding: str) -> dict[str, Any]:
    from psycopg2 import sql

    if not binding:
        raise ValueError("Source binding is required before root extraction")
    cursor.execute("SET TRANSACTION READ ONLY")
    extracted: dict[str, Any] = {"binding": binding, "screened": False, "tables": {}}
    for table, allowlist in TABLES.items():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        present = {row[0] for row in cursor.fetchall()}
        if not present:
            extracted["tables"][table] = {"count": None, "gap": "table-unavailable"}
            continue
        selected = [column for column in ("id", *allowlist) if column in present]
        columns = sql.SQL(", ").join(map(sql.Identifier, selected))
        if not selected:
            extracted["tables"][table] = {"count": None, "gap": "structural-columns-unavailable"}
            continue
        order = sql.Identifier("id" if "id" in present else selected[0])
        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(columns, sql.Identifier(table), order)
        cursor.execute(query)
        rows = [dict(zip(selected, row, strict=True)) for row in cursor.fetchall()]
        extracted["tables"][table] = {"count": len(rows), "rows": rows, "withheld_columns": sorted(set(allowlist) - present)}
    return extracted
