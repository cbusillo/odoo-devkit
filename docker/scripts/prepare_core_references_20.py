# Copyright 2026 Hunki Enterprises BV (_release_policy_xmlids only)
# License AGPL-3.0 or later for _release_policy_xmlids (https://www.gnu.org/licenses/agpl).

import csv
import json
import logging
from ast import literal_eval
from datetime import datetime
from importlib import import_module
from math import isfinite
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree

SOURCE_STATE = "state_in_or"
TARGET_STATE = "state_in_od"
STATE_DATA_FILE = Path("/odoo/odoo/addons/base/data/res.country.state.csv")
STOCK_VALUATION_DATA_FILE = Path("/odoo/addons/account/report/account_stock_valuation_report.xml")
_logger = logging.getLogger(__name__)


class Reference(NamedTuple):
    model: str
    record_id: int
    noupdate: bool


class DirectChat(NamedTuple):
    values: dict[str, Any]
    members: list[dict[str, Any]]


class InheritedView(NamedTuple):
    module: str
    name: str
    model: str
    parent: str
    source_selector: str
    target_selector: str


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
    records = ElementTree.parse(STOCK_VALUATION_DATA_FILE).findall(f"./record[@id='{name}']")
    if len(records) != 1 or records[0].get("model") != source.model:
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
    cr.execute(
        "SELECT module, name, model FROM ir_model_data WHERE model LIKE 'ir.actions.%%' AND res_id = %s FOR UPDATE",
        (source.record_id,),
    )
    aliases = {("stock_account", name, source.model)} | ({("account", name, source.model)} if target is not None else set())
    if set(cr.fetchall()) != aliases:
        raise RuntimeError("Customized stock valuation action aliases")
    try:
        context = literal_eval(action["context"])
        native_context = literal_eval(native["context"])
    except (ValueError, SyntaxError, TypeError) as error:
        raise RuntimeError("Unsupported stock valuation action context") from error
    old_model = "stock_account.stock.valuation.report"
    if native["res_model"] != "account.stock.valuation.report" or native_context != {"model": native["res_model"]}:
        raise RuntimeError("Unsupported native stock valuation report model")
    if action["res_model"] not in {old_model, native["res_model"]} or context != {"model": action["res_model"]}:
        raise RuntimeError("Customized stock valuation report model or context")
    if action["res_model"] != old_model and target is None:
        raise RuntimeError("Unowned converted stock valuation action")
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


def _bind_stock_valuation_action(cr: Any, source: Reference) -> None:
    name = "action_report_stock_valuation"
    if _reference(cr, "account", name) is None:
        _bind_target(cr, name, Reference(source.model, source.record_id, False), module="account")
    cr.execute("UPDATE ir_model_data SET noupdate = true WHERE module = 'stock_account' AND name = %s", (name,))


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


def _check_references(cr: Any, ids: dict[str, list[int]], internal_relations: set[tuple[str, str, str]]) -> None:
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
                _identified(
                    cr,
                    "SELECT 1 FROM %s record WHERE %s = ANY(%s) AND "
                    "(EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value WHERE value.value = %s) "
                    "OR EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value "
                    "WHERE value.key = ANY(%s) AND value.value = %s)) LIMIT 1",
                    (table, column),
                    (
                        [str(record) for record in ids[model.replace(".", "_")]] if text_storage else ids[model.replace(".", "_")],
                        model,
                        list(model_columns.get(table, ())),
                        str(model_id),
                    ),
                )
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
        _bind_stock_valuation_action(cr, stock_valuation)
