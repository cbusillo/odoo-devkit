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
EXPRESSION_FIELDS = ("context", "domain", "invisible", "readonly", "required", "column_invisible", "filter_domain")
REFERENCE = re.compile(r"%\(([^)]+)\)[ds]")


def finding(code: str, reference: str = "", *, state: str = "static-only") -> dict[str, str]:
    return {"code": code, "reference": reference, "state": state}


def resolve_field(target: dict[str, Any], model: str, path: str) -> bool:
    for name in path.split("."):
        field = target.get("models", {}).get(model, {}).get("fields", {}).get(name)
        if field is None:
            return False
        model = field.get("relation", "")
    return True


def field_findings(target: dict[str, Any], model: str, path: str) -> list[dict[str, str]]:
    if not path:
        return []
    if not model:
        return [finding("field-model-unresolved", path, state="data-dependent/unreached")]
    return [] if resolve_field(target, model, path) else [finding("missing-field", model + "." + path)]


def domain_findings(node: ast.AST, model: str, target: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    if len(node.elts) == 3:
        name, operator, value = node.elts
        if isinstance(name, ast.Constant) and isinstance(name.value, str) and isinstance(operator, ast.Constant):
            results = field_findings(target, model, name.value)
            if operator.value in {"any", "not any", "any!", "not any!"}:
                related = model
                for part in name.value.split("."):
                    related = target.get("models", {}).get(related, {}).get("fields", {}).get(part, {}).get("relation", "")
                results += domain_findings(value, related, target)
            return results
    return [result for child in node.elts for result in domain_findings(child, model, target)]


def expression_findings(value: str, kind: str, model: str, target: dict[str, Any]) -> list[dict[str, str]]:
    try:
        tree = ast.parse(value, mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return [finding("unsupported-expression", kind, state="data-dependent/unreached")]
    results = domain_findings(tree.body, model, target) if kind.endswith("domain") else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and kind == "context":
            for key, child in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    results.append(finding("dynamic-context-key", state="data-dependent/unreached"))
                    continue
                for prefix in ("default_", "search_default_"):
                    if key.value.startswith(prefix):
                        name = key.value.removeprefix(prefix)
                        if prefix == "search_default_" and not resolve_field(target, model, name):
                            results.append(finding("search-filter-unresolved", name, state="data-dependent/unreached"))
                        else:
                            results += field_findings(target, model, name)
                if key.value == "group_by" and isinstance(child, ast.Constant) and isinstance(child.value, str):
                    results += field_findings(target, model, child.value.split(":")[0])
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "ref" and len(node.args) == 1:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if argument.value not in target.get("xmlids", {}):
                        results.append(finding("missing-xmlid", argument.value))
            results.append(finding("unexecuted-expression-call", kind, state="data-dependent/unreached"))
        if isinstance(node, ast.Name) and kind not in {"domain", "filter_domain", "context"}:
            if node.id not in {"parent", "context", "uid", "id", "True", "False", "None"}:
                results += field_findings(target, model, node.id)
        elif isinstance(node, ast.Name):
            results.append(finding("data-dependent-expression", kind, state="data-dependent/unreached"))
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.ListComp, ast.DictComp, ast.GeneratorExp, ast.Lambda)):
            results.append(finding("dynamic-expression", kind, state="data-dependent/unreached"))
    return results


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
        results += walk_view(root, record.get("model", ""), target, record.get("type") == "qweb")
        if record.get("type") == "qweb":
            results.append(finding("qweb-render-unexecuted", state="data-dependent/unreached"))
    if record.get("active") is False:
        results.append(finding("inactive-view-native-limits", state="data-dependent/unreached"))
    if record.get("order_unreproducible"):
        results.append(finding("upgrade-order-unreproducible", state="conditional"))
    return results


def walk_view(element: ElementTree.Element, model: str, target: dict[str, Any], qweb: bool) -> list[dict[str, str]]:
    results = []
    name = element.get("name", "")
    child_model = model
    if element.tag == "field" and name:
        results += field_findings(target, model, name)
        child_model = target.get("models", {}).get(model, {}).get("fields", {}).get(name, {}).get("relation", model)
    if element.tag == "button" and element.get("type") == "object":
        if name not in target.get("models", {}).get(model, {}).get("methods", []):
            results.append(finding("method-unresolved", model + "." + name, state="data-dependent/unreached"))
    if element.tag == "xpath":
        results.append(finding("xpath-native-required"))
    for attribute, value in element.attrib.items():
        for xmlid in REFERENCE.findall(value):
            if xmlid not in target.get("xmlids", {}):
                results.append(finding("missing-xmlid", xmlid))
        if attribute in EXPRESSION_FIELDS:
            results += expression_findings(value, attribute, model, target)
        if attribute in {"attrs", "states"}:
            results.append(finding("obsolete-view-attribute", attribute))
        if attribute == "groups":
            for xmlid in value.split(","):
                xmlid = xmlid.strip().lstrip("!")
                if xmlid not in target.get("xmlids", {}):
                    results.append(finding("missing-xmlid", xmlid))
        if attribute.startswith("t-") and qweb:
            results.append(finding("qweb-expression-unexecuted", attribute, state="data-dependent/unreached"))
    for child in element:
        results += walk_view(child, child_model, target, qweb)
    return results


