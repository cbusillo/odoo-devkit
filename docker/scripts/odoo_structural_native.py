from __future__ import annotations

import hashlib
import inspect
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from odoo_release_inventory import module_graph
from odoo_release_plan import verify_image_declaration
from odoo_structural_census import collect_static, digest, families, finding, recount, validate_inventory

_logger = logging.getLogger(__name__)
NATIVE_ROLES = ("registry", "loading", "convert", "views", "actions")
SAFE_MANUAL_TYPES = {"char", "text", "html", "integer", "float", "boolean", "date", "datetime", "many2one"}


class ViewFailure(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__("Native retained family validation failed")
        self.key = key


class ProbeUnreached(ValueError):
    pass


class NativeLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.failed_module = ""
        self.warnings = 0

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.name == "odoo.modules.loading"
            and record.msg == "Loading module %s (%d/%d)"
            and isinstance(record.args, tuple)
            and record.args
        ):
            self.failed_module = str(record.args[0])
        if record.name == "odoo.modules.loading" and str(record.msg).startswith("Module %s loaded in"):
            self.failed_module = ""
        if record.levelno == logging.WARNING:
            self.warnings += 1


def closure(roots: set[str], graph: dict[str, set[str]]) -> set[str]:
    result, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name not in result:
            result.add(name)
            pending.extend(graph.get(name, set()) - result)
    return result


def reverse_closure(failed: set[str], graph: dict[str, set[str]]) -> set[str]:
    result = set(failed)
    while additions := {name for name, dependencies in graph.items() if dependencies & result} - result:
        result.update(additions)
    return result


