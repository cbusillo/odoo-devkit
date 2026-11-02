from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

TABLES = {
    "ir_module_module": ("name", "state", "latest_version"),
    "ir_module_module_dependency": ("module_id", "name"),
    "ir_model": ("model", "state", "modules"),
    "ir_model_inherit": ("model_id", "parent_id", "parent_field_id"),
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
        "modules",
        "index",
        "domain",
        "on_delete",
        "size",
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
        "website_id",
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
    "ir_cron": ("ir_actions_server_id", "active", "priority"),
    "mail_template": ("model_id", "report_template_ids", "lang"),
}
TECHNICAL_XMLID_MODELS = {
    "ir.module.module",
    "ir.model",
    "ir.model.fields",
    "ir.model.fields.selection",
    "ir.model.access",
    "ir.rule",
    "ir.model.constraint",
    "ir.model.relation",
    "ir.ui.view",
    "ir.ui.menu",
    "res.groups",
    "res.lang",
    "ir.filters",
    "ir.default",
    "base.automation",
    "ir.cron",
    "mail.template",
    "ir.actions.actions",
    "ir.actions.act_window",
    "ir.actions.act_window.view",
    "ir.actions.act_window_close",
    "ir.actions.server",
    "ir.actions.report",
    "ir.actions.client",
    "ir.actions.act_url",
}


def extract_private_technical(cursor: Any, binding: str) -> dict[str, Any]:
    from psycopg2 import sql

    if not binding:
        raise ValueError("Source binding is required before root extraction")
    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    extracted: dict[str, Any] = {"binding": binding, "screened": False, "tables": {}}
    technical_models = set(TECHNICAL_XMLID_MODELS)
    for table in [name for name in TABLES if name != "ir_model_data"] + ["ir_model_data"]:
        allowlist = TABLES[table]
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        present = {row[0] for row in cursor.fetchall()}
        if not present:
            extracted["tables"][table] = {"count": None, "gap": "table-unavailable"}
            continue
        selected = [column for column in ("id", *allowlist) if column in present]
        output_names = []
        expressions = []
        for column in selected:
            alias = "history_present" if column == "arch_prev" else "url_present" if column == "url" else column
            output_names.append(alias)
            if column in {"arch_prev", "code", "compute", "url"}:
                expressions.append(
                    sql.SQL("({} IS NOT NULL AND {}::text NOT IN ('', '{{}}', 'false')) AS {}").format(
                        sql.Identifier(column), sql.Identifier(column), sql.Identifier(alias)
                    )
                )
            else:
                expressions.append(sql.Identifier(column))
        columns = sql.SQL(", ").join(expressions)
        if not selected:
            extracted["tables"][table] = {"count": None, "gap": "structural-columns-unavailable"}
            continue
        order = sql.Identifier("id" if "id" in present else selected[0])
        predicate = sql.SQL(" WHERE model = ANY(%s)") if table == "ir_model_data" else sql.SQL("")
        parameters = (sorted(technical_models),) if table == "ir_model_data" else None
        query = sql.SQL("SELECT {} FROM {}{} ORDER BY {}").format(columns, sql.Identifier(table), predicate, order)
        cursor.execute(query, parameters)
        rows = [dict(zip(output_names, row, strict=True)) for row in cursor.fetchall()]
        extracted["tables"][table] = {"count": len(rows), "rows": rows, "withheld_columns": sorted(set(allowlist) - present)}
        if table == "ir_actions":
            technical_models.update(row["type"] for row in rows if row.get("type"))
        if table == "ir_model_data":
            cursor.execute("SELECT count(*) FROM ir_model_data WHERE NOT (model = ANY(%s))", parameters)
            extracted["out_of_scope"] = {"nontechnical_xmlids": cursor.fetchone()[0]}
    return extracted