def reference_findings(record: dict[str, Any], target: dict[str, Any]) -> list[dict[str, str]]:
    results = []
    model = record.get("model", "")
    if model and model not in target.get("models", {}):
        results.append(finding("missing-model", model))
    for owner in record.get("owners", []):
        if owner not in target.get("modules", {}):
            results.append(finding("missing-owner-module", owner))
    for reference in record.get("references", []):
        kind, name = reference["kind"], reference["name"]
        if kind == "field":
            results += field_findings(target, reference.get("model", model), name)
        elif kind in {"xmlid", "model", "module"}:
            key = {"xmlid": "xmlids", "model": "models", "module": "modules"}[kind]
            if name not in target.get(key, {}):
                results.append(finding("missing-" + kind, name))
        else:
            results.append(finding("unsupported-reference", kind, state="data-dependent/unreached"))
    for attribute in EXPRESSION_FIELDS:
        if record.get(attribute):
            results += expression_findings(record[attribute], attribute, model, target)
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
                    results += field_findings(target, model, node.attr)
    for attribute in record.get("withheld", []):
        results.append(finding("withheld-input", attribute, state="data-dependent/unreached"))
    return results


def action_findings(record: dict[str, Any], target: dict[str, Any], paths: dict[str, list[str]]) -> list[dict[str, str]]:
    results = []
    action_type = record.get("type", "")
    if action_type not in target.get("models", {}):
        results.append(finding("missing-action-class", action_type))
    modes = record.get("view_mode", "").split(",") if record.get("view_mode") else []
    for mode in modes:
        if mode not in target.get("view_types", []):
            results.append(finding("unsupported-view-mode", mode))
    if len(set(modes)) != len(modes):
        results.append(finding("duplicate-view-mode"))
    path: str = record.get("path") or ""
    if path:
        own = {record["key"], *record.get("xmlids", [])}
        if len(paths.get(path, [])) > 1 or set(target.get("action_paths", {}).get(path, [])) - own:
            results.append(finding("action-path-conflict", path))
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
    results = reference_findings(record, target)
    if bucket == "modules" and record["key"] not in target.get("modules", {}):
        results.append(finding("missing-module", record["key"]))
    if bucket == "fields":
        results += field_findings(target, record.get("model", ""), record.get("name", ""))
        for path in (record.get("related", ""), *record.get("depends", [])):
            results += field_findings(target, record.get("model", ""), path)
    if bucket in {"views", "qweb"}:
        results += view_findings(record, target)
    if bucket == "xmlids":
        actual = target.get("xmlids", {}).get(record["key"])
        if actual is None:
            results.append(finding("missing-xmlid", record["key"]))
        elif record.get("record_model") and actual["model"] != record["record_model"]:
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
        if not isinstance(total, int) or total < len(rows):
            raise ValueError(f"Missing or inconsistent source total for {bucket}")


def covered_findings(record: dict[str, Any], results: list[dict[str, str]], target: dict[str, Any]) -> None:
    for coverage in record.get("candidate_coverage", []):
        if coverage.get("candidate") != target.get("binding") or coverage.get("record_sha256") != digest(
            {key: value for key, value in record.items() if key != "candidate_coverage"}
        ):
            continue
        if not coverage.get("guard_receipt") or coverage.get("source_sha256") not in target.get("migration_sources", {}).values():
            continue
        for result in results:
            if result["code"] == coverage.get("code") and result["reference"] == coverage.get("reference"):
                result["state"] = "candidate-covered"
                result["issue"] = coverage.get("issue", "")


def collect_static(inventory: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    validate_inventory(inventory)
    if not target.get("binding"):
        raise ValueError("An exact candidate binding is required")
    paths: dict[str, list[str]] = {}
    for row in inventory["records"].get("actions", []):
        if row.get("path"):
            paths.setdefault(row["path"], []).append(row["key"])
    report: dict[str, Any] = {
        "inventory": digest(inventory),
        "source": inventory["binding"],
        "candidate": target["binding"],
        "target_surface": target.get("surface", "static"),
        "components": [],
        "coverage": {},
        "limitations": ["empty-target-not-real-upgrade", "dynamic-code-not-executed", "no-product-coverage-without-actual-inputs"],
    }
    known_views = {row["key"] for row in inventory["records"].get("views", [])}
    _, cyclic = families(inventory["records"].get("views", []))
    for bucket in sorted(set(BUCKETS) | set(inventory["records"]) | set(inventory["totals"])):
        rows = inventory["records"].get(bucket, [])
        for row in rows:
            results = record_findings(bucket, row, target, paths)
            if bucket == "views" and row.get("parent"):
                if row["parent"] not in known_views and row["parent"] not in target.get("xmlids", {}):
                    results.append(finding("missing-view-parent", row["parent"], state="dependency-blocked"))
            if row["key"] in cyclic and bucket == "views":
                results.append(finding("view-inheritance-cycle", state="dependency-blocked"))
            covered_findings(row, results, target)
            component: dict[str, Any] = {
                "bucket": bucket,
                "key": row["key"],
                "checked": True,
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
    for bucket in sorted(set(BUCKETS) | set(inventory["records"]) | set(inventory["totals"])):
        rows = [row for row in report["components"] if row["bucket"] == bucket]
        states = Counter(state for row in rows for state in {item["state"] for item in row["findings"]})
        total = inventory["totals"].get(bucket)
        report["coverage"][bucket] = {
            "total": total,
            "inventoried": len(rows),
            "withheld": None if total is None else total - len(rows),
            "checked": sum(row["checked"] for row in rows),
            **dict(states),
            "static-only": sum(row["native"] == "unreached" for row in rows),
            "native-checked": sum(row["native"] == "checked" for row in rows),
        }
        if total is None:
            report["coverage"][bucket]["gap"] = "source-total-missing"
    report["clear"] = False
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
    print(json.dumps({"coverage": report["coverage"], "clear": report["clear"]}))


if __name__ == "__main__":
    main()
