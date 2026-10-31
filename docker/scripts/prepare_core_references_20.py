import csv
import json
import logging
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, NamedTuple

SOURCE_STATE = "state_in_or"
TARGET_STATE = "state_in_od"
STATE_DATA_FILE = Path("/odoo/odoo/addons/base/data/res.country.state.csv")
_logger = logging.getLogger(__name__)


class Reference(NamedTuple):
    model: str
    record_id: int
    noupdate: bool


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


def _check_target(cr: Any, name: str, source: Reference, *, protect: bool = False) -> Reference | None:
    target = _reference(cr, "base", name)
    if target is not None and (
        target.model != source.model or target.record_id != source.record_id or (protect and not target.noupdate)
    ):
        raise RuntimeError(f"Conflicting core reference base.{name}")
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


def _bind_target(cr: Any, name: str, source: Reference) -> None:
    cr.execute(
        "INSERT INTO ir_model_data (module, name, model, res_id, noupdate) VALUES ('base', %s, %s, %s, %s)",
        (name, source.model, source.record_id, source.noupdate),
    )
    _logger.info("Bound core reference base.%s to preserved %s row %s", name, source.model, source.record_id)


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
    cr.execute(sql(query, *(sql.identifier(name) for name in identifiers), *parameters))


def _chat_groups(cr: Any) -> list[list[dict[str, Any]]]:
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
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for channel in channels:
        audience = audiences[channel["id"]]
        if not audience or any(bool(member["partner_id"]) == bool(member["guest_id"]) for member in audience):
            raise RuntimeError("Unsupported direct chat audience")
        identities = tuple(sorted((member["partner_id"] or 0, member["guest_id"] or 0) for member in audience))
        channel["members"] = audience
        groups.setdefault(identities, []).append(channel)
    return [group for group in groups.values() if len(group) > 1]


def _require_empty(row: dict[str, Any], ignored: set[str], defaults: dict[str, tuple]) -> None:
    for name, value in row.items():
        if name in ignored:
            continue
        if value not in defaults.get(name, (None,)):
            raise RuntimeError(f"Customized or unsupported direct chat state: {name}")


def _pinned(member: dict[str, Any], channel: dict[str, Any]) -> bool:
    unpin = member["unpin_dt"]
    return not unpin or any(
        value is not None and value >= unpin for value in (member["last_interest_dt"], channel["last_interest_dt"])
    )


def _check_creation_activity(row: dict[str, Any], fields: tuple[str, ...]) -> None:
    if not row.get("create_date"):
        raise RuntimeError("Unknown direct chat creation state")
    created = datetime.fromisoformat(str(row["create_date"]))
    if any(row.get(field) is not None and datetime.fromisoformat(str(row[field])) > created for field in fields):
        raise RuntimeError("Direct chat has activity after creation")


def _check_chat_state(cr: Any, group: list[dict[str, Any]]) -> None:
    identities = [member["partner_id"] for member in group[0]["members"]]
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
        _check_creation_activity(channel, ("last_interest_dt",))
        if channel["name"] not in generated_names or not channel["active"]:
            raise RuntimeError("Customized duplicate direct chat name or visibility")
        _require_empty(
            channel,
            {"id", "name", "uuid", "members", "create_uid", "write_uid", "create_date", "write_date", "last_interest_dt"},
            {
                "channel_type": ("chat",),
                "active": (True,),
                "allow_public_upload": (None, False),
                "livechat_active": (None, False),
                "rating_last_value": (None, 0),
                "livechat_is_escalated": (None, False),
                "livechat_start_hour": (None, 0),
                "has_crm_lead": (None, False),
                "is_pending_chat_request": (None, False),
            },
        )
        for member in channel["members"]:
            _check_creation_activity(member, ("last_interest_dt", "unpin_dt"))
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
    pinned = {member["partner_id"]: _pinned(member, survivor) for member in survivor["members"]}
    if any(_pinned(member, channel) != pinned[member["partner_id"]] for channel in group for member in channel["members"]):
        raise RuntimeError("Duplicate direct chats have different sidebar state")


