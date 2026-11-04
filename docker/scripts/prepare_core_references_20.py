# Copyright 2026 Hunki Enterprises BV (_release_policy_xmlids only)
# License AGPL-3.0 or later for _release_policy_xmlids (https://www.gnu.org/licenses/agpl).

import csv
import json
import logging
import runpy
from ast import literal_eval
from datetime import datetime
from importlib import import_module
from math import isfinite
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple
from xml.etree import ElementTree

SOURCE_STATE = "state_in_or"
TARGET_STATE = "state_in_od"
STATE_DATA_FILE = Path("/odoo/odoo/addons/base/data/res.country.state.csv")
STOCK_VALUATION_DATA_FILE = Path("/odoo/addons/account/report/account_stock_valuation_report.xml")
STOCK_PRODUCT_DATA_FILES = {
    "stock": Path("/odoo/addons/stock/views/product_views.xml"),
    "stock_account": Path("/odoo/addons/stock_account/views/product_views.xml"),
    "stock_delivery": Path("/odoo/addons/stock_delivery/views/product_template_view.xml"),
}
HR_ORG_CHART_DATA_FILE = Path("/odoo/addons/hr/views/hr_employee_public_views.xml")
HR_EMPLOYEE_DATA_FILE = Path("/odoo/addons/hr/views/hr_employee_views.xml")
HR_DEPARTMENT_DATA_FILE = Path("/odoo/addons/hr/views/hr_department_views.xml")
HR_USERS_DATA_FILE = Path("/odoo/addons/hr/views/res_users.xml")
HR_TYPE_DATA_FILE = Path("/odoo/addons/hr/data/hr_employee_type_data.xml")
HR_TYPE_FIELD_OWNERS = ("hr.job", "hr.version", "hr.employee")
UNTRACKED_PUBLIC_VIEW_DEFAULTS = {"track": (None, False), "visibility": (None, "")}
_logger = logging.getLogger(__name__)


class Reference(NamedTuple):
    model: str
    record_id: int
    noupdate: bool


class DirectChat(NamedTuple):
    values: dict[str, Any]
    members: list[dict[str, Any]]


class HrTypePlan(NamedTuple):
    utility: ModuleType | None
    employee_type_id: int | None
    version_ids: list[int]


class InheritedView(NamedTuple):
    module: str
    name: str
    model: str
    parent: str
    source_selector: str
    target_selector: str


class StockView(NamedTuple):
    module: str
    source: str
    target: str
    source_file: str


STOCK_VIEWS = (
    StockView("stock", "view_template_property_form", "product_template_form_view_stock", "stock/views/product_views.xml"),
    StockView(
        "stock_account",
        "view_template_property_form_stock_account",
        "product_template_form_view_stock_account",
        "stock_account/views/product_views.xml",
    ),
    StockView(
        "stock_delivery", "product_template_hs_code", "product_template_hs_code", "stock_delivery/views/product_template_view.xml"
    ),
)


INHERITED_VIEWS = (
    InheritedView(
        "account",
        "view_partner_bank_form_inherit_account",
        "res.partner.bank",
        "view_partner_bank_form",
        "expr=\"//field[@name='acc_number']\"",
        "expr=\"//field[@name='account_number']\"",
    ),
    InheritedView(
        "hr_calendar",
        "view_res_partner_filter_inherit_calendar",
        "res.partner",
        "view_res_partner_filter",
        '<filter name="type_company" position="after">',
        '<filter name="filter_vat_set" position="after">',
    ),
)


def _reference(cr: Any, module: str, name: str) -> Reference | None:
    cr.execute("SELECT model, res_id, noupdate FROM ir_model_data WHERE module = %s AND name = %s FOR UPDATE", (module, name))
    rows = cr.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"Ambiguous core reference {module}.{name}")
    return Reference(*rows[0]) if rows else None


def _require_source(cr: Any, module: str, name: str, model: str) -> Reference:
    reference = _reference(cr, module, name)
    if reference is None or reference.model != model:
        raise RuntimeError(f"Unsupported core reference {module}.{name}: expected {model}")
    return reference


def _check_target(cr: Any, name: str, source: Reference, *, protect: bool = False, module: str = "base") -> Reference | None:
    target = _reference(cr, module, name)
    if target is not None and (
        target.model != source.model or target.record_id != source.record_id or (protect and not target.noupdate)
    ):
        raise RuntimeError(f"Conflicting core reference {module}.{name}")
    return target


def _state_source(cr: Any) -> Reference:
    with STATE_DATA_FILE.open(newline="") as stream:
        targets = [row for row in csv.DictReader(stream) if row["id"] == TARGET_STATE]
    if len(targets) != 1:
        raise RuntimeError("Target state data does not identify one supported reference")
    target = targets[0]
    country_name = target["country_id:id"].removeprefix("base.")
    country = _require_source(cr, "base", country_name, "res.country")
    source = _require_source(cr, "base", SOURCE_STATE, "res.country.state")
    cr.execute(
        "SELECT state.name FROM res_country_state state JOIN res_country country ON country.id = state.country_id "
        "WHERE state.id = %s AND country.id = %s AND country.code = %s AND state.code = %s FOR UPDATE",
        (source.record_id, country.record_id, country_name.upper(), target["code"]),
    )
    rows = cr.fetchall()
    if len(rows) != 1 or (rows[0][0] != target["name"] and not source.noupdate):
        raise RuntimeError("Unsupported or customized source state; native data must not overwrite it")
    return source


def _website_source(cr: Any) -> Reference | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'website'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    source = _require_source(cr, "website", "default_website", "website")
    cr.execute("SELECT id FROM website WHERE id = %s FOR UPDATE", (source.record_id,))
    if cr.fetchone() is None or not source.noupdate:
        raise RuntimeError("Unsupported source website or metadata; native defaults must not overwrite it")
    return source


def _bind_target(cr: Any, name: str, source: Reference, module: str = "base") -> None:
    cr.execute(
        "INSERT INTO ir_model_data (module, name, model, res_id, noupdate) VALUES (%s, %s, %s, %s, %s)",
        (module, name, source.model, source.record_id, source.noupdate),
    )
    _logger.info("Bound core reference %s.%s to preserved %s row %s", module, name, source.model, source.record_id)


def _action_aliases(cr: Any, record_id: int) -> set[tuple[str, str, str]]:
    cr.execute(
        "SELECT module, name, model FROM ir_model_data WHERE model IN ('ir.actions.actions','ir.actions.act_window',"
        "'ir.actions.act_window_close','ir.actions.act_url','ir.actions.server','ir.actions.client','ir.actions.report') "
        "AND res_id = %s FOR UPDATE",
        (record_id,),
    )
    return set(cr.fetchall())


def _stock_valuation_action_plan(cr: Any) -> Reference | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'stock_account'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    name = "action_report_stock_valuation"
    source = _require_source(cr, "stock_account", name, "ir.actions.client")
    target = _check_target(cr, name, source, module="account")
    if (target is not None and target.noupdate) or (source.noupdate and target is None):
        raise RuntimeError("Protected stock valuation action ownership")
    document = ElementTree.parse(STOCK_VALUATION_DATA_FILE).getroot()
    records = document.findall(f"./record[@id='{name}']")
    if document.tag != "odoo" or document.attrib or len(records) != 1 or records[0].attrib != {"id": name, "model": source.model}:
        raise RuntimeError("Unsupported native stock valuation action definition")
    native = {field.attrib["name"]: field.text or "" for field in records[0].findall("field")}
    if set(native) != {"name", "tag", "res_model", "path", "context"} or not all(native.values()):
        raise RuntimeError("Unsupported native stock valuation action fields")
    actions = _rows(cr, "SELECT * FROM ir_act_client WHERE id = %s FOR UPDATE", (source.record_id,))
    if len(actions) != 1:
        raise RuntimeError("Missing stock valuation action")
    action = actions[0]
    cr.execute(
        "SELECT tableoid::regclass::text, id FROM ir_actions WHERE id = %s OR path = %s FOR UPDATE",
        (source.record_id, native["path"]),
    )
    if cr.fetchall() != [("ir_act_client", source.record_id)]:
        raise RuntimeError("Ambiguous stock valuation action storage or path")
    aliases = {("stock_account", name, source.model)} | ({("account", name, source.model)} if target is not None else set())
    if _action_aliases(cr, source.record_id) != aliases:
        raise RuntimeError("Customized stock valuation action aliases")
    try:
        context = literal_eval(action["context"])
        native_context = literal_eval(native["context"])
    except (ValueError, SyntaxError, TypeError) as error:
        raise RuntimeError("Unsupported stock valuation action context") from error
    old_model = "stock_account.stock.valuation.report"
    if native_context != {"model": native["res_model"]}:
        raise RuntimeError("Unsupported native stock valuation report model")
    if action["res_model"] != old_model or context != {"model": old_model}:
        raise RuntimeError("Customized stock valuation report model or context")
    _require_empty(
        action,
        {"id", "create_uid", "write_uid", "create_date", "write_date", "res_model", "context"},
        {
            "name": ({"en_US": native["name"]},),
            "type": (source.model,),
            "tag": (native["tag"],),
            "path": (native["path"],),
            "target": ("current",),
            "binding_type": ("action",),
            "binding_view_types": ("list,form",),
        },
    )
    return source


def _bind_moved_action(cr: Any, source: Reference, name: str, source_module: str, target_module: str) -> None:
    if _reference(cr, target_module, name) is None:
        _bind_target(cr, name, Reference(source.model, source.record_id, False), module=target_module)
    cr.execute("UPDATE ir_model_data SET noupdate = true WHERE module = %s AND name = %s", (source_module, name))


def _hr_native_fields(path: Path, name: str, model: str) -> dict[str, ElementTree.Element]:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    document = ElementTree.parse(path, parser=parser).getroot()
    records = document.findall(f".//record[@id='{name}']")
    if document.tag != "odoo" or len(records) != 1 or records[0].attrib != {"id": name, "model": model}:
        raise RuntimeError("Unsupported native record definition")
    if document.attrib or any(parent.attrib for parent in document.iter("data")):
        raise RuntimeError("Protected native record data")
    fields = {field.attrib.get("name", ""): field for field in records[0]}
    if len(fields) != len(records[0]):
        raise RuntimeError("Duplicate native record fields")
    return fields