def check_native_sources(candidate: dict[str, Any]) -> None:
    import odoo.addons.base.models.ir_actions as actions
    import odoo.addons.base.models.ir_ui_view as views
    import odoo.modules.loading as loading
    import odoo.orm.registry as registry
    import odoo.tools.convert as convert
    from odoo import release

    if release.version_info[0] != 20:
        raise ValueError("Structural native probes require the selected Community20 target")
    actual = dict(zip(NATIVE_ROLES, (registry, loading, convert, views, actions), strict=True))
    for role, module in actual.items():
        expected = candidate["native_sources"][role]
        if hashlib.sha256(Path(inspect.getfile(module)).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Candidate native source differs: {role}")
    for path, expected in candidate["migration_sources"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError("Candidate migration source differs")


def registry_surface(env: Any, candidate: dict[str, Any], graph: dict[str, set[str]]) -> dict[str, Any]:
    models = {
        name: {
            "fields": {
                key: {"relation": getattr(field, "comodel_name", "") or "", "type": field.type}
                for key, field in env[name]._fields.items()
            },
            "methods": [
                key for key in dir(type(env[name])) if not key.startswith("_") and callable(getattr(type(env[name]), key, None))
            ],
        }
        for name in env.registry.models
    }
    xmlids = {row.module + "." + row.name: {"model": row.model, "key": str(row.res_id)} for row in env["ir.model.data"].search([])}
    paths: dict[str, list[str]] = {}
    for action in env["ir.actions.actions"].search([("path", "!=", False)]):
        paths.setdefault(action.path, []).append(action.get_external_id().get(action.id) or "native:" + str(action.id))
    installed = set(env["ir.module.module"].search([("state", "=", "installed")]).mapped("name"))
    return {
        "binding": candidate["binding"],
        "surface": "native-empty20",
        "models": models,
        "xmlids": xmlids,
        "modules": {name: sorted(graph.get(name, set())) for name in installed},
        "action_paths": paths,
        "view_types": [value for value, _label in env["ir.ui.view"]._fields["type"]._description_selection(env)],
        "migration_sources": candidate["migration_sources"],
    }


def manual_supported(row: dict[str, Any]) -> bool:
    return (
        row.get("ttype") in SAFE_MANUAL_TYPES
        and not row.get("compute")
        and not row.get("related")
        and not row.get("depends")
        and not row.get("withheld")
        and not row.get("selection")
        and not row.get("domain")
    )


def prepare_manual(env: Any, inventory: dict[str, Any]) -> set[str]:
    blocked = set()
    for row in inventory["records"].get("models", []):
        if row.get("state") != "manual":
            continue
        model = row["model"]
        if model not in env:
            env["ir.model"].create({"name": model, "model": model, "state": "manual"})
    for row in inventory["records"].get("fields", []):
        if row.get("state") != "manual":
            continue
        if not manual_supported(row) or row["model"] not in env:
            blocked.add(row["key"])
            continue
        if row["name"] in env[row["model"]]._fields:
            continue
        values = {
            key: row[key]
            for key in ("name", "ttype", "relation", "required", "readonly", "store", "index", "size", "on_delete")
            if key in row
        }
        values.update(
            field_description=row["name"],
            state="manual",
            model_id=env["ir.model"]._get_id(row["model"]),
        )
        env["ir.model.fields"].create(values)
    return blocked


class DisposableCensus:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.owned: set[str] = set()
        self.baseline = ""
        self.conditional = False
        self.bootstrap: list[dict[str, Any]] = []
        self.manual_blocked: set[tuple[str, str]] = set()

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Census deadline reached; remaining components are unreached")

    def allocate(self) -> str:
        self.check_deadline()
        return "devkit_census_" + uuid.uuid4().hex

    def drop(self, name: str) -> None:
        if name not in self.owned:
            raise ValueError("Census cleanup only accepts databases it created")
        from odoo.modules import db

        if db.exist(name):
            db.drop(name)
        self.owned.remove(name)

    def close(self) -> None:
        for name in tuple(self.owned):
            self.drop(name)

    def build(self, modules: set[str], inventory: dict[str, Any], graph: dict[str, set[str]]) -> Any:
        from odoo import SUPERUSER_ID, api
        from odoo.modules import db
        from odoo.orm.registry import Registry
        from odoo.tools import config

        if config["db_template"] != "template0":
            raise ValueError("Empty census initialization requires db_template=template0")
        intended = closure(modules | {"base"}, graph)
        missing = intended - graph.keys()
        blocked = reverse_closure(missing, graph) & intended
        current = intended - blocked
        self.bootstrap += [
            {"module": name, "state": "missing" if name in missing else "dependency-blocked"} for name in sorted(blocked)
        ]
        self.conditional = bool(blocked)
        while current:
            self.check_deadline()
            name = self.allocate()
            log = NativeLog()
            loader = logging.getLogger("odoo.modules.loading")
            loader.addHandler(log)
            try:
                if db.exist(name):
                    raise ValueError("Generated census database already exists")
                self.owned.add(name)
                db._create_empty_database(name)
                registry = Registry.new(name, install_modules=sorted(current), new_db_demo=False)
                if not registry.ready:
                    raise ValueError("Native registry did not become ready")
            except Exception as error:
                _logger.exception("Native census registry initialization failed")
                failed = log.failed_module
                removed = reverse_closure({failed}, graph) & current if failed in current else set()
                self.bootstrap.append({"module": failed, "state": "native-reproduced", "exception": type(error).__name__})
                self.bootstrap += [{"module": item, "state": "dependency-blocked"} for item in sorted(removed - {failed})]
                if name in self.owned:
                    self.drop(name)
                if not removed:
                    raise
                current -= removed
                self.conditional = True
                continue
            finally:
                loader.removeHandler(log)
            self.baseline = name
            with registry.cursor() as cursor:
                env = api.Environment(cursor, SUPERUSER_ID, {})
                installed = set(env["ir.module.module"].search([("state", "=", "installed")]).mapped("name"))
                if not current <= installed:
                    raise ValueError("Native loader left intended modules uninstalled")
            for bucket in ("models", "fields"):
                for row in inventory["records"].get(bucket, []):
                    if row.get("state") != "manual":
                        continue
                    try:
                        with registry.cursor() as cursor:
                            env = api.Environment(cursor, SUPERUSER_ID, {})
                            refused = prepare_manual(env, {"records": {bucket: [row]}})
                            if refused:
                                self.manual_blocked.add((bucket, row["key"]))
                            cursor.commit()
                    except Exception:
                        _logger.exception("Manual census declaration could not enter the empty baseline")
                        self.manual_blocked.add((bucket, row["key"]))
                    Registry.delete(name)
                    registry = Registry.new(name)
            self.bootstrap.append(
                {"modules": sorted(installed), "state": "conditional" if self.conditional else "checked", "warnings": log.warnings}
            )
            return registry
        raise ValueError("No healthy independent module closure")

    @contextmanager
    def probe(self) -> Iterator[Any]:
        from odoo import SUPERUSER_ID, api
        from odoo.modules import db
        from odoo.orm.registry import Registry

        if self.baseline not in self.owned:
            raise ValueError("A census-owned committed empty baseline is required")
        name = self.allocate()
        if db.exist(name):
            raise ValueError("Generated census database already exists")
        self.owned.add(name)
        try:
            db.duplicate(self.baseline, name)
            with Registry(name).cursor() as cursor:
                env = api.Environment(cursor, SUPERUSER_ID, {})
                try:
                    yield env
                finally:
                    cursor.rollback()
                    env.invalidate_all(flush=False)
        finally:
            if name in self.owned:
                self.drop(name)


def native_view_values(env: Any, row: dict[str, Any], identities: dict[str, Any], language: str) -> dict[str, Any]:
    values = {
        key: row[key] for key in ("model", "type", "mode", "priority", "active", "key", "arch_fs", "arch_updated") if key in row
    }
    values.pop("key", None)
    values["arch"] = row["architectures"][language]
    values["name"] = "census:" + row["key"]
    values["inherit_id"] = False
    if "view_key" in row:
        values["key"] = row["view_key"]
    if "groups" in row:
        from odoo import Command

        groups = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row["groups"]]
        if any(not group or group._name != "res.groups" for group in groups):
            raise ProbeUnreached("Retained view groups are unavailable")
        values["group_ids"] = [Command.set([group.id for group in groups])]
    if row.get("parent"):
        parent = identities.get(row["parent"]) or env.ref(row["parent"], raise_if_not_found=False)
        if not parent:
            raise ProbeUnreached("Retained view parent is unavailable")
        values["inherit_id"] = parent.id
    return values


def load_view_family(env: Any, rows: list[dict[str, Any]], language: str) -> list[dict[str, str]]:
    identities: dict[str, Any] = {}
    pending = sorted(rows, key=lambda row: (row.get("priority", 16), row.get("source_order", 0)))
    keys = {row["key"] for row in rows}
    while pending:
        eligible = next((row for row in pending if row.get("parent") not in keys or row["parent"] in identities), None)
        if eligible is None:
            raise ProbeUnreached("Cyclic view family")
        if language not in eligible.get("architectures", {}) or language in eligible.get("changed_languages", []):
            raise ProbeUnreached("Incomplete language family")
        values = native_view_values(env, eligible, identities, language)
        matches = [env.ref(xmlid, raise_if_not_found=False) for xmlid in eligible.get("xmlids", [])]
        matches = [record for record in matches if record]
        if matches and (len({record.id for record in matches}) != 1 or any(record._name != "ir.ui.view" for record in matches)):
            raise ProbeUnreached("Ambiguous retained view identity")
        try:
            if matches:
                view = matches[0]
                view.write(values)
            else:
                view = env["ir.ui.view"].create(values)
        except Exception as error:
            raise ViewFailure(eligible["key"]) from error
        identities[eligible["key"]] = view
        pending.remove(eligible)
    for key, view in identities.items():
        try:
            view._check_xml()
        except Exception as error:
            raise ViewFailure(key) from error
    ordered: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        if "source_order" in row:
            ordered.setdefault((row.get("parent", ""), row.get("priority", 16)), []).append(row)
    changed_order = any(
        sorted(siblings, key=lambda row: row["source_order"]) != sorted(siblings, key=lambda row: identities[row["key"]].id)
        for siblings in ordered.values()
    )
    return [finding("relative-view-order-unreproduced", state="conditional")] if changed_order else []


def load_action(env: Any, row: dict[str, Any]) -> None:
    action_type = row["type"]
    if action_type not in {"ir.actions.act_window", "ir.actions.act_window_close"}:
        raise ProbeUnreached("Only non-executable window action structure is probed")
    if row.get("withheld") or row.get("res_id") or row.get("view_references"):
        raise ProbeUnreached("Action requires unavailable retained data or view family")
    values = {key: row[key] for key in ("path", "res_model", "view_mode", "target", "context", "domain") if key in row}
    values.update(name="census:" + row["key"], type=action_type)
    if action_type == "ir.actions.act_window":
        values["res_model"] = row.get("model", row.get("res_model", ""))
    matches = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row.get("xmlids", [])]
    matches = [record for record in matches if record]
    if matches:
        if len({record.id for record in matches}) != 1 or any(record._name != action_type for record in matches):
            raise ProbeUnreached("Moved or ambiguous action identity")
    try:
        if matches:
            matches[0].write(values)
        else:
            env[action_type].create(values)
    except Exception as error:
        raise ViewFailure(row["key"]) from error