def _check_chat_references(cr: Any, groups: list[list[dict[str, Any]]]) -> None:
    ids = {
        "discuss_channel": [channel["id"] for group in groups for channel in group],
        "discuss_channel_member": [member["id"] for group in groups for channel in group for member in channel["members"]],
    }
    cr.execute(
        "SELECT source_ns.nspname, source.relname, source_col.attname, target.relname, target_col.attname, "
        "cardinality(fk.conkey) FROM pg_constraint fk "
        "JOIN pg_class source ON source.oid = fk.conrelid JOIN pg_namespace source_ns ON source_ns.oid = source.relnamespace "
        "JOIN pg_class target ON target.oid = fk.confrelid JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace "
        "JOIN pg_attribute source_col ON source_col.attrelid = source.oid AND source_col.attnum = fk.conkey[1] "
        "JOIN pg_attribute target_col ON target_col.attrelid = target.oid AND target_col.attnum = fk.confkey[1] "
        "WHERE fk.contype = 'f' AND target_ns.nspname = 'public' "
        "AND target.relname IN ('discuss_channel', 'discuss_channel_member', 'ir_model')"
    )
    model_columns: dict[str, set[str]] = {}
    foreign_keys: set[tuple[str, str, str]] = set()
    for schema, table, column, target, target_column, width in cr.fetchall():
        if schema != "public" or target_column != "id" or width != 1:
            raise RuntimeError("Unsupported direct chat reference constraint")
        foreign_keys.add((table, column, target))
        if target == "ir_model":
            model_columns.setdefault(table, set()).add(column)
            continue
        if (table, column, target) == ("discuss_channel_member", "channel_id", "discuss_channel"):
            continue
        _identified(cr, "SELECT 1 FROM %s WHERE %s = ANY(%s) LIMIT 1", (table, column), (ids[target],))
        if cr.fetchone():
            raise RuntimeError(f"Referenced direct chat or member: {table}.{column}")
    cr.execute(
        "SELECT name FROM ir_model_fields WHERE store AND ttype = 'many2one_reference' "
        "UNION SELECT 'res_id' UNION SELECT 'alias_force_thread_id' UNION SELECT 'alias_parent_thread_id'"
    )
    indirect_columns = {row[0] for row in cr.fetchall()}
    cr.execute(
        "SELECT 1 FROM ir_model_fields WHERE store AND company_dependent "
        "AND relation IN ('discuss.channel', 'discuss.channel.member') LIMIT 1"
    )
    if cr.fetchone():
        raise RuntimeError("Unsupported company-dependent direct chat reference")
    cr.execute("SELECT id, model FROM ir_model WHERE model IN ('discuss.channel', 'discuss.channel.member')")
    model_ids = {model: model_id for model_id, model in cr.fetchall()}
    if set(model_ids) != {"discuss.channel", "discuss.channel.member"}:
        raise RuntimeError("Missing direct chat model metadata")
    cr.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
    )
    columns: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in cr.fetchall():
        columns.setdefault(table, []).append((column, data_type))
    cr.execute(
        "SELECT model, name, ttype, relation, relation_table FROM ir_model_fields WHERE store "
        "AND relation IN ('discuss.channel', 'discuss.channel.member') AND ttype IN ('many2one', 'many2many')"
    )
    for model, name, kind, relation, relation_table in cr.fetchall():
        table = relation_table if kind == "many2many" else model.replace(".", "_")
        if not any(
            owner == table and target == relation.replace(".", "_") and (kind == "many2many" or column == name)
            for owner, column, target in foreign_keys
        ):
            raise RuntimeError(f"Unsupported direct chat relational storage: {model}.{name}")
    if "ir_default" in columns:
        cr.execute(
            "SELECT 1 FROM ir_default value JOIN ir_model_fields field ON field.id = value.field_id "
            "WHERE field.relation IN ('discuss.channel', 'discuss.channel.member') LIMIT 1"
        )
        if cr.fetchone():
            raise RuntimeError("Unsupported direct chat field default")
    for table, fields in columns.items():
        for column, data_type in fields:
            if data_type in {"text", "character varying", "character"}:
                references = [f"{model},{record}" for model in model_ids for record in ids[model.replace(".", "_")]]
                _identified(cr, "SELECT 1 FROM %s WHERE %s = ANY(%s) LIMIT 1", (table, column), (references,))
                if cr.fetchone():
                    raise RuntimeError(f"Referenced direct chat or member: {table}.{column}")
            if column not in indirect_columns:
                continue
            for model, model_id in model_ids.items():
                _identified(
                    cr,
                    "SELECT 1 FROM %s record WHERE to_jsonb(record)->>%s = ANY(%s) AND "
                    "(EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value WHERE value.value = %s) "
                    "OR EXISTS (SELECT 1 FROM jsonb_each_text(to_jsonb(record)) value "
                    "WHERE value.key = ANY(%s) AND value.value = %s)) LIMIT 1",
                    (table,),
                    (
                        column,
                        [str(record) for record in ids[model.replace(".", "_")]],
                        model,
                        list(model_columns.get(table, ())),
                        str(model_id),
                    ),
                )
                if cr.fetchone():
                    raise RuntimeError(f"Indirectly referenced direct chat or member: {table}.{column}")
    cr.execute(
        "SELECT 1 FROM pg_trigger WHERE tgrelid IN ('discuss_channel'::regclass, 'discuss_channel_member'::regclass) "
        "AND NOT tgisinternal LIMIT 1"
    )
    if cr.fetchone():
        raise RuntimeError("Unsupported direct chat deletion trigger")


def _empty_chat_plan(cr: Any) -> list[list[dict[str, Any]]]:
    groups = _chat_groups(cr)
    for group in groups:
        _check_chat_state(cr, group)
    if groups:
        _check_chat_references(cr, groups)
    return groups


def _retire_empty_chats(cr: Any, groups: list[list[dict[str, Any]]]) -> None:
    for group in groups:
        for channel in group[1:]:
            cr.execute("DELETE FROM discuss_channel_member WHERE channel_id = %s", (channel["id"],))
            cr.execute("DELETE FROM discuss_channel WHERE id = %s", (channel["id"],))
            _logger.info("Retired redundant empty direct chat %s; preserved chat %s", channel["id"], group[0]["id"])


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