def project_private_inventory(extracted: dict[str, Any]) -> dict[str, Any]:
    from odoo_structural_census import BUCKETS

    tables = extracted["tables"]
    records: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in BUCKETS}
    totals: dict[str, int | None] = {}
    aliases: dict[tuple[str, int], list[dict[str, Any]]] = {}
    technical_models = TECHNICAL_XMLID_MODELS | {row["type"] for row in tables.get("ir_actions", {}).get("rows", [])}
    excluded_xmlids = 0
    for row in tables.get("ir_model_data", {}).get("rows", []):
        if row["model"] not in technical_models:
            excluded_xmlids += 1
            continue
        xmlid = row["module"] + "." + row["name"]
        aliases.setdefault((row["model"], row["res_id"]), []).append({"xmlid": xmlid, "noupdate": row.get("noupdate")})
        records["xmlids"].append(
            {"key": xmlid, "record_model": row["model"], "owners": [] if row["module"].startswith("__") else [row["module"]]}
        )
    action_models = {row["type"] for row in tables.get("ir_actions", {}).get("rows", [])} | {
        model for model in technical_models if model.startswith("ir.actions.") and model != "ir.actions.act_window.view"
    }
    action_aliases: dict[str, set[str]] = {}
    for (model, record_id), bindings in aliases.items():
        if model in action_models:
            action_aliases.setdefault(str(record_id), set()).update(item["xmlid"] for item in bindings)

    def base(source_row: dict[str, Any], record_model: str, prefix: str) -> dict[str, Any]:
        owned_bindings = aliases.get((record_model, source_row["id"]), [])
        return {
            "key": prefix + ":" + str(source_row["id"]),
            "xmlids": [item["xmlid"] for item in owned_bindings],
            "noupdate_by_xmlid": {item["xmlid"]: item["noupdate"] for item in owned_bindings},
            "noupdate": all(item["noupdate"] for item in owned_bindings) if owned_bindings else None,
            "owners": sorted(
                {item["xmlid"].split(".")[0] for item in owned_bindings if not item["xmlid"].startswith("__")}
                | set((source_row.get("modules") or "").split(",")) - {""}
            ),
        }

    model_names = {row["id"]: row["model"] for row in tables.get("ir_model", {}).get("rows", [])}
    field_rows = {row["id"]: row for row in tables.get("ir_model_fields", {}).get("rows", [])}
    selections: dict[int, list[list[str]]] = {}
    for row in sorted(
        tables.get("ir_model_fields_selection", {}).get("rows", []),
        key=lambda item: (item["field_id"], item.get("sequence", 0), item["id"]),
    ):
        selections.setdefault(row["field_id"], []).append([row["value"], row["value"]])
    inherited: dict[int, list[dict[str, Any]]] = {}
    for row in tables.get("ir_model_inherit", {}).get("rows", []):
        inherited.setdefault(row["model_id"], []).append({"kind": "model", "name": model_names.get(row["parent_id"], "")})
    for row in tables.get("ir_module_module", {}).get("rows", []):
        records["modules"].append({"key": row["name"], "state": row["state"], "installed_version": row.get("latest_version")})
    for row in tables.get("ir_model", {}).get("rows", []):
        model = {
            **base(row, "ir.model", "model"),
            "model": row["model"],
            "state": row.get("state"),
            "references": inherited.get(row["id"], []),
        }
        if model["references"] and model["state"] == "manual":
            model["withheld"] = ["manual-inheritance-native-baseline"]
        records["models"].append(model)
    for row in field_rows.values():
        values = {key: value for key, value in row.items() if key != "id"}
        values["depends"] = [part.strip() for part in (row.get("depends") or "").split(",") if part.strip()]
        if row["id"] in selections:
            values["selection"] = selections[row["id"]]
            values["minimized"] = ["selection-labels"]
        records["fields"].append({**base(row, "ir.model.fields", "field"), **values})
    custom = {row["ref_id"] for row in tables.get("ir_ui_view_custom", {}).get("rows", [])}
    groups: dict[int, list[str]] = {}
    unbound_groups: set[int] = set()
    for row in tables.get("ir_ui_view_group_rel", {}).get("rows", []):
        bindings = aliases.get(("res.groups", row["group_id"]), [])
        if bindings:
            groups.setdefault(row["view_id"], []).append(bindings[0]["xmlid"])
        else:
            unbound_groups.add(row["view_id"])
    for row in tables.get("ir_ui_view", {}).get("rows", []):
        values = {key: value for key, value in row.items() if key not in {"id", "inherit_id", "arch_db", "key", "website_id"}}
        architecture = row.get("arch_db")
        values["architectures"] = architecture if isinstance(architecture, dict) else {"en_US": architecture} if architecture else {}
        values.update(source_order=row["id"], customization_present=row["id"] in custom, groups=groups.get(row["id"], []))
        if row.get("inherit_id"):
            values["parent"] = "view:" + str(row["inherit_id"])
        if row.get("website_id"):
            values["website_scope"] = "website:" + str(row["website_id"])
        if row.get("key"):
            values["view_key"] = row["key"]
        if row["id"] in unbound_groups:
            values["withheld"] = ["unbound-group-identity"]
        projected = {**base(row, "ir.ui.view", "view"), **values}
        action_ids: set[str] = set()
        for architecture in values["architectures"].values():
            try:
                elements = ElementTree.fromstring(architecture).iter("button")
                action_ids.update(
                    element.get("name", "")
                    for element in elements
                    if element.get("type") == "action" and element.get("name", "").isdigit()
                )
            except (ElementTree.ParseError, TypeError):
                continue
        projected["action_bindings"] = {action_id: sorted(action_aliases.get(action_id, set())) for action_id in sorted(action_ids)}
        records["views"].append(projected)
        if row.get("type") == "qweb":
            records["qweb"].append({"key": projected["key"], "source_view": projected["key"]})
    action_tables = {
        "ir.actions.act_window": "ir_act_window",
        "ir.actions.server": "ir_act_server",
        "ir.actions.report": "ir_act_report_xml",
        "ir.actions.client": "ir_act_client",
        "ir.actions.act_url": "ir_act_url",
    }
    details = {kind: {row["id"]: row for row in tables.get(table, {}).get("rows", [])} for kind, table in action_tables.items()}
    action_views: dict[int, list[dict[str, Any]]] = {}
    for row in tables.get("ir_act_window_view", {}).get("rows", []):
        action_views.setdefault(row["act_window_id"], []).append(row)
    for row in tables.get("ir_actions", {}).get("rows", []):
        kind = row["type"]
        values = {**row, **details.get(kind, {}).get(row["id"], {})}
        if kind in details and row["id"] not in details[kind]:
            values["withheld"] = ["action-class-structure-unavailable"]
        values.pop("id", None)
        values["model"] = values.pop("res_model", None) or values.get("model") or model_names.get(values.get("model_id"), "")
        view_refs = [values.get("view_id"), values.get("search_view_id")]
        view_refs += [item.get("view_id") for item in action_views.get(row["id"], [])]
        values["view_references"] = ["view:" + str(value) for value in view_refs if value]
        values["view_modes"] = [item.get("view_mode") for item in action_views.get(row["id"], [])]
        records["actions"].append({**base(row, kind, "action"), **values})
    auxiliary = {
        "menus": "ir_ui_menu",
        "filters": "ir_filters",
        "defaults": "ir_default",
        "automation": "base_automation",
        "templates": "mail_template",
    }
    for bucket, table in auxiliary.items():
        for row in tables.get(table, {}).get("rows", []):
            values = {key: value for key, value in row.items() if key != "id"}
            model = values.get("model_id")
            values["model"] = model if isinstance(model, str) else model_names.get(model, "")
            if bucket == "defaults" and row.get("field_id") in field_rows:
                field = field_rows[row["field_id"]]
                values["references"] = [{"kind": "field", "model": field["model"], "name": field["name"]}]
            if bucket == "menus" and row.get("action"):
                action_model, separator, action_id = row["action"].partition(",")
                owned = aliases.get((action_model, int(action_id)), []) if separator and action_id.isdigit() else []
                values["references"] = (
                    [{"kind": "xmlid", "name": owned[0]["xmlid"]}] if owned else [{"kind": "unbound-action", "name": ""}]
                )
            if bucket == "templates" and values.get("lang"):
                values["expression"] = values.pop("lang")
            records[bucket].append({"key": bucket + ":" + str(row["id"]), **values})
    for row in tables.get("ir_cron", {}).get("rows", []):
        action_id = row.get("ir_actions_server_id")
        bindings = aliases.get(("ir.actions.server", action_id), [])
        values = {**base(row, "ir.cron", "cron"), **{name: row[name] for name in ("active", "priority") if name in row}}
        if action_id:
            values["source_action"] = "action:" + str(action_id)
            values["references"] = (
                [{"kind": "xmlid", "name": item["xmlid"]} for item in bindings]
                if bindings
                else [{"kind": "unbound-action", "name": ""}]
            )
        else:
            values["withheld"] = ["scheduled-action-identity"]
        records["automation"].append(values)
    sources = {
        "modules": "ir_module_module",
        "models": "ir_model",
        "fields": "ir_model_fields",
        "xmlids": "ir_model_data",
        "views": "ir_ui_view",
        "actions": "ir_actions",
        **auxiliary,
    }
    for bucket, table in sources.items():
        totals[bucket] = tables.get(table, {}).get("count")
    automation_totals = {table: tables.get(table, {}).get("count") for table in ("base_automation", "ir_cron")}
    totals["automation"] = (
        sum(automation_totals.values()) if all(isinstance(value, int) for value in automation_totals.values()) else None
    )
    if totals["xmlids"] is not None:
        totals["xmlids"] -= excluded_xmlids
    totals["qweb"] = len(records["qweb"]) if totals.get("views") is not None else None
    action_totals: dict[str, int] = {}
    for row in records["actions"]:
        action_totals[row["type"]] = action_totals.get(row["type"], 0) + 1
    nontechnical_xmlids = extracted.get("out_of_scope", {}).get("nontechnical_xmlids")
    return {
        "binding": extracted["binding"],
        "screened": False,
        "records": records,
        "totals": totals,
        "action_totals": action_totals,
        "automation_totals": automation_totals,
        "out_of_scope": {
            "nontechnical_xmlids": None
            if totals["xmlids"] is None or nontechnical_xmlids is None
            else nontechnical_xmlids + excluded_xmlids
        },
        "schema_gaps": {
            table: {key: value for key, value in values.items() if key in {"gap", "withheld_columns"}}
            for table, values in tables.items()
            if values.get("gap") or values.get("withheld_columns")
        },
    }