def run_probe(session: DisposableCensus, components: list[dict[str, Any]], probe: Any, *, label: str = "structure") -> None:
    log = NativeLog()
    root = logging.getLogger()
    root.addHandler(log)
    outcomes = {component["key"]: "unreached" for component in components}
    try:
        with session.probe() as env:
            additional = probe(env) or []
    except TimeoutError:
        for component in components:
            component["findings"].append(finding("deadline-unreached", state="data-dependent/unreached"))
    except ViewFailure as error:
        _logger.exception("Native census component failed")
        for component in components:
            if component["key"] == error.key:
                outcomes[component["key"]] = "native-reproduced"
                component["native"] = "native-reproduced"
                component["findings"].append(finding("native-validation-error", state="native-reproduced"))
            else:
                component["findings"].append(finding("family-validation-unreached", error.key, state="dependency-blocked"))
    except Exception as error:
        _logger.exception("Native census probe was not reached")
        for component in components:
            component["findings"].append(finding("native-probe-unreached", type(error).__name__, state="data-dependent/unreached"))
    else:
        for component in components:
            outcomes[component["key"]] = "checked"
            component["findings"].extend(additional)
            if component["native"] != "native-reproduced":
                component["native"] = "checked"
            if session.conditional:
                component["findings"].append(finding("reduced-module-set", state="conditional"))
    finally:
        root.removeHandler(log)
        for component in components:
            component.setdefault("native_checks", []).append({"case": label, "state": outcomes[component["key"]]})
            states = {check["state"] for check in component["native_checks"]}
            component["native"] = (
                "native-reproduced" if "native-reproduced" in states else "checked" if states == {"checked"} else "unreached"
            )
        if log.warnings:
            for component in components:
                component["findings"].append(finding("native-warning", str(log.warnings), state="warning"))