def _stock_product_view_plan(cr: Any) -> dict[tuple[str, str], Reference]:
    cr.execute("SELECT name, state FROM ir_module_module WHERE name IN (%s, %s, %s)", tuple(view.module for view in STOCK_VIEWS))
    states = dict(cr.fetchall())
    if states.get("stock") not in {"installed", "to upgrade"}:
        return {}
    selected = [view for view in STOCK_VIEWS if states.get(view.module) in {"installed", "to upgrade"}]
    cr.execute("LOCK TABLE ir_ui_view, ir_ui_view_custom, ir_ui_view_group_rel, ir_model_data IN SHARE ROW EXCLUSIVE MODE")
    sources = {view.module: _require_source(cr, view.module, view.source, "ir.ui.view") for view in selected}
    ancestor = _require_source(cr, "product", "product_template_form_view", "ir.ui.view")
    if any(source.noupdate for source in [ancestor, *sources.values()]):
        raise RuntimeError("Protected Stock product view ownership")
    if len({source.record_id for source in [ancestor, *sources.values()]}) != len(sources) + 1:
        raise RuntimeError("Overlapping Stock product view identities")
    cr.execute("SELECT model, type, active FROM ir_ui_view WHERE id = %s FOR UPDATE", (ancestor.record_id,))
    if cr.fetchall() != [("product.template", "form", True)]:
        raise RuntimeError("Unsupported Stock product view ancestor")
    action = _require_source(cr, "stock", "action_open_routes", "ir.actions.server")
    cr.execute("SELECT id FROM ir_act_server WHERE id = %s", (action.record_id,))
    if cr.fetchone() is None:
        raise RuntimeError("Missing Stock routes action")
    helpers = runpy.run_path(str(Path(__file__).with_name("mail_view_20.py")))
    normalize = helpers["_normalized_architecture"]
    render = helpers["_render_architecture"]
    plan = {}
    for view in selected:
        source = sources[view.module]
        target = _check_target(cr, view.target, source, module=view.module)
        if target is not None and target.noupdate:
            raise RuntimeError("Protected native Stock product view target")
        legacy = Path(__file__).with_name("legacy") / view.module
        current_series = ("19", "19_early") if view.module == "stock" else ("19",)
        current = [_hr_native_fields(legacy / f"product_views_{series}.xml", view.source, "ir.ui.view") for series in current_series]
        previous = current + (
            [_hr_native_fields(legacy / "product_views_18.xml", view.source, "ir.ui.view")]
            if view.module != "stock_delivery"
            else []
        )
        fields = current[0]
        if view.source != view.target and any(
            record.get("id") in {view.source, f"{view.module}.{view.source}"}
            for record in ElementTree.parse(STOCK_PRODUCT_DATA_FILES[view.module]).iter("record")
        ):
            raise RuntimeError("Native Stock data still declares a retired view identity")
        native = _hr_native_fields(STOCK_PRODUCT_DATA_FILES[view.module], view.target, "ir.ui.view")
        parent_name = "product.product_template_form_view" if view.module == "stock" else "stock.product_template_form_view_stock"
        if (
            set(native) != {"name", "model", "inherit_id", "arch"}
            or native["name"].attrib != {"name": "name"}
            or native["name"].text != fields["name"].text
            or native["model"].attrib != {"name": "model"}
            or native["model"].text != fields["model"].text
            or native["inherit_id"].attrib != {"name": "inherit_id", "ref": parent_name}
            or native["arch"].attrib != {"name": "arch", "type": "xml"}
            or not len(native["arch"])
        ):
            raise RuntimeError("Changed native Stock product view definition")
        cr.execute(
            "SELECT module, name, noupdate FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s", (source.record_id,)
        )
        aliases = {(view.module, view.source, False)}
        if target is not None:
            aliases.add((view.module, view.target, False))
        if set(cr.fetchall()) != aliases:
            raise RuntimeError("Customized Stock product view aliases")
        expected_children = (
            {value.record_id for module, value in sources.items() if module != "stock"} if view.module == "stock" else set()
        )
        cr.execute("SELECT id FROM ir_ui_view WHERE inherit_id = %s", (source.record_id,))
        if {row[0] for row in cr.fetchall()} != expected_children:
            raise RuntimeError("Preserve outside Stock product view descendants")
        for query in (
            "SELECT 1 FROM ir_ui_view_custom WHERE ref_id = %s LIMIT 1",
            "SELECT 1 FROM ir_ui_view_group_rel WHERE view_id = %s LIMIT 1",
        ):
            cr.execute(query, (source.record_id,))
            if cr.fetchone():
                raise RuntimeError("Customized or restricted Stock product view")
        rows = _rows(cr, "SELECT * FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
        if len(rows) != 1:
            raise RuntimeError("Missing Stock product view")
        row = rows[0]
        _require_empty(
            row,
            {"id", "arch_db", "arch_prev", "create_uid", "write_uid", "create_date", "write_date"},
            {
                "name": (fields["name"].text,),
                "model": ("product.template",),
                "inherit_id": (ancestor.record_id if view.module == "stock" else sources["stock"].record_id,),
                "type": ("form",),
                "mode": ("extension",),
                "active": (True,),
                "priority": (16,),
                "arch_updated": (False,),
                "arch_fs": (view.source_file,),
                "customize_show": (False,),
                "is_seo_optimized": (False,),
                **UNTRACKED_PUBLIC_VIEW_DEFAULTS,
            },
        )
        architectures = row["arch_db"]
        if not isinstance(architectures, dict) or set(architectures) != {"en_US"}:
            raise RuntimeError("Preserve translated Stock product view")
        try:
            admitted = []
            for candidate in previous:
                architecture = ElementTree.tostring(candidate["arch"], encoding="unicode").replace(
                    "%(action_open_routes)d", str(action.record_id)
                )
                parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
                admitted.append(normalize(render(ElementTree.fromstring(architecture, parser=parser))))
            if normalize(architectures["en_US"]) not in admitted[: len(current)]:
                raise RuntimeError("Preserve customized Stock product view architecture")
            if row.get("arch_prev") and normalize(row["arch_prev"]) not in admitted:
                raise RuntimeError("Preserve customized Stock product view history")
        except (ElementTree.ParseError, TypeError, ValueError) as error:
            raise RuntimeError("Unsupported Stock product view architecture") from error
        if target is None:
            plan[view.module, view.target] = source
    return plan


def _hr_org_chart_action_plan(cr: Any, *, private: bool = False) -> Reference | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'hr_org_chart'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'hr'")
    if cr.fetchone() not in {("installed",), ("to upgrade",)}:
        raise RuntimeError("Missing native HR owner for Org Chart action")
    name = "action_hr_employee_org_chart" if private else "action_hr_employee_public_org_chart"
    source = _require_source(cr, "hr_org_chart", name, "ir.actions.act_window")
    target = _check_target(cr, name, source, module="hr")
    if (target is not None and target.noupdate) or (source.noupdate and target is None):
        raise RuntimeError("Protected Org Chart action ownership")
    fields = _hr_native_fields(HR_EMPLOYEE_DATA_FILE if private else HR_ORG_CHART_DATA_FILE, name, source.model)
    expected = {"name", "res_model", "path", "view_mode", "mobile_view_mode", "domain", "context", "view_id", "help"}
    if private:
        expected.add("search_view_id")
    if set(fields) != expected:
        raise RuntimeError("Unsupported native Org Chart action fields")
    native = {key: field.text or "" for key, field in fields.items()}
    if (
        native["res_model"] != ("hr.employee" if private else "hr.employee.public")
        or native["path"] != ("org-chart" if private else "org-chart-public")
        or native["view_mode"]
        != ("hierarchy,kanban,list,form,activity,graph,pivot" if private else "hierarchy,kanban,list,form,graph,pivot")
        or native["mobile_view_mode"] != "hierarchy"
        or not native["name"]
        or fields["view_id"].attrib != {"name": "view_id", "eval": "False"}
        or len(fields["view_id"])
        or (fields["view_id"].text or "").strip()
        or fields["help"].attrib != {"name": "help", "type": "html"}
        or not len(fields["help"])
        or any(
            field.attrib != {"name": key} or len(field)
            for key, field in fields.items()
            if key not in {"help", "view_id", "search_view_id"}
        )
    ):
        raise RuntimeError("Changed native Org Chart action contract")
    search_view_id = None
    if private:
        search_field = fields["search_view_id"]
        if (
            search_field.attrib != {"name": "search_view_id", "ref": "hr.view_employee_filter"}
            or len(search_field)
            or (search_field.text or "").strip()
        ):
            raise RuntimeError("Changed native private Org Chart search binding")
        search = _require_source(cr, "hr", "view_employee_filter", "ir.ui.view")
        cr.execute("SELECT model,type FROM ir_ui_view WHERE id = %s FOR UPDATE", (search.record_id,))
        if cr.fetchall() != [(native["res_model"], "search")]:
            raise RuntimeError("Missing or rebound private Org Chart search view")
        search_view_id = search.record_id
    actions = _rows(cr, "SELECT * FROM ir_act_window WHERE id = %s FOR UPDATE", (source.record_id,))
    if len(actions) != 1:
        raise RuntimeError("Missing Org Chart action")
    action = actions[0]
    cr.execute(
        "SELECT tableoid::regclass::text, id FROM ir_actions WHERE id = %s OR path = %s FOR UPDATE",
        (source.record_id, native["path"]),
    )
    if cr.fetchall() != [("ir_act_window", source.record_id)]:
        raise RuntimeError("Ambiguous Org Chart action storage or path")
    aliases = {("hr_org_chart", name, source.model)} | ({("hr", name, source.model)} if target is not None else set())
    if _action_aliases(cr, source.record_id) != aliases:
        raise RuntimeError("Customized Org Chart action aliases")
    for query in (
        "SELECT 1 FROM ir_act_window_group_rel WHERE act_id = %s LIMIT 1",
        "SELECT 1 FROM ir_act_window_view WHERE act_window_id = %s LIMIT 1",
    ):
        cr.execute(query, (source.record_id,))
        if cr.fetchone():
            raise RuntimeError("Restricted or customized Org Chart action views")
    try:
        for value in (action["context"], native["context"]):
            context = literal_eval(value)
            if not isinstance(context, dict) or set(context) != {"chat_icon"} or context["chat_icon"] is not True:
                raise ValueError("Changed context")
        if literal_eval(action["domain"]) != [] or literal_eval(native["domain"]) != []:
            raise ValueError("Changed domain")
        help_values = action["help"]
        native_help = "".join(ElementTree.tostring(child, encoding="unicode") for child in fields["help"])
        if (
            not isinstance(help_values, dict)
            or set(help_values) != {"en_US"}
            or _action_help(help_values["en_US"]) != _action_help(native_help)
        ):
            raise ValueError("Edited or translated help")
    except (ValueError, SyntaxError, TypeError, ElementTree.ParseError) as error:
        raise RuntimeError("Customized Org Chart action context, domain or help") from error
    _require_empty(
        action,
        {"id", "create_uid", "write_uid", "create_date", "write_date", "context", "domain", "help"},
        {
            "name": ({"en_US": native["name"]},),
            "type": (source.model,),
            "res_model": (native["res_model"],),
            "path": (native["path"],),
            "view_mode": (native["view_mode"],),
            "mobile_view_mode": (native["mobile_view_mode"] if private else "kanban",),
            "search_view_id": (search_view_id,),
            "target": ("current",),
            "binding_type": ("action",),
            "binding_view_types": ("list,form",),
            "res_id": (None, 0),
            "limit": (80,),
            "filter": (None, False),
            "cache": (True,),
        },
    )
    return source


def _action_help(value: str) -> str:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    document = ElementTree.fromstring("<div>" + value + "</div>", parser=parser)
    return ElementTree.canonicalize(ElementTree.tostring(document, encoding="unicode"), strip_text=True, with_comments=True)


def _hr_org_chart_link_plan(
    cr: Any,
    name: str = "act_hr_employee_public_hierarchy_view",
    *,
    path: Path | None = None,
    parent: str = "hr_employee_public_action",
    model: str = "hr.employee.public",
    sequence: int = 16,
) -> Reference:
    source = _require_source(cr, "hr_org_chart", name, "ir.actions.act_window.view")
    target = _check_target(cr, name, source, module="hr")
    if (target is not None and target.noupdate) or (source.noupdate and target is None):
        raise RuntimeError("Protected Org Chart hierarchy link ownership")
    parent_action = _require_source(cr, "hr", parent, "ir.actions.act_window")
    cr.execute("SELECT id,res_model,type FROM ir_act_window WHERE id = %s FOR UPDATE", (parent_action.record_id,))
    if cr.fetchall() != [(parent_action.record_id, model, parent_action.model)]:
        raise RuntimeError("Missing or rebound Org Chart parent action")
    fields = _hr_native_fields(path if path is not None else HR_ORG_CHART_DATA_FILE, name, source.model)
    if (
        set(fields) != {"sequence", "view_mode", "act_window_id"}
        or fields["sequence"].attrib != {"name": "sequence", "eval": str(sequence)}
        or fields["view_mode"].attrib != {"name": "view_mode"}
        or fields["view_mode"].text != "hierarchy"
        or fields["act_window_id"].attrib != {"name": "act_window_id", "ref": f"hr.{parent}"}
        or any(len(field) for field in fields.values())
        or any((fields[key].text or "").strip() for key in ("sequence", "act_window_id"))
    ):
        raise RuntimeError("Changed native Org Chart hierarchy link contract")
    links = _rows(cr, "SELECT * FROM ir_act_window_view WHERE id = %s FOR UPDATE", (source.record_id,))
    if len(links) != 1:
        raise RuntimeError("Missing Org Chart hierarchy link")
    _require_empty(
        links[0],
        {"id", "create_uid", "write_uid", "create_date", "write_date"},
        {
            "sequence": (sequence,),
            "view_mode": ("hierarchy",),
            "act_window_id": (parent_action.record_id,),
            "multi": (None, False),
        },
    )
    cr.execute(
        "SELECT module,name,model FROM ir_model_data WHERE model = %s AND res_id = %s FOR UPDATE", (source.model, source.record_id)
    )
    aliases = {("hr_org_chart", name, source.model)} | ({("hr", name, source.model)} if target is not None else set())
    if set(cr.fetchall()) != aliases:
        raise RuntimeError("Customized Org Chart hierarchy link aliases")
    return source


def _hr_org_chart_stock_views(series: str) -> dict[str, dict[str, ElementTree.Element]]:
    source = Path(__file__).with_name("legacy") / "hr_org_chart" / f"hr_employee_public_views_{series}.xml"
    sources = [source, source.with_name("hr_employee_public_form_18.xml")] if series == "18" else [source]
    return {
        record.attrib["id"]: {field.attrib["name"]: field for field in record.findall("field")}
        for source in sources
        for record in ElementTree.parse(source).findall("record[@model='ir.ui.view']")
    }


def _hr_org_chart_view_plan(cr: Any) -> tuple[Reference, int | None]:
    normalize = runpy.run_path(str(Path(__file__).with_name("mail_view_20.py")))["_normalized_architecture"]
    stock = _hr_org_chart_stock_views("19")
    previous = _hr_org_chart_stock_views("18")
    cr.execute("LOCK TABLE ir_ui_view, ir_ui_view_custom, ir_ui_view_group_rel, ir_model_data IN SHARE ROW EXCLUSIVE MODE")
    hierarchy_name = "hr_employee_public_hierarchy_view"
    hierarchy = _require_source(cr, "hr_org_chart", hierarchy_name, "ir.ui.view")
    target = _check_target(cr, hierarchy_name, hierarchy, module="hr")
    if (target is not None and target.noupdate) or (hierarchy.noupdate and target is None):
        raise RuntimeError("Protected Org Chart hierarchy ownership")
    native = ElementTree.parse(HR_ORG_CHART_DATA_FILE)
    records = native.findall(f".//record[@id='{hierarchy_name}']")
    if len(records) != 1 or records[0].attrib != {"id": hierarchy_name, "model": "ir.ui.view"}:
        raise RuntimeError("Unsupported native Org Chart hierarchy definition")
    fields = {field.attrib.get("name"): field for field in records[0]}
    if (
        set(fields) != {"name", "model", "arch"}
        or len(records[0]) != len(fields)
        or fields["name"].text != stock[hierarchy_name]["name"].text
        or fields["model"].text != stock[hierarchy_name]["model"].text
        or fields["name"].attrib != {"name": "name"}
        or fields["model"].attrib != {"name": "model"}
        or fields["arch"].attrib != {"name": "arch", "type": "xml"}
        or len(fields["arch"]) != 1
        or fields["arch"][0].tag != "hierarchy"
    ):
        raise RuntimeError("Changed native Org Chart hierarchy contract")
    forms = native.findall(".//record[@id='hr_employee_public_view_form']")
    if len(forms) != 1 or forms[0].attrib != {"id": "hr_employee_public_view_form", "model": "ir.ui.view"}:
        raise RuntimeError("Unsupported native public employee form")
    form_fields = {field.attrib.get("name"): field for field in forms[0]}
    if (
        set(form_fields) != {"name", "model", "arch"}
        or len(forms[0]) != len(form_fields)
        or not form_fields["name"].text
        or form_fields["model"].text != stock[hierarchy_name]["model"].text
        or form_fields["model"].attrib != {"name": "model"}
        or form_fields["arch"].attrib != {"name": "arch", "type": "xml"}
        or len(form_fields["arch"]) != 1
        or form_fields["arch"][0].tag != "form"
        or any(native.findall(f".//record[@id='{name}']") for name in stock if name != hierarchy_name)
    ):
        raise RuntimeError("Changed native public employee form contract")
    containers = form_fields["arch"].findall(".//div[@id='o_employee_org_chart']")
    if len(containers) != 1 or len(containers[0].findall(".//field[@name='child_ids'][@widget='hr_org_chart']")) != 1:
        raise RuntimeError("Native public employee form must own its Org Chart")
    cr.execute(
        "SELECT id FROM ir_ui_view WHERE model = %s AND type = 'hierarchy' AND mode = 'primary' AND id != %s",
        (stock[hierarchy_name]["model"].text, hierarchy.record_id),
    )
    if cr.fetchone():
        raise RuntimeError("Preserve competing public employee hierarchy views")
    action = _require_source(cr, "hr_org_chart", "action_hr_employee_public_org_chart", "ir.actions.act_window")
    extension_change = None
    for name, fields in stock.items():
        source = _require_source(cr, "hr_org_chart", name, "ir.ui.view")
        inherited = "inherit_id" in fields
        parent = (
            _require_source(cr, "hr", fields["inherit_id"].attrib["ref"].removeprefix("hr."), "ir.ui.view") if inherited else None
        )
        rows = _rows(cr, "SELECT * FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
        if len(rows) != 1 or (parent is not None and (source.noupdate or parent.noupdate)):
            raise RuntimeError("Unsupported retained Org Chart view ownership")
        if parent is not None:
            cr.execute("SELECT id FROM ir_ui_view WHERE id = %s AND model = %s", (parent.record_id, fields["model"].text))
            if cr.fetchone() is None:
                raise RuntimeError("Missing public employee form parent")
        cr.execute(
            "SELECT module, name, noupdate FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s", (source.record_id,)
        )
        aliases = {("hr_org_chart", name, source.noupdate)}
        if not inherited and target is not None:
            aliases.add(("hr", name, False))
        if set(cr.fetchall()) != aliases:
            raise RuntimeError("Customized retained Org Chart view aliases")
        for query in (
            "SELECT 1 FROM ir_ui_view WHERE inherit_id = %s LIMIT 1",
            "SELECT 1 FROM ir_ui_view_custom WHERE ref_id = %s LIMIT 1",
            "SELECT 1 FROM ir_ui_view_group_rel WHERE view_id = %s LIMIT 1",
        ):
            cr.execute(query, (source.record_id,))
            if cr.fetchone():
                raise RuntimeError("Preserve dependent, customized or restricted Org Chart view")
        view = rows[0]
        _require_empty(
            view,
            {"id", "arch_db", "arch_prev", "create_uid", "write_uid", "create_date", "write_date"},
            {
                "name": (fields["name"].text,),
                "model": (fields["model"].text,),
                "inherit_id": (parent.record_id if parent else None,),
                "type": ("form" if inherited else "hierarchy",),
                "mode": ("extension" if inherited else "primary",),
                "active": (True,),
                "priority": (16,),
                "arch_updated": (False,),
                "arch_fs": ("hr_org_chart/views/hr_employee_public_views.xml",),
                "customize_show": (False,),
                "is_seo_optimized": (False,),
                **UNTRACKED_PUBLIC_VIEW_DEFAULTS,
            },
        )
        expected = ElementTree.tostring(fields["arch"][0], encoding="unicode").replace(
            "%(hr_org_chart.action_hr_employee_public_org_chart)d", str(action.record_id)
        )
        architectures = view["arch_db"]
        if not isinstance(architectures, dict) or set(architectures) != {"en_US"}:
            raise RuntimeError("Preserve translated Org Chart architecture")
        try:
            expected = normalize(expected)
            current = normalize(architectures["en_US"])
            admitted_history = {expected}
            if name in previous:
                admitted_history.add(normalize(ElementTree.tostring(previous[name]["arch"][0], encoding="unicode")))
            if view.get("arch_prev") and normalize(view["arch_prev"]) not in admitted_history:
                raise RuntimeError("Preserve customized Org Chart previous architecture")
        except (ElementTree.ParseError, TypeError, ValueError) as error:
            raise RuntimeError("Unsupported Org Chart architecture") from error
        if current == expected:
            if inherited:
                extension_change = source.record_id
        elif not inherited or current != normalize("<data/>"):
            raise RuntimeError("Preserve customized Org Chart architecture")
    return hierarchy, extension_change


def _inherited_view_source(cr: Any, view: InheritedView) -> tuple[int, dict[str, str]] | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (view.module,))
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    source = _require_source(cr, view.module, view.name, "ir.ui.view")
    parent = _require_source(cr, "base", view.parent, "ir.ui.view")
    cr.execute("SELECT model, inherit_id, mode, arch_db FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
    rows = cr.fetchall()
    if source.noupdate or parent.noupdate or len(rows) != 1 or rows[0][:3] != (view.model, parent.record_id, "primary"):
        raise RuntimeError(f"Unsupported {view.module}.{view.name} ownership or inheritance")
    architectures = rows[0][3]
    if not architectures or not isinstance(architectures, dict) or any(not isinstance(arch, str) for arch in architectures.values()):
        raise RuntimeError(f"Unsupported {view.module}.{view.name} architecture")
    counts = {(arch.count(view.source_selector), arch.count(view.target_selector)) for arch in architectures.values()}
    if counts == {(0, 1)}:
        return None
    if counts != {(1, 0)}:
        raise RuntimeError(f"Ambiguous {view.module}.{view.name} selectors or mixed language transition")
    return source.record_id, {
        language: arch.replace(view.source_selector, view.target_selector, 1) for language, arch in architectures.items()
    }


def _rows(cr: Any, query: str, parameters: tuple = ()) -> list[dict[str, Any]]:
    cr.execute(query, parameters)
    columns = [column[0] for column in cr.description]
    return [dict(zip(columns, row, strict=True)) for row in cr.fetchall()]


def _identified(cr: Any, query: str, identifiers: tuple[str, ...], parameters: tuple = ()) -> None:
    sql = import_module("odoo.tools").SQL
    try:
        names = tuple(sql.identifier(name) for name in identifiers)
    except (AssertionError, ValueError) as error:
        raise RuntimeError("Unsupported direct chat reference identifier") from error
    cr.execute(sql(query, *names, *parameters))


def _chat_groups(cr: Any) -> list[list[DirectChat]]:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'mail'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return []
    channels = _rows(cr, "SELECT * FROM discuss_channel WHERE channel_type = 'chat' ORDER BY id FOR UPDATE")
    members = _rows(
        cr,
        "SELECT member.* FROM discuss_channel_member member JOIN discuss_channel channel ON channel.id = member.channel_id "
        "WHERE channel.channel_type = 'chat' ORDER BY member.id FOR UPDATE",
    )
    audiences: dict[int, list[dict[str, Any]]] = {channel["id"]: [] for channel in channels}
    for member in members:
        audiences[member["channel_id"]].append(member)
    groups: dict[tuple, list[DirectChat]] = {}
    for channel in channels:
        audience = audiences[channel["id"]]
        identities = tuple(sorted((member["partner_id"] or 0, member["guest_id"] or 0) for member in audience))
        groups.setdefault(identities, []).append(DirectChat(channel, audience))
    return [group for group in groups.values() if len(group) > 1]


def _require_empty(row: dict[str, Any], ignored: set[str], defaults: dict[str, tuple]) -> None:
    for name, value in row.items():
        if name in ignored:
            continue
        if value not in defaults.get(name, (None,)):
            raise RuntimeError(f"Customized or unsupported core transition state: {name}")


def _pinned(member: dict[str, Any], channel: dict[str, Any]) -> bool:
    unpin = member["unpin_dt"]
    return not unpin or any(
        value is not None and value >= unpin for value in (member["last_interest_dt"], channel["last_interest_dt"])
    )


def _check_chat_state(cr: Any, group: list[DirectChat]) -> None:
    identities = [member["partner_id"] for member in group[0].members]
    if not 1 <= len(identities) <= 2 or None in identities or len(set(identities)) != len(identities):
        raise RuntimeError("Unsupported duplicate direct chat audience")
    names = []
    for partner in identities:
        cr.execute("SELECT name FROM res_partner WHERE id = %s", (partner,))
        row = cr.fetchone()
        if row is None or not row[0]:
            raise RuntimeError("Unsupported duplicate direct chat partner")
        names.append(row[0])
    generated_names = {", ".join(names), ", ".join(reversed(names))}
    for channel in group:
        if channel.values["name"] not in generated_names or not channel.values["active"]:
            raise RuntimeError("Customized duplicate direct chat name or visibility")
        created = channel.values.get("create_date")
        projections = (
            {"livechat_start_hour": (None, created.hour), "livechat_week_day": (None, str(created.weekday()))}
            if isinstance(created, datetime) and created.tzinfo is None
            else {}
        )
        _require_empty(
            channel.values,
            {"id", "name", "uuid", "create_uid", "write_uid", "create_date", "write_date", "last_interest_dt"},
            {
                "channel_type": ("chat",),
                "active": (True,),
                "allow_public_upload": (None, False),
                "livechat_active": (None, False),
                "rating_last_value": (None, 0),
                "livechat_is_escalated": (None, False),
                "has_crm_lead": (None, False),
                "is_pending_chat_request": (None, False),
                **projections,
            },
        )
        for member in channel.members:
            _require_empty(
                member,
                {
                    "id",
                    "channel_id",
                    "partner_id",
                    "create_uid",
                    "write_uid",
                    "create_date",
                    "write_date",
                    "last_interest_dt",
                    "unpin_dt",
                },
                {"fold_state": ("closed",), "new_message_separator": (0,)},
            )
    survivor = group[0]
    pinned = {member["partner_id"]: _pinned(member, survivor.values) for member in survivor.members}
    if any(_pinned(member, channel.values) != pinned[member["partner_id"]] for channel in group for member in channel.members):
        raise RuntimeError("Duplicate direct chats have different sidebar state")


def _check_encoded_relations(cr: Any, table: str, column: str, targets: dict[str, list[int]], known_models: set[str]) -> None:
    references = {f"{model},{record}" for model, records in targets.items() for record in records}
    ambiguous = set(targets) | references | {str(record) for records in targets.values() for record in records}
    _identified(cr, "SELECT %s FROM %s WHERE %s IS NOT NULL", (column, table, column))
    while row := cr.fetchone():
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"Unsupported core transition reference encoding: {table}.{column}") from error
        if (
            not isinstance(value, list)
            or len(value) not in {2, 3, 4}
            or not isinstance(value[0], str)
            or not value[0]
            or not isinstance(value[1], str)
        ):
            raise RuntimeError(f"Unsupported core transition reference encoding: {table}.{column}")
        model = value[1]
        topics = (value[0], *(value[1:] if len(value) == 2 else value[3:]))
        if any(isinstance(topic, str) and topic in ambiguous for topic in topics):
            raise RuntimeError(f"Ambiguous core transition reference: {table}.{column}")
        if len(value) == 2:
            continue
        if model not in known_models or type(value[2]) is not int or (len(value) == 4 and not isinstance(value[3], str)):
            raise RuntimeError(f"Unsupported core transition reference encoding: {table}.{column}")
        if model in targets and value[2] in targets[model]:
            raise RuntimeError(f"Referenced core transition row: {table}.{column}")


def _check_references(
    cr: Any,
    ids: dict[str, list[int]],
    internal_relations: set[tuple[str, str, str]],
    *,
    indirect_rows: dict[tuple[str, str, str], list[int]] | None = None,
) -> None:
    indirect_rows = indirect_rows or {}
    for (source, _column, target), rows in indirect_rows.items():
        if source not in ids or target not in ids or not set(rows).issubset(ids[source]):
            raise RuntimeError("Internal reference rows must belong to retiring source and target cohorts")

    model_targets = {table.replace("_", "."): records for table, records in ids.items()}
    models = list(model_targets)
    cr.execute(
        "SELECT source_ns.nspname, source.relname, source_col.attname, target.relname, target_col.attname, "
        "cardinality(fk.conkey) FROM pg_constraint fk "
        "JOIN pg_class source ON source.oid = fk.conrelid JOIN pg_namespace source_ns ON source_ns.oid = source.relnamespace "
        "JOIN pg_class target ON target.oid = fk.confrelid JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace "
        "JOIN pg_attribute source_col ON source_col.attrelid = source.oid AND source_col.attnum = fk.conkey[1] "
        "JOIN pg_attribute target_col ON target_col.attrelid = target.oid AND target_col.attnum = fk.confkey[1] "
        "WHERE fk.contype = 'f' AND target_ns.nspname = 'public' "
    )
    model_columns: dict[str, set[str]] = {}
    foreign_keys: set[tuple[str, str, str]] = set()
    for schema, table, column, target, target_column, width in cr.fetchall():
        if target not in ids and target != "ir_model":
            if schema == "public":
                foreign_keys.add((table, column, target))
            continue
        if schema != "public" or target_column != "id" or width != 1:
            raise RuntimeError("Unsupported core transition reference constraint")
        foreign_keys.add((table, column, target))
        if target == "ir_model":
            model_columns.setdefault(table, set()).add(column)
            continue
        if (table, column, target) in internal_relations:
            continue
        _identified(cr, "SELECT 1 FROM %s WHERE %s = ANY(%s) LIMIT 1", (table, column), (ids[target],))
        if cr.fetchone():
            raise RuntimeError(f"Referenced core transition row: {table}.{column}")
    cr.execute(
        "SELECT name FROM ir_model_fields WHERE store AND ttype = 'many2one_reference' "
        "UNION SELECT 'res_id' UNION SELECT 'alias_force_thread_id' UNION SELECT 'alias_parent_thread_id'"
    )
    indirect_columns = {row[0] for row in cr.fetchall()}
    _identified(
        cr,
        "SELECT 1 FROM ir_model_fields WHERE store AND company_dependent "
        "AND (relation = ANY(%s) OR ttype IN ('reference', 'many2one_reference')) LIMIT 1",
        (),
        (models,),
    )
    if cr.fetchone():
        raise RuntimeError("Unsupported company-dependent core transition reference")
    cr.execute("SELECT id, model FROM ir_model")
    model_metadata = {model: model_id for model_id, model in cr.fetchall()}
    known_models = set(model_metadata)
    model_ids = {model: model_metadata[model] for model in models if model in model_metadata}
    if set(model_ids) != set(models):
        raise RuntimeError("Missing core transition model metadata")
    cr.execute(
        "SELECT field.table_name, field.column_name, field.data_type FROM information_schema.columns field "
        "JOIN information_schema.tables owner ON owner.table_schema = field.table_schema AND owner.table_name = field.table_name "
        "WHERE field.table_schema = 'public' AND owner.table_type = 'BASE TABLE' ORDER BY field.table_name, field.ordinal_position"
    )
    columns: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in cr.fetchall():
        columns.setdefault(table, []).append((column, data_type))
    _identified(
        cr,
        "SELECT name, ttype, relation, relation_table, column2, model, column1 FROM ir_model_fields WHERE store AND "
        "((relation = ANY(%s) AND ttype IN ('many2one', 'many2many')) "
        "OR (model = ANY(%s) AND ttype = 'many2many'))",
        (),
        (models, models),
    )
    relational_columns: dict[str, list[tuple[str | None, list[int]]]] = {}
    for name, kind, relation, relation_table, target_column, model, source_column in cr.fetchall():
        endpoints = ((target_column, relation), (source_column, model)) if kind == "many2many" else ((name, relation),)
        for column, target in endpoints:
            if target not in models:
                continue
            if not column or (kind == "many2many" and not relation_table):
                raise RuntimeError("Unsupported core transition relation metadata")
            owner = relation_table if kind == "many2many" else None
            relational_columns.setdefault(column, []).append((owner, ids[target.replace(".", "_")]))
    cr.execute(
        "SELECT 1 FROM pg_class relation JOIN pg_namespace schema ON schema.oid = relation.relnamespace "
        "JOIN pg_attribute field ON field.attrelid = relation.oid WHERE schema.nspname = 'public' "
        "AND relation.relkind = 'm' AND NOT field.attisdropped AND field.attname = ANY(%s) LIMIT 1",
        (list(relational_columns),),
    )
    if cr.fetchone():
        raise RuntimeError("Unsupported materialized core transition reference storage")
    foreign_columns = {(table, column) for table, column, _target in foreign_keys}
    if "ir_default" in columns:
        _identified(
            cr,
            "SELECT 1 FROM ir_default value JOIN ir_model_fields field ON field.id = value.field_id "
            "WHERE field.relation = ANY(%s) OR field.ttype IN ('reference', 'many2one_reference') LIMIT 1",
            (),
            (models,),
        )
        if cr.fetchone():
            raise RuntimeError("Unsupported core transition field default")
    references = [f"{model},{record}" for model in model_ids for record in ids[model.replace(".", "_")]]
    json_targets = json.dumps({"targets": [*model_ids, *references]})
    for table, fields in columns.items():
        for column, data_type in fields:
            text_storage = data_type in {"text", "character varying", "character"}
            if (table, column) not in foreign_columns:
                for owner, targets in relational_columns.get(column, ()):
                    if owner is not None and owner != table:
                        continue
                    if owner is None and text_storage:
                        _check_encoded_relations(cr, table, column, model_targets, known_models)
                        continue
                    if data_type not in {"smallint", "integer", "bigint"}:
                        raise RuntimeError(f"Unsupported core transition reference column type: {table}.{column}")
                    _identified(cr, "SELECT 1 FROM %s WHERE %s = ANY(%s) LIMIT 1", (table, column), (targets,))
                    if cr.fetchone():
                        raise RuntimeError(f"Unsupported FK-less core transition reference: {table}.{column}")
            if text_storage:
                _identified(cr, "SELECT 1 FROM %s WHERE %s = ANY(%s) LIMIT 1", (table, column), (references,))
                if cr.fetchone():
                    raise RuntimeError(f"Referenced core transition row: {table}.{column}")
            if data_type in {"json", "jsonb"}:
                _identified(
                    cr,
                    "SELECT 1 FROM %s WHERE jsonb_path_exists(%s::jsonb, '$.** ? (@ == $targets[*])', %s::jsonb) LIMIT 1",
                    (table, column),
                    (json_targets,),
                )
                if cr.fetchone():
                    raise RuntimeError(f"Unsupported core transition JSON reference: {table}.{column}")
            if column not in indirect_columns:
                continue
            for model, model_id in model_ids.items():
                query = (
                    "SELECT 1 FROM %s record WHERE %s = ANY(%s) AND "
                    "(EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value WHERE value.value = %s) "
                    "OR EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value "
                    "WHERE value.key = ANY(%s) AND value.value = %s))"
                )
                parameters = (
                    [str(record) for record in ids[model.replace(".", "_")]] if text_storage else ids[model.replace(".", "_")],
                    model,
                    list(model_columns.get(table, ())),
                    str(model_id),
                )
                allowed = indirect_rows.get((table, column, model.replace(".", "_")))
                if allowed:
                    query += " AND NOT (record.id = ANY(%s))"
                    parameters += (allowed,)
                _identified(cr, query + " LIMIT 1", (table, column), parameters)
                if cr.fetchone():
                    raise RuntimeError(f"Indirectly referenced core transition row: {table}.{column}")
    cr.execute("SELECT 1 FROM pg_trigger WHERE tgrelid = ANY(%s::regclass[]) AND NOT tgisinternal LIMIT 1", (list(ids),))
    if cr.fetchone():
        raise RuntimeError("Unsupported core transition deletion trigger")


def _empty_chat_plan(cr: Any) -> list[list[DirectChat]]:
    groups = _chat_groups(cr)
    for group in groups:
        _check_chat_state(cr, group)
    if groups:
        _check_references(
            cr,
            {
                "discuss_channel": [channel.values["id"] for group in groups for channel in group],
                "discuss_channel_member": [member["id"] for group in groups for channel in group for member in channel.members],
            },
            {("discuss_channel_member", "channel_id", "discuss_channel")},
        )
    return groups


def _retire_empty_chats(cr: Any, groups: list[list[DirectChat]]) -> None:
    for group in groups:
        for channel in group[1:]:
            cr.execute("DELETE FROM discuss_channel_member WHERE channel_id = %s", (channel.values["id"],))
            cr.execute("DELETE FROM discuss_channel WHERE id = %s", (channel.values["id"],))
            _logger.info("Retired redundant empty direct chat %s; preserved chat %s", channel.values["id"], group[0].values["id"])


def _lunch_plan(cr: Any) -> list[int]:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'resource'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return []
    attendances = _rows(cr, "SELECT * FROM resource_calendar_attendance ORDER BY id FOR UPDATE")
    lunches = [row for row in attendances if row["day_period"] == "lunch"]
    if not lunches:
        return []
    calendar_ids = {row["calendar_id"] for row in lunches}
    attendances = [row for row in attendances if row["calendar_id"] in calendar_ids]
    calendars = _rows(cr, "SELECT id, two_weeks_calendar FROM resource_calendar FOR UPDATE")
    single_week = {row["id"] for row in calendars if row["two_weeks_calendar"] in (None, False)}
    for row in attendances:
        if row["calendar_id"] not in single_week or row["dayofweek"] not in {str(day) for day in range(7)}:
            raise RuntimeError("Unsupported legacy attendance calendar or weekday")
        if row["day_period"] not in {"morning", "afternoon", "full_day", "lunch"}:
            raise RuntimeError("Unsupported legacy attendance period")
        if any(row.get(name) is not None for name in ("week_type", "display_type", "date_from", "date_to", "resource_id")):
            raise RuntimeError("Unsupported legacy attendance recurrence, section, dates or resource")
        if not all(isinstance(row[name], (int, float)) and isfinite(row[name]) for name in ("hour_from", "hour_to")):
            raise RuntimeError("Unsupported legacy attendance clock range")
        if not 0 <= row["hour_from"] < row["hour_to"] <= 24:
            raise RuntimeError("Unsupported legacy attendance clock range")
    for lunch in lunches:
        _require_empty(
            lunch,
            {
                "id",
                "calendar_id",
                "name",
                "dayofweek",
                "day_period",
                "hour_from",
                "hour_to",
                "sequence",
                "create_uid",
                "write_uid",
                "create_date",
                "write_date",
            },
            {"duration_hours": (0,), "duration_days": (0,)},
        )
        if any(
            row["day_period"] != "lunch"
            and row["calendar_id"] == lunch["calendar_id"]
            and row["dayofweek"] == lunch["dayofweek"]
            and row["hour_from"] < lunch["hour_to"]
            and lunch["hour_from"] < row["hour_to"]
            for row in attendances
        ):
            raise RuntimeError("Legacy lunch overlaps working attendance")
    ids = [row["id"] for row in lunches]
    _check_references(cr, {"resource_calendar_attendance": ids}, set())
    return ids


def _policy_xmlid_plan(cr: Any) -> list[int]:
    cr.execute(
        "SELECT id, module, name, model FROM ir_model_data data "
        "WHERE model IN ('ir.rule', 'ir.model.access') AND module NOT IN ('__export__', '__custom__', 'studio_customization') "
        "AND EXISTS (SELECT 1 FROM ir_module_module owner WHERE owner.name = data.module) ORDER BY id FOR UPDATE"
    )
    policies = cr.fetchall()
    pending = []
    for record_id, module, name, model in policies:
        suffix = "_" + model.replace(".", "_")
        if name.endswith(suffix):
            continue
        if _reference(cr, module, name + suffix) is not None:
            raise RuntimeError(f"Conflicting legacy policy reference {module}.{name + suffix}")
        pending.append(record_id)
    return pending


def _release_policy_xmlids(cr: Any, policies: list[int]) -> None:
    # https://github.com/hbrunn/OpenUpgrade/blob/2d9eb6bab7e7e579953e38d7ad3b96aad6fcc44d/openupgrade_scripts/scripts/base/20.0.1.3/pre-migration.py
    for record_id in policies:
        cr.execute("UPDATE ir_model_data SET name = name || '_' || replace(model, '.', '_') WHERE id = %s", (record_id,))
    _logger.info("Released %s legacy policy XML IDs for native Odoo 20 data loading", len(policies))


def _admit_mail(cr: Any) -> None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'mail'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return
    get_module_path = import_module("odoo.modules.module").get_module_path

    helpers = runpy.run_path(str(Path(__file__).with_name("mail_view_20.py")))
    target = Path(get_module_path("mail")) / "views/ir_actions_server_views.xml"
    helpers["admit_before_effects"](cr, target)


def _calendar_stock_views(series: str) -> dict[str, dict[str, ElementTree.Element]]:
    source = Path(__file__).with_name("legacy") / "calendar" / f"res_users_views_{series}.xml"
    return {
        record.attrib["id"]: {field.attrib["name"]: field for field in record.findall("field")}
        for record in ElementTree.parse(source).findall("./data/record")
    }


def _check_calendar_privacy(cr: Any) -> None:
    cr.execute("SELECT to_regclass('calendar_calendar'), to_regclass('calendar_user')")
    if cr.fetchone() != (None, None):
        raise RuntimeError("Calendar ownership already exists; qualify this crossing separately")
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'calendar.default_privacy' FOR UPDATE")
    defaults = cr.fetchall()
    if len(defaults) > 1:
        raise RuntimeError("Ambiguous Calendar global privacy")
    fallback = defaults[0][0] if defaults else "public"
    if fallback not in {"public", "private", "confidential"}:
        raise RuntimeError("Unsupported Calendar global privacy")
    cr.execute("SELECT user_id, calendar_default_privacy FROM res_users_settings ORDER BY id FOR UPDATE")
    settings = cr.fetchall()
    cr.execute("SELECT id FROM res_users FOR UPDATE")
    users = {row[0] for row in cr.fetchall()}
    owners = [row[0] for row in settings]
    if len(owners) != len(set(owners)) or not set(owners) <= users:
        raise RuntimeError("Ambiguous Calendar preference owners")
    if any((privacy or fallback) != fallback for _, privacy in settings):
        raise RuntimeError("Preserve divergent Calendar privacy before native calendar creation")
    cr.execute("SELECT 1 FROM calendar_event LIMIT 1")
    if cr.fetchone():
        raise RuntimeError("Populated Calendar events require a qualified ownership transition")
    cr.execute("SELECT 1 FROM calendar_recurrence LIMIT 1")
    if cr.fetchone():
        raise RuntimeError("Populated Calendar recurrence requires a qualified ownership transition")


def _calendar_view_plan(cr: Any) -> list[int]:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'calendar'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return []
    normalize = runpy.run_path(str(Path(__file__).with_name("mail_view_20.py")))["_normalized_architecture"]
    stock = _calendar_stock_views("19")
    sources = {name: _require_source(cr, "calendar", name, "ir.ui.view") for name in stock}
    cr.execute(
        "LOCK TABLE ir_ui_view, ir_ui_view_custom, ir_ui_view_group_rel, ir_model_data, res_users, res_users_settings, "
        "ir_config_parameter, calendar_event, calendar_recurrence IN SHARE ROW EXCLUSIVE MODE"
    )
    _check_calendar_privacy(cr)
    previous = _calendar_stock_views("18")
    changes = []
    for name, fields in stock.items():
        source = sources[name]
        parent_name = fields["inherit_id"].attrib["ref"].removeprefix("base.")
        parent = _require_source(cr, "base", parent_name, "ir.ui.view")
        rows = _rows(cr, "SELECT * FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
        if source.noupdate or parent.noupdate or len(rows) != 1 or rows[0]["inherit_id"] != parent.record_id:
            raise RuntimeError(f"Unsupported Calendar view ownership or inheritance: {name}")
        cr.execute("SELECT id FROM ir_ui_view WHERE id = %s FOR UPDATE", (parent.record_id,))
        if cr.fetchone() is None:
            raise RuntimeError("Missing Calendar users parent")
        cr.execute(
            "SELECT module, name, noupdate FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s", (source.record_id,)
        )
        if cr.fetchall() != [("calendar", name, False)]:
            raise RuntimeError("Customized Calendar view aliases")
        cr.execute("SELECT 1 FROM ir_ui_view WHERE inherit_id = %s LIMIT 1", (source.record_id,))
        children = cr.fetchone()
        cr.execute("SELECT 1 FROM ir_ui_view_custom WHERE ref_id = %s LIMIT 1", (source.record_id,))
        customizations = cr.fetchone()
        cr.execute("SELECT 1 FROM ir_ui_view_group_rel WHERE view_id = %s LIMIT 1", (source.record_id,))
        if children or customizations or cr.fetchone():
            raise RuntimeError("Preserve dependent, customized or restricted Calendar view")
        view = rows[0]
        _require_empty(
            view,
            {"id", "inherit_id", "arch_db", "arch_prev", "create_uid", "write_uid", "create_date", "write_date"},
            {
                "name": (fields["name"].text,),
                "model": (fields["model"].text,),
                "type": ("form",),
                "mode": ("extension",),
                "active": (True,),
                "priority": (16,),
                "arch_updated": (False,),
                "arch_fs": ("calendar/views/res_users_views.xml",),
                "is_seo_optimized": (False,),
                **UNTRACKED_PUBLIC_VIEW_DEFAULTS,
            },
        )
        expected = normalize(ElementTree.tostring(fields["arch"][0], encoding="unicode"))
        old = normalize(ElementTree.tostring(previous[name]["arch"][0], encoding="unicode"))
        architectures = view["arch_db"]
        if not isinstance(architectures, dict) or set(architectures) != {"en_US"}:
            raise RuntimeError("Preserve translated Calendar architecture")
        try:
            current = normalize(architectures["en_US"])
            if view.get("arch_prev") and normalize(view["arch_prev"]) not in {old, expected}:
                raise RuntimeError("Preserve customized Calendar previous architecture")
        except (ElementTree.ParseError, TypeError, ValueError) as error:
            raise RuntimeError("Unsupported Calendar architecture") from error
        if current == expected:
            changes.append(source.record_id)
        elif current != normalize("<data/>"):
            raise RuntimeError("Preserve customized Calendar architecture")
    cr.execute(
        "SELECT 1 FROM ir_ui_view WHERE id NOT IN (%s, %s) AND CAST(arch_db AS TEXT) LIKE %s LIMIT 1",
        (*[source.record_id for source in sources.values()], "%calendar_default_privacy%"),
    )
    if cr.fetchone():
        raise RuntimeError("Preserve other Calendar preference views")
    for query in (
        "SELECT 1 FROM ir_rule WHERE CAST(domain_force AS TEXT) LIKE %s LIMIT 1",
        "SELECT 1 FROM ir_act_window WHERE CAST(context AS TEXT) LIKE %s OR CAST(domain AS TEXT) LIKE %s LIMIT 1",
        "SELECT 1 FROM ir_act_server WHERE CAST(code AS TEXT) LIKE %s LIMIT 1",
        "SELECT 1 FROM ir_filters WHERE CAST(context AS TEXT) LIKE %s OR CAST(domain AS TEXT) LIKE %s LIMIT 1",
    ):
        cr.execute(query, ("%calendar_default_privacy%",) * query.count("%s"))
        if cr.fetchone():
            raise RuntimeError("Preserve outside Calendar preference references")
    return changes


def _hr_preferences_view_plan(cr: Any) -> tuple[int, str] | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'hr'")
    if cr.fetchone() not in {("installed",), ("to upgrade",)}:
        return None
    primary_name = "res_users_view_form_simple_modif"
    child_name = "res_users_view_form_preferences"
    if _reference(cr, "hr", child_name) is None:
        return None
    helpers = runpy.run_path(str(Path(__file__).with_name("mail_view_20.py")))
    normalize = helpers["_normalized_architecture"]
    render = helpers["_render_architecture"]
    modules = {"hr": (primary_name, child_name)}
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'mail_bot_hr'")
    if cr.fetchone() in {("installed",), ("to upgrade",)}:
        modules["mail_bot_hr"] = (primary_name, child_name)
    references = {"base.view_users_form_simple_modif": _require_source(cr, "base", "view_users_form_simple_modif", "ir.ui.view")}
    stock: dict[str, dict[str, ElementTree.Element]] = {}
    for module, names in modules.items():
        path = Path(__file__).with_name("legacy") / module / ("res_users_19.xml" if module == "hr" else "res_users_views_19.xml")
        for name in names:
            key = f"{module}.{name}"
            references[key] = _require_source(cr, module, name, "ir.ui.view")
            stock[key] = _hr_native_fields(path, name, "ir.ui.view")
    cr.execute("LOCK TABLE ir_ui_view, ir_ui_view_custom, ir_ui_view_group_rel, ir_model_data IN SHARE ROW EXCLUSIVE MODE")
    if any(ref.noupdate for ref in references.values()) or len({ref.record_id for ref in references.values()}) != len(references):
        raise RuntimeError("Protected or overlapping HR preference ownership")
    base = references["base.view_users_form_simple_modif"]
    cr.execute("SELECT model, type, mode, active FROM ir_ui_view WHERE id = %s FOR UPDATE", (base.record_id,))
    if cr.fetchall() != [("res.users", "form", "primary", True)]:
        raise RuntimeError("Missing or unsupported HR preferences ancestor")
    native = {name: _hr_native_fields(HR_USERS_DATA_FILE, name, "ir.ui.view") for name in modules["hr"]}
    for name, fields in native.items():
        expected = stock[f"hr.{name}"]
        if (
            set(fields) != set(expected)
            or fields["arch"].attrib != expected["arch"].attrib
            or any(
                normalize(ElementTree.tostring(fields[key], encoding="unicode"))
                != normalize(ElementTree.tostring(expected[key], encoding="unicode"))
                for key in expected.keys() - {"arch"}
            )
        ):
            raise RuntimeError("Changed native HR preferences ownership or fields")
    if normalize(render(native[primary_name]["arch"])) != normalize(render(stock[f"hr.{primary_name}"]["arch"])):
        raise RuntimeError("Changed native HR preferences primary architecture")
    target = render(native[child_name]["arch"])
    retired = _reference(cr, "hr_homeworking", "res_useurs_view_form_profile")
    if retired is not None:
        child = references[f"hr.{child_name}"]
        cr.execute("SELECT model, inherit_id FROM ir_ui_view WHERE id = %s FOR UPDATE", (retired.record_id,))
        if retired.model != "ir.ui.view" or retired.noupdate or cr.fetchall() != [("res.users", child.record_id)]:
            raise RuntimeError("Unsupported retained HR Homeworking preference binding")
        cr.execute(
            "SELECT module, name, noupdate FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s", (retired.record_id,)
        )
        if cr.fetchall() != [("hr_homeworking", "res_useurs_view_form_profile", False)]:
            raise RuntimeError("Customized retained HR Homeworking preference aliases")
    parents = {
        key: fields["inherit_id"].attrib["ref"]
        if "." in fields["inherit_id"].attrib["ref"]
        else f"{key.split('.')[0]}.{fields['inherit_id'].attrib['ref']}"
        for key, fields in stock.items()
    }
    plan = None
    for key, fields in stock.items():
        module, name = key.split(".")
        source = references[key]
        rows = _rows(cr, "SELECT * FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
        if len(rows) != 1:
            raise RuntimeError("Missing HR preferences view")
        view = rows[0]
        cr.execute(
            "SELECT module, name, noupdate FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s", (source.record_id,)
        )
        if cr.fetchall() != [(module, name, False)]:
            raise RuntimeError("Customized HR preferences aliases")
        children = {references[child_key].record_id for child_key, parent in parents.items() if parent == key}
        if key == f"hr.{child_name}" and retired is not None:
            children.add(retired.record_id)
        cr.execute("SELECT id FROM ir_ui_view WHERE inherit_id = %s", (source.record_id,))
        if {row[0] for row in cr.fetchall()} != children:
            raise RuntimeError("Unqualified HR preferences descendants")
        for query in (
            "SELECT 1 FROM ir_ui_view_custom WHERE ref_id = %s LIMIT 1",
            "SELECT 1 FROM ir_ui_view_group_rel WHERE view_id = %s LIMIT 1",
        ):
            cr.execute(query, (source.record_id,))
            if cr.fetchone():
                raise RuntimeError("Customized or restricted HR preferences")
        _require_empty(
            view,
            {"id", "arch_db", "arch_prev", "create_uid", "write_uid", "create_date", "write_date"},
            {
                "name": (fields["name"].text,),
                "model": (fields["model"].text,),
                "inherit_id": (references[parents[key]].record_id,),
                "type": ("form",),
                "mode": (fields["mode"].text if "mode" in fields else "extension",),
                "active": (True,),
                "priority": (int(fields["priority"].text or "") if "priority" in fields else 16,),
                "arch_updated": (False,),
                "arch_fs": (f"{module}/views/res_users.xml" if module == "hr" else f"{module}/views/res_users_views.xml",),
                "customize_show": (False,),
                "is_seo_optimized": (False,),
                **UNTRACKED_PUBLIC_VIEW_DEFAULTS,
            },
        )
        architectures = view["arch_db"]
        if not isinstance(architectures, dict) or set(architectures) != {"en_US"}:
            raise RuntimeError("Preserve translated HR preferences")
        try:
            expected = normalize(render(fields["arch"]))
            current = normalize(architectures["en_US"])
            if view.get("arch_prev") and normalize(view["arch_prev"]) != expected:
                raise RuntimeError("Preserve customized HR preferences history")
            if key == f"hr.{child_name}" and current == expected:
                plan = source.record_id, target
            elif current != expected and (key != f"hr.{child_name}" or current != normalize(target)):
                raise RuntimeError("Preserve customized HR preferences architecture")
        except (ElementTree.ParseError, TypeError, ValueError) as error:
            raise RuntimeError("Unsupported HR preferences architecture") from error
    return plan


def _hr_type_plan(cr: Any) -> HrTypePlan | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'hr'")
    if cr.fetchone() not in {("installed",), ("to upgrade",)}:
        return None
    models = _rows(cr, "SELECT id, model, state FROM ir_model WHERE model IN ('hr.contract.type','hr.employee.type') FOR UPDATE")
    cr.execute("SELECT to_regclass('hr_contract_type'), to_regclass('hr_employee_type')")
    tables = cr.fetchone()
    if not models and tables == (None, None):
        return None
    if len(models) != 1 or models[0]["state"] != "base":
        raise RuntimeError("Ambiguous HR type model transition")
    source_model = models[0]["model"]
    completed = source_model == "hr.employee.type"
    if tables != ((None, "hr_employee_type") if completed else ("hr_contract_type", None)):
        raise RuntimeError("Mixed HR type model storage")
    utility = import_module("odoo.upgrade.util")
    paths = runpy.run_path(str(Path(__file__).with_name("odoo_upgrade_paths.py")))
    checkout = Path(utility.__file__ or "").resolve().parents[2]
    if paths["resolve_upgrade_util"]([checkout.parent.parent]) != checkout / "src":
        raise RuntimeError("Unsupported native HR migration utility")
    source_table = "hr_employee_type" if completed else "hr_contract_type"
    source_field = "employee_type_id" if completed else "contract_type_id"
    other_field = "contract_type_id" if completed else "employee_type_id"
    unavailable_model = "hr.contract.type" if completed else "hr.employee.type"
    cr.execute("SELECT id FROM ir_model_fields WHERE relation=%s LIMIT 1", (unavailable_model,))
    if cr.fetchone() is not None:
        raise RuntimeError("Mixed HR type relational references")
    for column, expected in (("id", "int4"), ("name", "jsonb"), ("code", "varchar"), ("sequence", "int4"), ("country_id", "int4")):
        if utility.column_type(cr, source_table, column) != expected:
            raise RuntimeError("Unsupported HR type data storage")
    fields = _rows(
        cr,
        "SELECT * FROM ir_model_fields WHERE model IN ('hr.contract.type','hr.employee.type') "
        "OR (model IN ('hr.job','hr.version','hr.employee') AND name IN ('contract_type_id','employee_type_id')) FOR UPDATE",
    )
    relation_fields = [field for field in fields if field["model"] in HR_TYPE_FIELD_OWNERS]
    cr.execute(
        "SELECT id FROM ir_model_fields WHERE ttype='many2many' "
        "AND (model IN ('hr.contract.type','hr.employee.type') OR relation IN ('hr.contract.type','hr.employee.type')) LIMIT 1"
    )
    if cr.fetchone() is not None or utility.get_m2m_on(cr, source_table):
        raise RuntimeError("Unsupported HR type many-to-many storage")
    cr.execute("SELECT id FROM ir_model_fields WHERE relation=%s", (source_model,))
    if {row[0] for row in cr.fetchall()} != {field["id"] for field in relation_fields}:
        raise RuntimeError("Unsupported additional HR type relation fields")
    if len({(field["model"], field["name"]) for field in fields}) != len(fields):
        raise RuntimeError("Ambiguous HR type field metadata")
    if any(field["model"] not in {*HR_TYPE_FIELD_OWNERS, source_model} for field in fields):
        raise RuntimeError("Mixed HR type field owners")
    type_fields = {field["name"]: field for field in fields if field["model"] == source_model}
    for name, kind, relation in (
        ("id", "integer", None),
        ("name", "char", None),
        ("code", "char", None),
        ("sequence", "integer", None),
        ("country_id", "many2one", "res.country"),
    ):
        field = type_fields.get(name)
        if field is None or field["ttype"] != kind or field["relation"] != relation or not field.get("store"):
            raise RuntimeError("Unsupported HR type field identity")
    if not completed and {"company_id", "country_code", "employees_count", "employee_ids"} & type_fields.keys():
        raise RuntimeError("Colliding new native HR type fields")
    owners = _rows(cr, "SELECT id,model FROM ir_model WHERE model IN ('hr.job','hr.version','hr.employee') FOR UPDATE")
    owner_ids = {owner["model"]: owner["id"] for owner in owners}
    owner_ids[source_model] = models[0]["id"]
    if set(owner_ids) != {*HR_TYPE_FIELD_OWNERS, source_model} or any(
        field["model_id"] != owner_ids[field["model"]] for field in fields
    ):
        raise RuntimeError("Rebound HR type field ownership")
    if {field["model"] for field in relation_fields} != set(HR_TYPE_FIELD_OWNERS) or len(relation_fields) != len(
        HR_TYPE_FIELD_OWNERS
    ):
        raise RuntimeError("Incomplete or colliding HR type field cohort")
    for field in relation_fields:
        if field["name"] != source_field or field["ttype"] != "many2one" or field["relation"] != source_model:
            raise RuntimeError("Mixed HR type field metadata")
        table = field["model"].replace(".", "_")
        if not utility.table_exists(cr, table) or utility.column_exists(cr, table, other_field):
            raise RuntimeError("Colliding HR type field storage")
        if field["model"] == "hr.employee":
            if field.get("related") != f"version_id.{source_field}" or field.get("store"):
                raise RuntimeError("Unsupported delegated HR type field")
        elif not field.get("store") or field.get("related"):
            raise RuntimeError("Unsupported stored HR type field")
        column_type = utility.column_type(cr, table, source_field)
        if (field.get("store") and column_type is None) or column_type not in {None, "int4"}:
            raise RuntimeError("Unsupported HR type relation column")
        if not completed:
            index_name = import_module("odoo.tools.sql").make_index_name(table, other_field)
            cr.execute("SELECT to_regclass(%s)", (index_name,))
            if cr.fetchone()[0] is not None:
                raise RuntimeError("Colliding HR type field index")
        alias = _require_source(cr, "hr", f"field_{table}__{source_field}", "ir.model.fields")
        if alias.record_id != field["id"]:
            raise RuntimeError("Rebound HR type field alias")
    model_name = "model_hr_employee_type" if completed else "model_hr_contract_type"
    if _require_source(cr, "hr", model_name, "ir.model").record_id != models[0]["id"]:
        raise RuntimeError("Rebound HR type model alias")
    if not completed:
        cr.execute("SELECT relname FROM pg_class WHERE relname LIKE %s ESCAPE '\\'", ("%hr\\_employee\\_type%",))
        if cr.fetchall():
            raise RuntimeError("Colliding HR type table, sequence or index")
    fields_by_id = {field["id"]: field for field in fields}
    aliases = _rows(
        cr,
        "SELECT module,name,model,res_id,noupdate FROM ir_model_data "
        "WHERE (model='ir.model' AND (res_id=%s OR name='model_hr_contract_type')) "
        "OR (model='ir.model.fields' AND (res_id=ANY(%s) OR name LIKE %s ESCAPE '\\' OR name LIKE %s ESCAPE '\\')) FOR UPDATE",
        (models[0]["id"], list(fields_by_id), "field\\_hr\\_contract\\_type\\_\\_%", "field\\_hr\\_employee\\_type\\_\\_%"),
    )
    for alias in aliases:
        destination = alias["name"]
        if alias["model"] == "ir.model" and destination == "model_hr_contract_type":
            if alias["res_id"] != models[0]["id"]:
                raise RuntimeError("Rebound HR type model aliases")
            destination = "model_hr_employee_type"
        elif alias["model"] == "ir.model.fields":
            field = fields_by_id.get(alias["res_id"])
            if field is None or (not completed and destination.startswith("field_hr_employee_type__")):
                raise RuntimeError("Rebound or mixed HR type field aliases")
            if field["model"] in HR_TYPE_FIELD_OWNERS:
                conventional = f"field_{field['model'].replace('.', '_')}__{source_field}"
                if alias["module"] != "__export__" and (alias["name"] != conventional or alias["noupdate"]):
                    raise RuntimeError("Preserve functional custom HR type field aliases")
                if not completed and alias["module"] != "__export__":
                    destination = conventional.removesuffix(source_field) + other_field
            elif destination.startswith("field_hr_contract_type__"):
                destination = destination.replace("field_hr_contract_type__", "field_hr_employee_type__", 1)
        if completed and destination != alias["name"]:
            raise RuntimeError("Mixed HR type metadata aliases")
        if not completed and destination != alias["name"]:
            if alias["module"] != "hr":
                raise RuntimeError("Preserve functional custom HR type metadata aliases")
            if _reference(cr, alias["module"], destination) is not None:
                raise RuntimeError("Colliding HR type metadata alias")
    document = ElementTree.parse(HR_TYPE_DATA_FILE).getroot()
    records = document.findall("./data/record")
    if (
        document.tag != "odoo"
        or document.attrib
        or not records
        or any(data.attrib != {"noupdate": "1"} for data in document.findall("data"))
        or any(record.get("model") != "hr.employee.type" or not record.get("id") for record in records)
    ):
        raise RuntimeError("Unsupported native HR type data ownership")
    native_names = {record.attrib["id"] for record in records}
    data_aliases = _rows(
        cr,
        "SELECT module,name,model,res_id,noupdate FROM ir_model_data "
        "WHERE model IN ('hr.contract.type','hr.employee.type') "
        "OR (module='hr' AND name LIKE %s ESCAPE '\\') FOR UPDATE",
        ("contract\\_type\\_%",),
    )
    for alias in data_aliases:
        if alias["model"] != source_model:
            raise RuntimeError("Rebound or mixed HR type data aliases")
        if (
            not alias["noupdate"]
            and alias["module"] != "__export__"
            and (alias["module"] != "hr" or alias["name"] not in native_names)
        ):
            raise RuntimeError("Preserve HR type data omitted by native20")
    cr.execute(
        utility.format_query(
            cr,
            "SELECT d.id FROM ir_model_data d LEFT JOIN {} t ON t.id=d.res_id WHERE d.model=%s AND t.id IS NULL",
            source_table,
        ),
        (source_model,),
    )
    if cr.fetchall():
        raise RuntimeError("Dangling HR type data aliases")
    employee_type_id = None
    version_ids = []
    if utility.column_exists(cr, "hr_version", "employee_type"):
        cr.execute(
            utility.format_query(
                cr, "SELECT id,employee_type,{} FROM hr_version WHERE employee_type IS NOT NULL FOR UPDATE", source_field
            )
        )
        versions = cr.fetchall()
        if any(category != "employee" for _, category, _ in versions):
            raise RuntimeError("Preserve legacy HR employment categories before native field removal")
        version_ids = [identity for identity, _, existing_type in versions if existing_type is None]
        if version_ids:
            legacy_fields = _rows(cr, "SELECT * FROM ir_model_fields WHERE model='hr.version' AND name='employee_type' FOR UPDATE")
            if (
                utility.column_type(cr, "hr_version", "employee_type") != "varchar"
                or len(legacy_fields) != 1
                or legacy_fields[0]["model_id"] != owner_ids["hr.version"]
                or legacy_fields[0]["ttype"] != "selection"
                or legacy_fields[0]["state"] != "base"
                or not legacy_fields[0].get("store")
                or legacy_fields[0].get("related")
                or legacy_fields[0].get("relation")
            ):
                raise RuntimeError("Unsupported legacy HR Employee classification storage")
            if sum(record.get("id") == "contract_type_employee" for record in records) != 1:
                raise RuntimeError("Native HR data does not identify one Employee type")
            employee_type_id = _require_source(cr, "hr", "contract_type_employee", source_model).record_id
            cr.execute(utility.format_query(cr, "SELECT id FROM {} WHERE id=%s FOR UPDATE", source_table), (employee_type_id,))
            if cr.fetchall() != [(employee_type_id,)]:
                raise RuntimeError("Missing existing HR Employee type")
    return None if completed and not version_ids else HrTypePlan(None if completed else utility, employee_type_id, version_ids)


def _migrate_hr_types(cr: Any, plan: HrTypePlan) -> None:
    utility = plan.utility
    if utility is not None:
        utility.rename_table(cr, "hr_contract_type", "hr_employee_type", remove_constraints=False)
        utility.rename_model(cr, "hr.contract.type", "hr.employee.type", rename_table=False)
        for model in HR_TYPE_FIELD_OWNERS:
            utility.rename_field(cr, model, "contract_type_id", "employee_type_id", skip_inherit="*")
    if plan.version_ids:
        cr.execute(
            "UPDATE hr_version SET employee_type_id=%s WHERE id=ANY(%s) AND employee_type_id IS NULL",
            (plan.employee_type_id, plan.version_ids),
        )


def migrate(cr: Any, version: str | None) -> None:
    major = (version or "").split(".", 1)[0]
    if major == "20":
        return
    if major != "19":
        raise RuntimeError("Core reference preparation requires a native 19-to-20 crossing")
    state = _state_source(cr)
    website = _website_source(cr)
    state_target = _check_target(cr, TARGET_STATE, state, protect=state.noupdate)
    website_target = _check_target(cr, "default_website", website, protect=True) if website is not None else None
    inherited_views = [(view, _inherited_view_source(cr, view)) for view in INHERITED_VIEWS]
    empty_chats = _empty_chat_plan(cr)
    lunches = _lunch_plan(cr)
    policies = _policy_xmlid_plan(cr)
    stock_valuation = _stock_valuation_action_plan(cr)
    stock_views = _stock_product_view_plan(cr)
    calendar_views = _calendar_view_plan(cr)
    hr_preferences = _hr_preferences_view_plan(cr)
    hr_types = _hr_type_plan(cr)
    hr_org_chart = _hr_org_chart_action_plan(cr)
    hr_moved: dict[str, Reference] = {}
    if hr_org_chart is not None:
        hr_moved["action_hr_employee_public_org_chart"] = hr_org_chart
        private = _hr_org_chart_action_plan(cr, private=True)
        if private is None:
            raise RuntimeError("Missing private Org Chart source owner")
        hr_moved["action_hr_employee_org_chart"] = private
        hr_moved["act_hr_employee_public_hierarchy_view"] = _hr_org_chart_link_plan(cr)
        name = "act_open_view_employee_list_my_hierarchy_view"
        hr_moved[name] = _hr_org_chart_link_plan(
            cr, name, path=HR_EMPLOYEE_DATA_FILE, parent="open_view_employee_list_my", model="hr.employee"
        )
        for suffix, parent in (("kanban", "hr_department_kanban_action"), ("tree", "hr_department_tree_action")):
            name = f"act_hr_department_hierarchy_view_{suffix}_action"
            hr_moved[name] = _hr_org_chart_link_plan(
                cr, name, path=HR_DEPARTMENT_DATA_FILE, parent=parent, model="hr.department", sequence=25
            )
    hr_org_chart_views = _hr_org_chart_view_plan(cr) if hr_org_chart is not None else None
    _admit_mail(cr)
    for (module, name), source in stock_views.items():
        _bind_target(cr, name, source, module=module)
    if hr_types is not None:
        _migrate_hr_types(cr, hr_types)
    if hr_preferences is not None:
        record_id, architecture = hr_preferences
        cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", (json.dumps({"en_US": architecture}), record_id))
    if hr_org_chart_views is not None:
        hierarchy, extension_change = hr_org_chart_views
        _bind_moved_action(cr, hierarchy, "hr_employee_public_hierarchy_view", "hr_org_chart", "hr")
        if extension_change is not None:
            cr.execute(
                "UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", (json.dumps({"en_US": "<data/>"}), extension_change)
            )
    for record_id in calendar_views:
        cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", (json.dumps({"en_US": "<data/>"}), record_id))
    if state_target is None:
        _bind_target(cr, TARGET_STATE, state)
    if website is not None and website_target is None:
        _bind_target(cr, "default_website", website)
    for view, change in inherited_views:
        if change is None:
            continue
        record_id, architectures = change
        cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", (json.dumps(architectures), record_id))
        _logger.info("Retargeted %s.%s on preserved ir.ui.view row %s", view.module, view.name, record_id)
    _retire_empty_chats(cr, empty_chats)
    if lunches:
        _identified(cr, "DELETE FROM resource_calendar_attendance WHERE id = ANY(%s)", (), (lunches,))
        _logger.info("Removed %s redundant nonworking lunch rows; preserved working intervals", len(lunches))
    _release_policy_xmlids(cr, policies)
    if stock_valuation is not None:
        _bind_moved_action(cr, stock_valuation, "action_report_stock_valuation", "stock_account", "account")
    for name, source in hr_moved.items():
        _bind_moved_action(cr, source, name, "hr_org_chart", "hr")
