from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from odoo_release_plan import digest

BUCKETS = (
    "modules",
    "models",
    "fields",
    "xmlids",
    "views",
    "actions",
    "menus",
    "filters",
    "defaults",
    "automation",
    "templates",
    "qweb",
)
CODE_FIELDS = ("code", "compute", "condition", "python", "expression")
EXPRESSION_FIELDS = (
    "context",
    "domain",
    "invisible",
    "readonly",
    "required",
    "column_invisible",
    "filter_domain",
    "filter_pre_domain",
)
REFERENCE = re.compile(r"%\(([^)]+)\)[ds]")


def finding(code: str, reference: str = "", *, state: str = "static-only") -> dict[str, str]:
    return {"code": code, "reference": reference, "state": state}


def resolve_field(target: dict[str, Any], model: str, path: str) -> bool:
    return bool(path) and not field_findings(target, model, path)


def walk_field(target: dict[str, Any], model: str, path: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    if not path:
        return None, []
    if not model:
        return None, [finding("field-model-unresolved", path, state="data-dependent/unreached")]
    original = model + "." + path
    field = None
    for name in path.split("."):
        reference = model + "." + name
        if model in target.get("pending_models", []):
            return None, [finding("model-baseline-pending", model, state="data-dependent/unreached")]
        if reference in target.get("pending_fields", []):
            return None, [finding("field-baseline-pending", reference, state="data-dependent/unreached")]
        if model in target.get("blocked_models", []) or reference in target.get("blocked_fields", []):
            return None, [finding("field-baseline-blocked", reference, state="dependency-blocked")]
        field = target.get("models", {}).get(model, {}).get("fields", {}).get(name)
        if field is None:
            return None, [finding("missing-field", original)]
        model = field.get("relation", "")
    return field, []


def field_findings(target: dict[str, Any], model: str, path: str) -> list[dict[str, str]]:
    return walk_field(target, model, path)[1]


def xmlid_findings(target: dict[str, Any], xmlid: str, *, technical: bool = False) -> list[dict[str, str]]:
    if xmlid in target.get("xmlids", {}):
        return []
    if xmlid.split(".")[0] in target.get("blocked_modules", []):
        return [finding("xmlid-baseline-blocked", xmlid, state="dependency-blocked")]
    if xmlid in target.get("retained_xmlids", []):
        return [finding("retained-xmlid-native-unavailable", xmlid, state="dependency-blocked")]
    if technical and target.get("technical_xmlids_complete"):
        return [finding("missing-xmlid", xmlid)]
    if target.get("nontechnical_xmlids") is None or target["nontechnical_xmlids"] > 0:
        return [finding("xmlid-scope-unresolved", xmlid, state="data-dependent/unreached")]
    return [finding("missing-xmlid", xmlid)]


def domain_findings(node: ast.AST, model: str, target: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    if len(node.elts) == 3:
        name, operator, value = node.elts
        if isinstance(name, ast.Constant) and isinstance(name.value, str) and isinstance(operator, ast.Constant):
            field, results = walk_field(target, model, name.value)
            if operator.value in {"any", "not any", "any!", "not any!"} and field is not None:
                results += domain_findings(value, field.get("relation", ""), target)
            return results
    return [result for child in node.elts for result in domain_findings(child, model, target)]


def context_key_findings(
    node: ast.Dict, model: str, target: dict[str, Any], search_filters: set[str] | None
) -> list[dict[str, str]]:
    results = []
    for key, child in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            results.append(finding("dynamic-context-key", state="data-dependent/unreached"))
            continue
        if not model:
            continue
        if key.value.startswith("search_default_"):
            name = key.value.removeprefix("search_default_")
            if search_filters is not None and name in search_filters:
                continue
            references = field_findings(target, model, name)
            if references and not any(
                item["code"] in {"field-baseline-pending", "model-baseline-pending", "field-baseline-blocked"} for item in references
            ):
                results.append(finding("search-filter-unresolved", name, state="data-dependent/unreached"))
            else:
                results += references
        elif key.value.startswith("default_"):
            results += field_findings(target, model, key.value.removeprefix("default_"))
        elif key.value == "group_by":
            groups = child.elts if isinstance(child, (ast.List, ast.Tuple)) else [child]
            for group in groups:
                if isinstance(group, ast.Constant) and isinstance(group.value, str):
                    results += field_findings(target, model, group.value.split(":")[0])
                else:
                    results.append(finding("expression-shape-unvalidated", "group_by", state="data-dependent/unreached"))
    return results


def expression_findings(
    value: str,
    kind: str,
    model: str,
    target: dict[str, Any],
    *,
    consumer_model: str | None = None,
    search_filters: set[str] | None = None,
    producer_fields: bool = True,
) -> list[dict[str, str]]:
    try:
        tree = ast.parse(value, mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return [finding("unsupported-expression", kind, state="data-dependent/unreached")]
    results = domain_findings(tree.body, model, target) if kind.endswith("domain") else []
    if kind == "context":
        consumer = model if consumer_model is None else consumer_model
        if not consumer:
            results.append(finding("context-consumer-uncertain", kind, state="data-dependent/unreached"))
        if isinstance(tree.body, ast.Dict):
            results += context_key_findings(tree.body, consumer, target, search_filters)
    if (kind.endswith("domain") and not isinstance(tree.body, (ast.List, ast.Tuple))) or (
        kind == "context" and not isinstance(tree.body, ast.Dict)
    ):
        results.append(finding("expression-shape-unvalidated", kind, state="data-dependent/unreached"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and kind == "context" and node is not tree.body:
            results.append(finding("context-consumer-uncertain", "nested-context", state="data-dependent/unreached"))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "ref" and len(node.args) == 1:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    results += xmlid_findings(target, argument.value)
            results.append(finding("unexecuted-expression-call", kind, state="data-dependent/unreached"))
        if isinstance(node, ast.Name) and kind not in {"domain", "filter_domain", "context"}:
            if node.id not in {"parent", "context", "uid", "id", "True", "False", "None"}:
                if resolve_field(target, model, node.id) or node.id in target.get("source_fields", {}).get(model, []):
                    results += field_findings(target, model, node.id)
                else:
                    results.append(finding("expression-name-unresolved", state="data-dependent/unreached"))
        elif isinstance(node, ast.Name):
            if (
                kind == "context"
                and producer_fields
                and (
                    node.id in target.get("models", {}).get(model, {}).get("fields", {})
                    or node.id in target.get("source_fields", {}).get(model, [])
                )
            ):
                results += field_findings(target, model, node.id)
            results.append(finding("data-dependent-expression", kind, state="data-dependent/unreached"))
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.ListComp, ast.DictComp, ast.GeneratorExp, ast.Lambda)):
            results.append(finding("dynamic-expression", kind, state="data-dependent/unreached"))
    return results


def button_context_action(
    element: ElementTree.Element, target: dict[str, Any], action_bindings: dict[str, list[str]]
) -> dict[str, Any] | None:
    if element.get("type") != "action" or element.get("position"):
        return None
    name = element.get("name", "")
    symbolic = REFERENCE.fullmatch(name)
    references = action_bindings.get(name, []) if name.isdigit() else [symbolic[1] if symbolic else name]
    bindings = [target.get("xmlids", {}).get(reference, {}) for reference in references]
    if not bindings or any(binding.get("model") != "ir.actions.act_window" or not binding.get("key") for binding in bindings):
        return None
    if len({binding["key"] for binding in bindings}) != 1:
        return None
    matches = [
        row
        for row in target.get("context_records", {}).get("actions", [])
        if any(target.get("xmlids", {}).get(reference) == bindings[0] for reference in row.get("xmlids", []))
    ]
    if len(matches) != 1:
        return None
    action = matches[0]
    if (
        action.get("type") != "ir.actions.act_window"
        or not action.get("model")
        or action.get("withheld")
        or action.get("minimized")
        or not action.get("noupdate")
        or native_replacement(action, target, action["type"])
    ):
        return None
    aliases = [target.get("xmlids", {}).get(reference, {}) for reference in action.get("xmlids", [])]
    if any(binding != bindings[0] for binding in aliases):
        return None
    return action


def action_search_filters(action: dict[str, Any], target: dict[str, Any]) -> set[str] | None:
    key = "view:" + str(action.get("search_view_id", ""))
    views = target.get("context_records", {}).get("views", [])
    matches = [row for row in views if row["key"] == key]
    if len(matches) != 1:
        return None
    row = matches[0]
    if (
        row.get("type") != "search"
        or row.get("model") != action.get("model")
        or row.get("parent")
        or any(view.get("parent") == key for view in views)
        or row.get("withheld")
        or row.get("minimized")
        or row.get("changed_languages")
        or row.get("website_scope")
        or row.get("active") is False
        or (
            not row.get("noupdate")
            and any(target.get("xmlids", {}).get(xmlid, {}).get("model") == "ir.ui.view" for xmlid in row.get("xmlids", []))
        )
        or native_replacement(row, target, "ir.ui.view")
    ):
        return None
    architectures = row.get("architectures", {})
    if not architectures or not all(architectures.values()):
        return None
    names = []
    for architecture in architectures.values():
        try:
            root = ElementTree.fromstring(architecture)
        except ElementTree.ParseError:
            return None
        if root.tag != "search" or any(element.get("position") for element in root.iter()):
            return None
        names.append({element.get("name", "") for element in root.iter("filter")})
    return set.intersection(*names)


def view_findings(record: dict[str, Any], target: dict[str, Any]) -> list[dict[str, str]]:
    results = []
    variants = record.get("architectures", {})
    if not variants:
        return [finding("architecture-withheld", state="data-dependent/unreached")]
    for language, architecture in variants.items():
        if not architecture or language in record.get("changed_languages", []):
            results.append(finding("architecture-withheld-or-changed", language, state="data-dependent/unreached"))
            continue
        try:
            root = ElementTree.fromstring(architecture)
        except ElementTree.ParseError:
            results.append(finding("invalid-xml", language))
            continue
        results += walk_view(root, record.get("model", ""), target, record.get("action_bindings", {}))
        if record.get("type") == "qweb":
            results.append(finding("qweb-render-unexecuted", state="data-dependent/unreached"))
    if record.get("website_scope"):
        results.append(finding("website-variant-unbound", state="data-dependent/unreached"))
    if record.get("active") is False:
        results.append(finding("inactive-view-native-limits", state="data-dependent/unreached"))
    if record.get("order_unreproducible"):
        results.append(finding("upgrade-order-unreproducible", state="conditional"))
    if record.get("type") and record["type"] not in target.get("view_types", []):
        results.append(finding("unsupported-view-type", record["type"]))
    return results


def walk_view(
    element: ElementTree.Element, model: str, target: dict[str, Any], action_bindings: dict[str, list[str]]
) -> list[dict[str, str]]:
    results = []
    name = element.get("name", "")
    button = element.tag in {"button", "a"} and element.get("type") in {"object", "action"}
    child_model = model
    fragment = bool(element.get("position")) or element.tag == "xpath"
    if fragment:
        child_model = ""
        results.append(finding("inheritance-fragment-native-required", state="data-dependent/unreached"))
    elif element.tag in {"field", "groupby"} and name:
        results += field_findings(target, model, name)
        child_model = target.get("models", {}).get(model, {}).get("fields", {}).get(name, {}).get("relation", "")
    if button and element.get("type") == "object":
        if name not in target.get("models", {}).get(model, {}).get("methods", []):
            results.append(finding("method-unresolved", model + "." + name, state="data-dependent/unreached"))
    if button and element.get("type") == "action" and name.isdigit():
        bindings = action_bindings.get(name, [])
        if not bindings:
            results.append(finding("numeric-action-binding-unavailable", name, state="data-dependent/unreached"))
        elif not any(target.get("xmlids", {}).get(xmlid, {}).get("model", "").startswith("ir.actions.") for xmlid in bindings):
            results.append(finding("numeric-action-target-unavailable", name, state="data-dependent/unreached"))
    for attribute, value in element.attrib.items():
        for xmlid in REFERENCE.findall(value):
            results += xmlid_findings(target, xmlid, technical=button and element.get("type") == "action" and attribute == "name")
        if attribute in EXPRESSION_FIELDS:
            expression_model = child_model if element.tag in {"field", "groupby"} and attribute in {"domain", "context"} else model
            consumer_model = expression_model
            search_filters = None
            if attribute == "context" and button:
                action = button_context_action(element, target, action_bindings) if model and not fragment else None
                consumer_model = action["model"] if action else ""
                search_filters = action_search_filters(action, target) if action else None
            results += expression_findings(
                value,
                attribute,
                model if attribute == "context" else expression_model,
                target,
                consumer_model=consumer_model,
                search_filters=search_filters,
            )
        if attribute in {"attrs", "states"}:
            results.append(finding("obsolete-view-attribute", attribute))
        if attribute == "groups":
            for xmlid in value.split(","):
                results += xmlid_findings(target, xmlid.strip().lstrip("!"), technical=True)
        if attribute.startswith("t-"):
            results.append(finding("template-expression-unexecuted", attribute, state="data-dependent/unreached"))
    if element.tag == "attribute" and name in EXPRESSION_FIELDS:
        results += expression_findings(element.text or "", name, model, target, consumer_model="" if name == "context" else None)
    for child in element:
        results += walk_view(child, child_model, target, action_bindings)
    return results


def reference_findings(record: dict[str, Any], target: dict[str, Any], *, field_declaration: bool = False) -> list[dict[str, str]]:
    results = []
    model = record.get("model", "")
    if model in target.get("pending_models", []):
        results.append(finding("model-baseline-pending", model, state="data-dependent/unreached"))
    elif model and model not in target.get("models", {}):
        state = "dependency-blocked" if model in target.get("blocked_models", []) else "static-only"
        results.append(finding("missing-model", model, state=state))
    for owner in record.get("owners", []):
        if owner not in target.get("modules", {}):
            results.append(finding("missing-owner-module", owner))
    for reference in record.get("references", []):
        kind, name = reference["kind"], reference["name"]
        if kind == "field":
            results += field_findings(target, reference.get("model", model), name)
        elif kind == "xmlid":
            results += xmlid_findings(target, name)
        elif kind in {"model", "module"}:
            key = {"model": "models", "module": "modules"}[kind]
            if kind == "model" and name in target.get("pending_models", []):
                results.append(finding("model-baseline-pending", name, state="data-dependent/unreached"))
            elif name not in target.get(key, {}):
                results.append(finding("missing-" + kind, name))
        else:
            results.append(finding("unsupported-reference", kind, state="data-dependent/unreached"))
    for attribute in EXPRESSION_FIELDS:
        if record.get(attribute) and (attribute not in {"readonly", "required"} or isinstance(record[attribute], str)):
            expression_model = record.get("relation", "") if field_declaration and attribute in {"domain", "context"} else model
            results += expression_findings(
                record[attribute],
                attribute,
                model if attribute == "context" else expression_model,
                target,
                consumer_model=expression_model,
                search_filters=action_search_filters(record, target) if record.get("type") == "ir.actions.act_window" else None,
                producer_fields=field_declaration,
            )
    for attribute in CODE_FIELDS:
        if record.get(attribute):
            results.append(finding("stored-code-unexecuted", attribute, state="data-dependent/unreached"))
            if not isinstance(record[attribute], str):
                continue
            try:
                code = ast.parse(record[attribute])
            except (SyntaxError, ValueError):
                results.append(finding("unsupported-stored-code", attribute, state="data-dependent/unreached"))
                continue
            for node in ast.walk(code):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in {"record", "self"}:
                    if node.attr in target.get("models", {}).get(model, {}).get("fields", {}) or node.attr in target.get(
                        "source_fields", {}
                    ).get(model, []):
                        results += field_findings(target, model, node.attr)
                    elif node.attr not in target.get("models", {}).get(model, {}).get("methods", []):
                        results.append(
                            finding("stored-attribute-unresolved", model + "." + node.attr, state="data-dependent/unreached")
                        )
    for attribute in record.get("minimized", []):
        results.append(finding("minimized-input", attribute, state="data-dependent/unreached"))
    for attribute in record.get("withheld", []):
        results.append(finding("withheld-input", attribute, state="data-dependent/unreached"))
    return results


def action_identity(value: dict[str, Any]) -> tuple[str, str] | None:
    model, key = value.get("model"), value.get("key")
    return (model, key) if isinstance(model, str) and model and isinstance(key, str) and key else None


def action_path_findings(record: dict[str, Any], target: dict[str, Any], paths: dict[str, list[str]]) -> list[dict[str, str]]:
    path = record.get("path") or ""
    if not path:
        return []
    conflict = len(paths.get(path, [])) > 1
    unresolved = False
    owners = target.get("action_paths", {}).get(path, [])
    if owners:
        xmlids = target.get("xmlids", {})
        matches = [xmlids[name] for name in {record["key"], *record.get("xmlids", [])} if name in xmlids]
        identities = {action_identity(value) for value in matches}
        own = next(iter(identities)) if len(identities) == 1 else None
        if own is None or own[0] != record.get("type"):
            unresolved = True
        else:
            for owner in owners:
                identity = action_identity(xmlids.get(owner, {}) if isinstance(owner, str) else owner)
                if identity is None:
                    unresolved = True
                elif identity != own:
                    conflict = True
    results = [finding("action-path-conflict", path)] if conflict else []
    if unresolved:
        results.append(finding("action-path-identity-unresolved", path, state="data-dependent/unreached"))
    return results


def action_findings(record: dict[str, Any], target: dict[str, Any], paths: dict[str, list[str]]) -> list[dict[str, str]]:
    results = []
    for attribute in ("context", "domain"):
        if record.get(attribute):
            results.append(finding("action-expression-unvalidated", attribute, state="data-dependent/unreached"))
    action_type = record.get("type", "")
    if action_type not in target.get("models", {}):
        results.append(finding("missing-action-class", action_type))
    modes = record.get("view_mode", "").split(",") if record.get("view_mode") else []
    additional_modes = record.get("view_modes", [])
    for mode in [*modes, *additional_modes]:
        if mode not in target.get("view_types", []):
            results.append(finding("unsupported-view-mode", mode))
    if len(set(modes)) != len(modes):
        results.append(finding("duplicate-view-mode"))
    results += action_path_findings(record, target, paths)
    if action_type not in {"ir.actions.act_window", "ir.actions.act_window_close"}:
        results.append(finding("action-behavior-unexecuted", action_type, state="data-dependent/unreached"))
    return results


def families(views: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], set[str]]:
    by_key = {row["key"]: row for row in views}
    groups: dict[str, list[dict[str, Any]]] = {}
    blocked: set[str] = set()
    for row in views:
        root, seen = row, set()
        while root.get("parent") in by_key:
            if root["key"] in seen:
                blocked.add(row["key"])
                break
            seen.add(root["key"])
            root = by_key[root["parent"]]
        else:
            groups.setdefault(root["key"], []).append(row)
    return list(groups.values()), blocked


def record_findings(
    bucket: str, record: dict[str, Any], target: dict[str, Any], paths: dict[str, list[str]]
) -> list[dict[str, str]]:
    if (bucket == "views" and native_replacement(record, target, "ir.ui.view")) or (
        bucket == "actions" and native_replacement(record, target, record.get("type", ""))
    ):
        return []
    results = reference_findings(record, target, field_declaration=bucket == "fields")
    if bucket in {"views", "actions"} and not record.get("noupdate"):
        model = "ir.ui.view" if bucket == "views" else record.get("type", "")
        for xmlid in record.get("xmlids", []):
            if target.get("xmlids", {}).get(xmlid, {}).get("model") == model:
                results.append(finding("stock-upgrade-disposition-required", xmlid, state="data-dependent/unreached"))
    if (
        bucket == "modules"
        and record.get("state", "installed") not in {"uninstalled", "uninstallable"}
        and record["key"] not in target.get("modules", {})
    ):
        results.append(finding("missing-module", record["key"]))
    if bucket == "fields":
        results += field_findings(target, record.get("model", ""), record.get("name", ""))
        actual = target.get("models", {}).get(record.get("model", ""), {}).get("fields", {}).get(record.get("name", ""))
        if actual and record.get("ttype") and actual.get("type") and actual["type"] != record["ttype"]:
            results.append(finding("field-type-changed", record["model"] + "." + record["name"]))
        for path in (record.get("related", ""), *record.get("depends", [])):
            results += field_findings(target, record.get("model", ""), path)
    if bucket == "views" or bucket == "qweb" and not record.get("source_view"):
        results += view_findings(record, target)
    if bucket == "xmlids":
        actual = target.get("xmlids", {}).get(record["key"])
        if actual is None:
            results.append(finding("xmlid-upgrade-disposition-unresolved", record["key"], state="data-dependent/unreached"))
        elif record.get("record_model", record.get("model")) and actual["model"] != record.get("record_model", record.get("model")):
            results.append(finding("moved-xmlid-model", record["key"]))
    if bucket == "actions":
        results += action_findings(record, target, paths)
    if bucket not in {"modules", "models", "fields", "xmlids", "views", "actions"}:
        results.append(finding("auxiliary-native-unreached", bucket, state="data-dependent/unreached"))
    unique = {json.dumps(item, sort_keys=True): item for item in results}
    return list(unique.values())


def validate_inventory(inventory: dict[str, Any]) -> None:
    if not inventory.get("binding"):
        raise ValueError("A source-bound technical inventory is required")
    for bucket, rows in inventory["records"].items():
        keys = [row["key"] for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Duplicate technical keys in {bucket}")
        total = inventory["totals"].get(bucket)
        if total is None:
            continue
        if not isinstance(total, int) or total < len(rows):
            raise ValueError(f"Missing or inconsistent source total for {bucket}")


def qualified_coverage(record: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    if not record.get("candidate_coverage"):
        return []
    fingerprint = digest({key: value for key, value in record.items() if key != "candidate_coverage"})
    hashes = {*target.get("migration_sources", {}).values(), *target.get("source_hashes", [])}
    return [
        item
        for item in record.get("candidate_coverage", [])
        if item.get("candidate") == target.get("binding")
        and item.get("record_sha256") == fingerprint
        and item.get("guard_receipt")
        and item.get("source_sha256") in hashes
    ]


def covered_findings(record: dict[str, Any], results: list[dict[str, str]], target: dict[str, Any]) -> None:
    for coverage in qualified_coverage(record, target):
        model = target.get("xmlids", {}).get(coverage.get("reference"), {}).get("model", "")
        if model and coverage.get("code") == "native20-stock-replacement" and native_replacement(record, target, model):
            if not any(
                item["code"] == "native20-stock-replacement" and item["reference"] == coverage["reference"] for item in results
            ):
                results.append(finding("native20-stock-replacement", coverage["reference"], state="candidate-covered"))
        for result in results:
            if result["code"] == coverage.get("code") and result["reference"] == coverage.get("reference"):
                result["state"] = "candidate-covered"
                result["issue"] = coverage.get("issue", "")


def native_replacement(row: dict[str, Any], target: dict[str, Any], model: str) -> bool:
    return any(
        item.get("code") == "native20-stock-replacement"
        and item.get("reference") in row.get("xmlids", [])
        and target.get("xmlids", {}).get(item.get("reference"), {}).get("model") == model
        for item in qualified_coverage(row, target)
    )


def collect_static(inventory: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    validate_inventory(inventory)
    if not target.get("binding"):
        raise ValueError("An exact candidate binding is required")
    source_fields: dict[str, list[str]] = {}
    target = target.copy()
    target["context_records"] = inventory["records"]
    target["retained_xmlids"] = {row["key"] for row in inventory["records"].get("xmlids", [])}
    target["technical_xmlids_complete"] = inventory["totals"].get("xmlids") == len(
        inventory["records"].get("xmlids", [])
    ) and not any(row.get("withheld") or row.get("minimized") for row in inventory["records"].get("xmlids", []))
    target["nontechnical_xmlids"] = inventory.get("out_of_scope", {}).get("nontechnical_xmlids")
    target["source_fields"] = source_fields
    for row in inventory["records"].get("fields", []):
        source_fields.setdefault(row.get("model", ""), []).append(row.get("name", ""))
    paths: dict[str, list[str]] = {}
    for row in inventory["records"].get("actions", []):
        if row.get("path") and not native_replacement(row, target, row.get("type", "")):
            paths.setdefault(row["path"], []).append(row["key"])
    report: dict[str, Any] = {
        "inventory": digest(inventory),
        "source": inventory["binding"],
        "candidate": target["binding"],
        "target_surface": target.get("surface", "static"),
        "components": [],
        "coverage": {},
        "schema_gaps": inventory.get("schema_gaps", {}),
        "out_of_scope": inventory.get("out_of_scope", {}),
        "automation_totals": inventory.get("automation_totals", {}),
        "limitations": ["empty-target-not-real-upgrade", "dynamic-code-not-executed", "no-product-coverage-without-actual-inputs"],
    }
    known_views = {row["key"] for row in inventory["records"].get("views", [])}
    _, cyclic = families(inventory["records"].get("views", []))
    for bucket in sorted(set(BUCKETS) | set(inventory["records"]) | set(inventory["totals"])):
        rows = inventory["records"].get(bucket, [])
        for row in rows:
            results = record_findings(bucket, row, target, paths)
            if bucket == "views" and row.get("parent") and not native_replacement(row, target, "ir.ui.view"):
                if row["parent"] not in known_views and row["parent"] not in target.get("xmlids", {}):
                    results.append(finding("missing-view-parent", row["parent"], state="dependency-blocked"))
            if row["key"] in cyclic and bucket == "views":
                results.append(finding("view-inheritance-cycle", state="dependency-blocked"))
            if (bucket == "modules" and row["key"] in target.get("blocked_modules", [])) or set(row.get("owners", [])) & set(
                target.get("blocked_modules", [])
            ):
                for result in results:
                    if result["code"].startswith("missing-"):
                        result["state"] = "dependency-blocked"
            if target.get("conditional"):
                results.append(finding("reduced-module-set", state="conditional"))
            covered_findings(row, results, target)
            component: dict[str, Any] = {
                "bucket": bucket,
                "key": row["key"],
                "checked": not any(item["state"] in {"data-dependent/unreached", "dependency-blocked"} for item in results),
                "native": "unreached",
                "findings": results,
            }
            if bucket in {"views", "qweb"}:
                component["facts"] = {
                    name: row[name]
                    for name in (
                        "active",
                        "priority",
                        "source_order",
                        "noupdate",
                        "arch_updated",
                        "history_present",
                        "customization_present",
                        "mode",
                    )
                    if name in row
                }
                component["facts"]["languages"] = sorted(row.get("architectures", {}))
            report["components"].append(component)
    recount(report, inventory)
    return report


def recount(report: dict[str, Any], inventory: dict[str, Any]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in report["components"]:
        grouped.setdefault(row["bucket"], []).append(row)
    for bucket in sorted(set(BUCKETS) | set(inventory["records"]) | set(inventory["totals"])):
        rows = grouped.get(bucket, [])
        for row in rows:
            row["checked"] = row.get("checked", False) and not any(
                item["state"] in {"data-dependent/unreached", "dependency-blocked"} for item in row["findings"]
            )
        states = Counter(state for row in rows for state in {item["state"] for item in row["findings"]})
        static_findings = states.pop("static-only", 0)
        total = inventory["totals"].get(bucket)
        report["coverage"][bucket] = {
            "total": total,
            "inventoried": len(rows),
            "withheld": None if total is None else total - len(rows),
            "checked": sum(row["checked"] for row in rows),
            **dict(states),
            "static-findings": static_findings,
            "native-unreached": sum(row["native"] == "unreached" for row in rows),
            "native-checked": sum(row["native"] == "checked" for row in rows),
        }
        if total is None:
            report["coverage"][bucket]["gap"] = "source-total-missing"
    classes = Counter(row.get("type", "unknown") for row in inventory["records"].get("actions", []))
    report["action_classes"] = {
        name: {"inventoried": count, "total": inventory.get("action_totals", {}).get(name)} for name, count in classes.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare screened technical metadata with an exact target surface; never execute stored code."
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = collect_static(json.loads(args.inventory.read_text()), json.loads(args.target.read_text()))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"coverage": report["coverage"]}))


if __name__ == "__main__":
    main()