def collect_native(
    inventory: dict[str, Any], candidate: dict[str, Any], roots: list[Path], *, declaration: dict[str, Any], deadline: float
) -> dict[str, Any]:
    from odoo import SUPERUSER_ID, api

    validate_inventory(inventory)
    check_native_sources(candidate)
    verify_image_declaration(declaration, candidate["release_compatibility"], platform=candidate["platform"])
    graph = module_graph(roots)
    declared = {row["name"]: set(row["depends"]) for row in candidate["release_compatibility"]["modules"]}
    if candidate["release_compatibility"].get("complete") is not True or declared != graph:
        raise ValueError("Candidate module graph differs from actual addon paths")
    session = DisposableCensus(deadline)
    try:
        try:
            registry = session.build(set(candidate["intended_modules"]), inventory, graph)
        except Exception as error:
            _logger.exception("No healthy empty target is available for this census")
            report = {
                "inventory": digest(inventory),
                "source": inventory["binding"],
                "candidate": candidate["binding"],
                "bootstrap": session.bootstrap,
                "target_surface": "unavailable",
                "coverage": {},
                "components": [
                    {
                        "bucket": bucket,
                        "key": row["key"],
                        "checked": False,
                        "native": "unreached",
                        "findings": [finding("target-registry-unavailable", type(error).__name__, state="dependency-blocked")],
                    }
                    for bucket, rows in inventory["records"].items()
                    for row in rows
                ],
            }
            recount(report, inventory)
            return report
        with registry.cursor() as cursor:
            target = registry_surface(api.Environment(cursor, SUPERUSER_ID, {}), candidate, graph)
        report = collect_static(inventory, target)
        report["bootstrap"] = session.bootstrap
        report["intended_modules"] = sorted(candidate["intended_modules"])
        by_key = {(row["bucket"], row["key"]): row for row in report["components"]}
        for bucket, key in session.manual_blocked:
            by_key[bucket, key]["findings"].append(finding("manual-baseline-unavailable", state="dependency-blocked"))
        view_families, _cyclic = families(inventory["records"].get("views", []))
        for family in view_families:
            components = [by_key["views", row["key"]] for row in family]
            languages = set().union(*(row.get("architectures", {}) for row in family))
            if not languages or any(row.get("withheld") for row in family):
                continue
            for language in sorted(languages):
                run_probe(session, components, partial(load_view_family, rows=family, language=language), label=language)
        for row in inventory["records"].get("actions", []):
            if row.get("type") not in {"ir.actions.act_window", "ir.actions.act_window_close"}:
                continue
            run_probe(session, [by_key["actions", row["key"]]], partial(load_action, row=row))
        recount(report, inventory)
        return report
    finally:
        session.close()
