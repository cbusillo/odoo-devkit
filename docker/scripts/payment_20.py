from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

AUDIT = {"create_uid", "create_date", "write_uid", "write_date"}
IMAGE_FIELDS = {"image", "image_payment_form"}
RELATIONS = {
    "providers": ("payment_method_payment_provider_rel", "payment_provider_id", "payment.provider"),
    "countries": ("payment_method_res_country_rel", "res_country_id", "res.country"),
    "currencies": ("payment_method_res_currency_rel", "res_currency_id", "res.currency"),
}


@dataclass
class Retirement:
    methods: list[int]
    attachments: list[int]
    aliases: list[int]
    files: dict[Path, dict[str, Any]]


def _fail(subject: str) -> None:
    raise RuntimeError(f"Unqualified stock payment retirement: {subject}")


def _semantic_values(values: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    if values.keys() != expected.keys():
        _fail("stored columns or material fields")
    result = dict(values)
    for field, value in expected.items():
        if type(value) is bool:
            observed = result.get(field)
            if observed is not None and type(observed) is not bool:
                _fail("Boolean storage")
            result[field] = bool(observed)
    return result


def source_witness(profile: dict[str, Any], version: str) -> dict[str, Any]:
    witness = profile.get("payment")
    if not isinstance(witness, dict):
        _fail("missing public source witness")
    if "database_uuid" in witness or witness.get("source_version") != version or version.split(".", 1)[0] != "19":
        _fail("public source version or provenance")
    files = witness.get("source_files")
    if (
        not isinstance(files, dict)
        or not files
        or not re.fullmatch(r"[0-9a-f]{40}", witness.get("source_commit", ""))
        or any(not isinstance(path, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for path, digest in files.items())
    ):
        _fail("public source bindings")
    for family in ("methods", "providers"):
        records = witness.get(family)
        if not isinstance(records, dict) or not records or any(not key.startswith("payment.") for key in records):
            _fail("public source family")
    qualification = witness.get("asset_qualification")
    if (
        not isinstance(qualification, dict)
        or set(qualification) != {"public_source_receipt_sha256", "original_representation_receipt_sha256"}
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in qualification.values())
    ):
        _fail("qualified image provenance")
    if not isinstance(witness.get("attachment_defaults"), dict):
        _fail("public attachment defaults")
    return witness


def _owned(rows: list[dict[str, Any]], aliases: list[dict[str, Any]], expected: dict[str, Any], model: str) -> dict[int, str]:
    identifiers = {row["id"] for row in rows}
    if len(identifiers) != len(rows) or any(type(identifier) is not int or identifier <= 0 for identifier in identifiers):
        _fail("duplicate row identity")
    result = {}
    for alias in aliases:
        if set(alias) - AUDIT != {"id", "module", "name", "model", "res_id", "noupdate"}:
            _fail("alias material metadata")
        key = alias["module"] + "." + alias["name"]
        identity = alias["res_id"]
        if (
            type(alias["id"]) is not int
            or alias["id"] <= 0
            or type(identity) is not int
            or alias["model"] != model
            or alias["module"] != "payment"
            or alias["noupdate"] is not True
            or identity not in identifiers
            or identity in result
            or key not in expected
        ):
            _fail("source ownership")
        result[identity] = key
    if result.keys() != identifiers or set(result.values()) != expected.keys() or len(result.values()) != len(set(result.values())):
        _fail("source alias family")
    return result


def _image_file(attachment: dict[str, Any], expected: dict[str, Any], filestore: Path) -> Path:
    checksum = attachment["checksum"]
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{40}", checksum):
        _fail("attachment checksum")
    if attachment["store_fname"] != checksum[:2] + "/" + checksum or attachment["has_db_datas"]:
        _fail("attachment storage")
    root = filestore.resolve(strict=True)
    path = root / attachment["store_fname"]
    if path.parent.is_symlink() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        _fail("attachment path")
    body = path.read_bytes()
    if len(body) != attachment["file_size"] or hashlib.sha1(body).hexdigest() != checksum:
        _fail("original attachment bytes")
    if expected["sha1"] != checksum or expected["bytes"] != len(body) or hashlib.sha256(body).hexdigest() != expected["sha256"]:
        _fail("qualified original image representation")
    return path


def admit(snapshot: dict[str, Any], witness: dict[str, Any], filestore: Path) -> Retirement:
    if snapshot["relation_columns"] != {
        table: sorted(("payment_method_id", column)) for table, column, _model in RELATIONS.values()
    }:
        _fail("stock relation material columns")
    methods = _owned(snapshot["methods"], snapshot["method_aliases"], witness["methods"], "payment.method")
    providers = _owned(snapshot["providers"], snapshot["provider_aliases"], witness["providers"], "payment.provider")
    if snapshot["company_ids"] != [snapshot["main_company"]] or snapshot["tokens"] or snapshot["transactions"]:
        _fail("company or payment history")
    for row in snapshot["providers"]:
        expected = witness["providers"][providers[row["id"]]]
        if (
            row["code"] != "none"
            or row["state"] != "disabled"
            or row["company_id"] != snapshot["main_company"]
            or snapshot["modules"].get(row["module_id"]) != {"xmlid": expected["module"], "state": "uninstalled"}
        ):
            _fail("configured provider")
    expected_links = {key: set() for key in witness["providers"]}
    for provider_id, method_id in snapshot["provider_links"]:
        if provider_id not in providers or method_id not in methods:
            _fail("provider association identity")
        expected_links[providers[provider_id]].add(methods[method_id])
    if expected_links != {key: set(row["methods"]) for key, row in witness["providers"].items()}:
        _fail("provider stock associations")
    for row in snapshot["methods"]:
        expected = witness["methods"][methods[row["id"]]]["values"]
        values = {key: value for key, value in row.items() if key not in AUDIT | {"id"}}
        parent = values["primary_payment_method_id"]
        if parent is not None and parent not in methods:
            _fail("outside brand parent")
        values["primary_payment_method_id"] = methods.get(parent)
        values["supported_country_ids"] = snapshot["countries"][row["id"]]
        values["supported_currency_ids"] = snapshot["currencies"][row["id"]]
        if _semantic_values(values, expected) != expected:
            _fail("method values, translations or relations")
    if snapshot["attachment_aliases"]:
        _fail("image aliases")
    seen = set()
    files = {}
    for row in snapshot["attachments"]:
        field = row["res_field"]
        if row["res_id"] not in methods or field not in IMAGE_FIELDS or (row["res_id"], field) in seen:
            _fail("extra or duplicate attachment")
        seen.add((row["res_id"], field))
        if (
            row["res_model"] != "payment.method"
            or row["name"] != field
            or row["type"] != "binary"
            or row["mimetype"] != "image/png"
            or row["company_id"] != snapshot["main_company"]
        ):
            _fail("attachment identity or metadata")
        dynamic = {
            "id",
            "name",
            "res_model",
            "res_field",
            "res_id",
            "type",
            "mimetype",
            "company_id",
            "store_fname",
            "checksum",
            "file_size",
            "has_db_datas",
        }
        metadata = {key: value for key, value in row.items() if key not in AUDIT | dynamic}
        if _semantic_values(metadata, witness["attachment_defaults"]) != witness["attachment_defaults"]:
            _fail("attachment customization or unknown fields")
        expected_image = witness["methods"][methods[row["res_id"]]]["images"][field]
        path = _image_file(row, expected_image, filestore)
        files[path] = dict(expected_image)
    if seen != {(identifier, field) for identifier in methods for field in IMAGE_FIELDS}:
        _fail("missing stock image")
    return Retirement(
        sorted(methods),
        sorted(row["id"] for row in snapshot["attachments"]),
        sorted(row["id"] for row in snapshot["method_aliases"]),
        files,
    )


def _rows(cr: Any, query: str, parameters: tuple = ()) -> list[Any]:
    cr.execute(query, parameters)
    return cr.fetchall()


def _logical_links(cr: Any, table: str, column: str, model: str, methods: list[int]) -> dict[int, list[str]]:
    SQL = import_module("odoo.tools").SQL

    cr.execute(
        SQL(
            "SELECT payment_method_id, %s FROM %s ORDER BY payment_method_id, %s",
            SQL.identifier(column),
            SQL.identifier(table),
            SQL.identifier(column),
        )
    )
    pairs = cr.fetchall()
    aliases = _rows(
        cr,
        "SELECT res_id,module||'.'||name FROM ir_model_data WHERE model=%s AND module='base' AND res_id=ANY(%s)",
        (model, sorted({target for _source, target in pairs})),
    )
    by_id = {}
    for identifier, alias in aliases:
        if identifier in by_id:
            _fail("ambiguous relation identity")
        by_id[identifier] = alias
    result = {identifier: [] for identifier in methods}
    for identifier, target in pairs:
        if identifier not in result or target not in by_id or by_id[target] in result[identifier]:
            _fail("stock relation identity")
        result[identifier].append(by_id[target])
    return {identifier: sorted(values) for identifier, values in result.items()}


def capture(cr: Any) -> dict[str, Any]:
    cr.execute(
        "LOCK TABLE payment_method,payment_provider,ir_attachment,ir_model_data,ir_module_module,res_company,"
        "payment_method_payment_provider_rel,payment_method_res_country_rel,payment_method_res_currency_rel "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    if _rows(
        cr,
        "SELECT 1 FROM pg_trigger WHERE tgrelid=ANY(%s::regclass[]) AND NOT tgisinternal LIMIT 1",
        ([relation[0] for relation in RELATIONS.values()],),
    ):
        _fail("stock relation trigger")
    methods = [row[0] for row in _rows(cr, "SELECT to_jsonb(m) FROM payment_method m ORDER BY id")]
    providers = [row[0] for row in _rows(cr, "SELECT to_jsonb(p) FROM payment_provider p ORDER BY id")]
    aliases = [
        row[0]
        for row in _rows(
            cr, "SELECT to_jsonb(d) FROM ir_model_data d WHERE model IN ('payment.method','payment.provider') ORDER BY id"
        )
    ]
    attachments = [
        row[0]
        for row in _rows(
            cr,
            "SELECT (to_jsonb(a)-'db_datas') || jsonb_build_object('has_db_datas',COALESCE(octet_length(db_datas),0)>0) "
            "FROM ir_attachment a WHERE res_model='payment.method' ORDER BY id",
        )
    ]
    modules = {}
    for identifier, state, alias in _rows(
        cr,
        "SELECT m.id,m.state,d.module||'.'||d.name FROM ir_module_module m JOIN ir_model_data d "
        "ON d.model='ir.module.module' AND d.res_id=m.id AND d.module='base' "
        "WHERE m.id=ANY(%s) ORDER BY m.id,d.name",
        ([row["module_id"] for row in providers],),
    ):
        if identifier in modules:
            _fail("ambiguous connector module")
        modules[identifier] = {"xmlid": alias, "state": state}
    company = _rows(cr, "SELECT res_id FROM ir_model_data WHERE module='base' AND name='main_company' AND model='res.company'")
    if len(company) != 1:
        _fail("main company binding")
    columns = _rows(
        cr,
        "SELECT table_name,array_agg(column_name ORDER BY column_name) FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=ANY(%s) GROUP BY table_name",
        ([relation[0] for relation in RELATIONS.values()],),
    )
    return {
        "methods": methods,
        "providers": providers,
        "method_aliases": [row for row in aliases if row["model"] == "payment.method"],
        "provider_aliases": [row for row in aliases if row["model"] == "payment.provider"],
        "attachments": attachments,
        "attachment_aliases": _rows(
            cr, "SELECT id FROM ir_model_data WHERE model='ir.attachment' AND res_id=ANY(%s)", ([row["id"] for row in attachments],)
        ),
        "modules": modules,
        "relation_columns": dict(columns),
        "main_company": company[0][0],
        "company_ids": [row[0] for row in _rows(cr, "SELECT id FROM res_company ORDER BY id")],
        "tokens": bool(_rows(cr, "SELECT 1 FROM payment_token LIMIT 1")),
        "transactions": bool(_rows(cr, "SELECT 1 FROM payment_transaction LIMIT 1")),
        "provider_links": _rows(
            cr, "SELECT payment_provider_id,payment_method_id FROM payment_method_payment_provider_rel ORDER BY 1,2"
        ),
        **{
            key: _logical_links(cr, table, column, model, [row["id"] for row in methods])
            for key, (table, column, model) in RELATIONS.items()
            if key != "providers"
        },
    }


def _delete_exact(cr: Any, table: str, identifiers: list[int]) -> None:
    SQL = import_module("odoo.tools").SQL

    cr.execute(SQL("DELETE FROM %s WHERE id=ANY(%s) RETURNING id", SQL.identifier(table), identifiers))
    if sorted(row[0] for row in cr.fetchall()) != identifiers:
        _fail("retirement identity changed")


def _check_relation_cleanup(cr: Any) -> None:
    SQL = import_module("odoo.tools").SQL

    for table, _column, _model in RELATIONS.values():
        cr.execute(SQL("SELECT 1 FROM %s LIMIT 1", SQL.identifier(table)))
        if cr.fetchone():
            _fail("stock relation rows survived")


def retire(cr: Any, profile: dict[str, Any], version: str, filestore: Path, check_references: Callable[..., None]) -> Retirement:
    witness = source_witness(profile, version)
    with cr.savepoint(flush=False):
        current = capture(cr)
        plan = admit(current, witness, filestore)
        targets = {"payment_method": plan.methods, "ir_attachment": plan.attachments, "ir_model_data": plan.aliases}
        internal_rows = {
            ("payment_method", "primary_payment_method_id", "payment_method"): plan.methods,
            ("ir_attachment", "res_id", "payment_method"): plan.attachments,
            ("ir_model_data", "res_id", "payment_method"): plan.aliases,
        }
        internal_relations = {(table, "payment_method_id", "payment_method") for table, _column, _model in RELATIONS.values()}
        check_references(cr, targets, internal_relations, internal_rows=internal_rows)
        for table in ("ir_attachment", "ir_model_data", "payment_method"):
            _delete_exact(cr, table, targets[table])
        providers = [row[0] for row in _rows(cr, "SELECT to_jsonb(p) FROM payment_provider p ORDER BY id")]
        if providers != current["providers"]:
            _fail("retained provider rows changed")
        _check_relation_cleanup(cr)
        for path, expected in plan.files.items():
            body = path.read_bytes()
            if (
                len(body) != expected["bytes"]
                or hashlib.sha1(body).hexdigest() != expected["sha1"]
                or hashlib.sha256(body).hexdigest() != expected["sha256"]
            ):
                _fail("original file changed")
    return plan
