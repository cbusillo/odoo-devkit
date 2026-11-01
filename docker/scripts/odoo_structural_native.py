from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from odoo_release_inventory import build_inventory, module_graph
from odoo_release_plan import digest, verify_image_declaration
from odoo_structural_census import collect_static, families, finding, native_replacement, recount, validate_inventory

_logger = logging.getLogger(__name__)
SAFE_MANUAL_TYPES = {"char", "text", "html", "integer", "float", "boolean", "date", "datetime", "many2one", "selection"}
CHECKPOINT_INTERVAL = 30.0


class ViewFailure(ValueError):
    def __init__(self, key: str, *, attribution: str = "operation") -> None:
        super().__init__("Native retained family validation failed")
        self.key = key
        self.attribution = attribution


class ProbeUnreached(ValueError):
    def __init__(self, message: str, *, code: str = "native-probe-unreached") -> None:
        super().__init__(message)
        self.code = code


class NativeLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.failed_module = ""
        self.events: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.name == "odoo.modules.loading"
            and record.msg == "Loading module %s (%d/%d)"
            and isinstance(record.args, tuple)
            and record.args
            and isinstance(record.args[0], str)
        ):
            self.failed_module = record.args[0]
        if record.name == "odoo.modules.loading" and str(record.msg).startswith("Module %s loaded in"):
            self.failed_module = ""
        if record.levelno >= logging.WARNING:
            self.events.append({"logger": record.name, "severity": record.levelname, "message": record.getMessage()})


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


def declared_files(
    declaration: dict[str, Any], platform: str, roots: list[Path], *, evidence: Path = Path("/opt/launchplane/evidence")
) -> dict[tuple[str, str], str]:
    prefix = "platforms/" + platform.replace("/", "_") + "/"
    result = {}
    for source in declaration.get("sources", []):
        files = source["files"]
        if source["input_name"] in {"base:runtime", "base:devtools"}:
            role = source["input_name"].removeprefix("base:")
            files = json.loads((evidence / f"base-{role}-inventory.json").read_text())
            rebound = build_inventory(
                {"addon_paths": list(map(str, roots)), "sources": [{**source, "roots": list(map(str, roots)), "files": files}]}
            )["sources"][0]
            if rebound != source:
                raise ValueError("Baked base inventory differs from its artifact declaration")
        for file in files:
            if file["path"].startswith("platforms/") and not file["path"].startswith(prefix):
                continue
            path = file["path"].removeprefix(prefix)
            key = source["input_name"], path
            if key in result and result[key] != file["sha256"]:
                raise ValueError("Conflicting artifact source hashes")
            result[key] = file["sha256"]
    return result


def verify_file(path: Path, files: dict[tuple[str, str], str]) -> str:
    actual = path.resolve().as_posix()
    expected = {value for (_source, name), value in files.items() if actual.lstrip("/") == name or actual.endswith("/" + name)}
    if len(expected) != 1 or hashlib.sha256(path.read_bytes()).hexdigest() not in expected:
        raise ValueError("Running source file is not bound to the candidate artifact: " + path.name)
    return next(iter(expected))


def check_native_sources(files: dict[tuple[str, str], str], migration_paths: list[str]) -> dict[str, str]:
    import odoo.addons.base.models.ir_actions as actions
    import odoo.addons.base.models.ir_ui_view as views
    import odoo.modules.loading as loading
    import odoo.orm.environments as environments
    import odoo.orm.registry as registry
    import odoo.sql_db as sql_db
    import odoo.tools.convert as convert
    from odoo import release

    if release.version_info[0] != 20:
        raise ValueError("Structural native probes require the selected Community20 target")
    paths = [Path(inspect.getfile(module)) for module in (registry, loading, convert, views, actions, environments, sql_db)]
    return {str(path): verify_file(path, files) for path in [*paths, *map(Path, migration_paths)]}


def registry_surface(
    env: Any, candidate: dict[str, Any], graph: dict[str, set[str]], migration_sources: dict[str, str]
) -> dict[str, Any]:
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
        "binding": digest(candidate["artifact"]),
        "surface": "native-empty20",
        "models": models,
        "xmlids": xmlids,
        "modules": {name: sorted(graph.get(name, set())) for name in installed},
        "action_paths": paths,
        "view_types": [value for value, _label in env["ir.ui.view"]._fields["type"]._description_selection(env)],
        "migration_sources": migration_sources,
    }


def literal_expression(value: Any, *, allowed_types: tuple[type, ...] = (object,)) -> bool:
    if not value:
        return True
    try:
        return isinstance(ast.literal_eval(value), allowed_types)
    except (ValueError, TypeError, SyntaxError):
        return False


def manual_supported(row: dict[str, Any]) -> bool:
    if row.get("ttype") not in SAFE_MANUAL_TYPES or row.get("compute") or row.get("withheld"):
        return False
    if row.get("ttype") == "selection" and not row.get("related"):
        selection = row.get("selection")
        if not isinstance(selection, list) or any(
            not isinstance(item, (list, tuple)) or len(item) != 2 or not all(isinstance(value, str) for value in item)
            for item in selection
        ):
            return False
    return literal_expression(row.get("domain"), allowed_types=(list, tuple))


def manual_dependencies_available(env: Any, row: dict[str, Any], pending: set[tuple[str, str]]) -> bool:
    model = row["model"]
    if model not in env or (row.get("relation") and row["relation"] not in env):
        return False
    related = row.get("related")
    if not related:
        return True
    if not isinstance(related, str):
        return False
    for name in related.split("."):
        if model not in env:
            return False
        field = env[model]._fields.get(name)
        if field is None:
            return (model, name) in pending
        model = getattr(field, "comodel_name", "") or ""
    return True


def prepare_manual(env: Any, inventory: dict[str, Any]) -> set[tuple[str, str]]:
    blocked: set[tuple[str, str]] = {
        ("models", row["key"])
        for row in inventory["records"].get("models", [])
        if row.get("state") == "manual" and row.get("withheld")
    }
    models = [
        {"name": row["model"], "model": row["model"], "state": "manual"}
        for row in inventory["records"].get("models", [])
        if row.get("state") == "manual" and row["model"] not in env and not row.get("withheld")
    ]
    if models:
        env["ir.model"].create(models)
    pending = {
        (row["model"], row["name"])
        for row in inventory["records"].get("fields", [])
        if row.get("state") == "manual"
        and manual_supported(row)
        and row["model"] in env
        and (not row.get("relation") or row["relation"] in env)
    }
    values_list = []
    for row in inventory["records"].get("fields", []):
        if row.get("state") != "manual":
            continue
        if not manual_supported(row) or not manual_dependencies_available(env, row, pending):
            blocked.add(("fields", row["key"]))
            continue
        if row["name"] in env[row["model"]]._fields:
            if env[row["model"]]._fields[row["name"]].type != row["ttype"]:
                blocked.add(("fields", row["key"]))
            continue
        values = {
            key: row[key]
            for key in (
                "name",
                "ttype",
                "relation",
                "related",
                "domain",
                "required",
                "readonly",
                "store",
                "index",
                "size",
                "on_delete",
            )
            if key in row
        }
        if row.get("selection"):
            values["selection"] = repr(row["selection"])
        values.update(field_description=row["name"], state="manual", model_id=env["ir.model"]._get_id(row["model"]))
        values_list.append(values)
    if values_list:
        env["ir.model.fields"].create(values_list)
    return blocked


class DisposableCensus:
    def __init__(self, deadline: float, *, output: Path | None = None) -> None:
        self.deadline = deadline
        self.output = output
        self.owned: set[str] = set()
        self.baseline = ""
        self.conditional = False
        self.bootstrap: list[dict[str, Any]] = []
        self.manual_blocked: set[tuple[str, str]] = set()
        self.report: dict[str, Any] = {}
        self.inventory: dict[str, Any] | None = None
        self.last_checkpoint: float | None = None
        self.thread: Any = threading.current_thread()
        self.previous_dbname = getattr(self.thread, "dbname", None)

    def checkpoint(self, *, force: bool = False) -> None:
        if self.output is None:
            return
        now = time.monotonic()
        if not force and self.last_checkpoint is not None and now - self.last_checkpoint < CHECKPOINT_INTERVAL:
            return
        if self.inventory is not None:
            recount(self.report, self.inventory)
        self.report["bootstrap"] = self.bootstrap
        self.report["owned_databases"] = sorted(self.owned)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(self.output)
        self.last_checkpoint = now

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Census work deadline reached; remaining components are unreached")

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
        try:
            for name in tuple(self.owned):
                self.drop(name)
        finally:
            if self.previous_dbname is not None:
                self.thread.dbname = self.previous_dbname
            elif hasattr(self.thread, "dbname"):
                del self.thread.dbname

    def manual_baseline(self, registry: Any, inventory: dict[str, Any]) -> Any:
        from odoo import SUPERUSER_ID, api
        from odoo.orm.registry import Registry

        if not any(row.get("state") == "manual" for bucket in ("models", "fields") for row in inventory["records"].get(bucket, [])):
            return registry

        def reload_committed() -> Any:
            self.check_deadline()
            Registry.delete(self.baseline)
            return Registry.new(self.baseline)

        def apply(selected: dict[str, Any]) -> None:
            self.check_deadline()
            with registry.cursor() as cursor:
                self.manual_blocked.update(prepare_manual(api.Environment(cursor, SUPERUSER_ID, {}), selected))
                cursor.commit()

        try:
            apply(inventory)
        except TimeoutError:
            raise
        except Exception:
            _logger.exception("Batched manual declarations failed; rebuilding the committed empty baseline")
            registry = reload_committed()
            for bucket in ("models", "fields"):
                for row in inventory["records"].get(bucket, []):
                    if row.get("state") != "manual":
                        continue
                    self.check_deadline()
                    try:
                        apply({"records": {bucket: [row]}})
                    except TimeoutError:
                        raise
                    except Exception:
                        _logger.exception("Manual declaration unavailable in the empty baseline")
                        self.manual_blocked.add((bucket, row["key"]))
                        registry = reload_committed()
                    self.checkpoint()
        return reload_committed()

    def build(self, modules: set[str], graph: dict[str, set[str]]) -> Any:
        from odoo import SUPERUSER_ID, api
        from odoo.modules import db
        from odoo.modules.module import Manifest
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
        excluded: set[str] = set()
        while current:
            self.check_deadline()
            name = self.allocate()
            log = NativeLog()
            loader = logging.getLogger("odoo.modules.loading")
            previous_level = loader.level
            loader.setLevel(logging.INFO)
            logging.getLogger().addHandler(log)
            self.checkpoint(force=True)
            try:
                if db.exist(name):
                    raise ValueError("Generated census database already exists")
                self.owned.add(name)
                self.checkpoint(force=True)
                db._create_empty_database(name)
                registry = Registry.new(name, install_modules=sorted(current), new_db_demo=False)
                if not registry.ready:
                    raise ValueError("Native registry did not become ready")
            except Exception as error:
                _logger.exception("Native census registry initialization failed")
                failed = log.failed_module
                removal = reverse_closure({failed}, graph)
                if failed in graph and failed not in current and failed not in excluded:
                    automatic = (Manifest.for_addon(failed) or {}).get("auto_install", False)
                    requirements = graph[failed] if automatic is True else set(automatic or [])
                    trigger = sorted((current & requirements) - {"base"})
                    if trigger:
                        removal |= reverse_closure({trigger[0]}, graph)
                removed = removal & current if failed in graph and failed not in excluded else set()
                self.bootstrap.append(
                    {"module": failed, "state": "native-reproduced", "exception": type(error).__name__, "logs": log.events}
                )
                self.bootstrap += [{"module": item, "state": "dependency-blocked"} for item in sorted(removed - {failed})]
                if name in self.owned:
                    self.drop(name)
                self.checkpoint(force=True)
                if not removed:
                    raise
                excluded.add(failed)
                current -= removed
                self.conditional = True
                continue
            finally:
                logging.getLogger().removeHandler(log)
                loader.setLevel(previous_level)
            self.baseline = name
            with registry.cursor() as cursor:
                env = api.Environment(cursor, SUPERUSER_ID, {})
                installed = set(env["ir.module.module"].search([("state", "=", "installed")]).mapped("name"))
                if not current <= installed:
                    raise ValueError("Native loader left intended modules uninstalled")
            self.bootstrap.append(
                {"modules": sorted(installed), "state": "conditional" if self.conditional else "checked", "logs": log.events}
            )
            self.checkpoint(force=True)
            return registry
        raise ValueError("No healthy independent module closure")

    @contextmanager
    def probe(self) -> Iterator[Any]:
        from odoo import SUPERUSER_ID, api
        from odoo.orm.registry import Registry

        self.check_deadline()
        if self.baseline not in self.owned:
            raise ValueError("A census-owned committed empty baseline is required")
        with Registry(self.baseline).cursor() as cursor:
            env = api.Environment(cursor, SUPERUSER_ID, {})
            try:
                yield env
            finally:
                cursor.rollback()


def bind_view_actions(env: Any, row: dict[str, Any], architecture: str) -> str:
    try:
        root = ElementTree.fromstring(architecture)
    except ElementTree.ParseError:
        return architecture
    changed = False
    for button in root.iter("button"):
        name = button.get("name", "")
        if button.get("type") != "action" or not name.isdigit():
            continue
        matches = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row.get("action_bindings", {}).get(name, [])]
        matches = [record for record in matches if record]
        if (
            not matches
            or len({(record._name, record.id) for record in matches}) != 1
            or not matches[0]._name.startswith("ir.actions.")
        ):
            raise ProbeUnreached("Numeric action identity is unavailable", code="numeric-action-target-unavailable")
        button.set("name", str(matches[0].id))
        changed = True
    return ElementTree.tostring(root, encoding="unicode") if changed else architecture


def view_architecture(row: dict[str, Any], language: str) -> str:
    variants = row.get("architectures", {})
    changed = row.get("changed_languages", [])
    if row.get("withheld") or language in changed:
        raise ProbeUnreached("Architecture input was withheld or changed", code="view-architecture-unavailable")
    if language in variants:
        if variants[language]:
            return variants[language]
    elif variants.get("en_US") and "en_US" not in changed:
        return variants["en_US"]
    raise ProbeUnreached("Architecture input is unavailable", code="view-architecture-unavailable")


def load_view_family(
    env: Any, rows: list[dict[str, Any]], language: str, *, replacements: set[str] | None = None
) -> list[dict[str, str]]:
    if not env["res.lang"].search_count([("code", "=", language), ("active", "=", True)]):
        raise ProbeUnreached("Target language is not active", code="target-language-unavailable")
    env = env(context={**env.context, "lang": language})
    identities: dict[str, Any] = {}
    pending = sorted(rows, key=lambda row: (row.get("priority", 16), row.get("source_order", 0)))
    keys = {row["key"] for row in rows}
    while pending:
        row = next((item for item in pending if item.get("parent") not in keys or item["parent"] in identities), None)
        if row is None:
            raise ProbeUnreached("Cyclic view family", code="view-inheritance-cycle")
        matches = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row.get("xmlids", [])]
        matches = [record for record in matches if record]
        if matches and (len({record.id for record in matches}) != 1 or any(record._name != "ir.ui.view" for record in matches)):
            raise ProbeUnreached("Moved or ambiguous retained view identity", code="moved-view-class")
        if matches and row["key"] in (replacements or set()):
            identities[row["key"]] = matches[0]
            pending.remove(row)
            continue
        if row.get("website_scope"):
            raise ProbeUnreached("Website variant is not bound to this empty target", code="website-variant-unbound")
        architecture = view_architecture(row, language)
        if matches and not row.get("noupdate"):
            raise ProbeUnreached(
                "Matched stock view requires a source-qualified upgrade disposition", code="stock-upgrade-disposition-required"
            )
        values = {key: row[key] for key in ("model", "type", "mode", "priority", "active") if key in row}
        values.update(name="census:" + row["key"], arch=bind_view_actions(env, row, architecture), inherit_id=False)
        if row.get("view_key"):
            values["key"] = row["view_key"]
        if row.get("parent"):
            parent = identities.get(row["parent"]) or env.ref(row["parent"], raise_if_not_found=False)
            if not parent:
                raise ProbeUnreached("Retained parent is unavailable", code="retained-parent-unavailable")
            values["inherit_id"] = parent.id
        if "groups" in row:
            from odoo import Command

            groups = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row["groups"]]
            if any(not group or group._name != "res.groups" for group in groups):
                raise ProbeUnreached("Retained groups are unavailable", code="retained-groups-unavailable")
            values["group_ids"] = [Command.set([group.id for group in groups])]
        try:
            if matches:
                view = matches[0]
                view.write(values)
            else:
                view = env["ir.ui.view"].create(values)
        except Exception as error:
            raise ViewFailure(row["key"]) from error
        identities[row["key"]] = view
        pending.remove(row)
    for key, view in identities.items():
        try:
            view._check_xml()
        except Exception as error:
            raise ViewFailure(key, attribution="family") from error
    ordered: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        view = identities[row["key"]]
        if "source_order" in row and view.mode == "extension" and view.active and view.inherit_id:
            ordered.setdefault(view.inherit_id.id, []).append(row)
    changed = any(
        sorted(siblings, key=lambda row: (identities[row["key"]].priority, row["source_order"]))
        != sorted(siblings, key=lambda row: (identities[row["key"]].priority, identities[row["key"]].id))
        for siblings in ordered.values()
    )
    return [finding("relative-view-order-unreproduced", state="conditional")] if changed else []


def load_action(env: Any, row: dict[str, Any], *, replace_native: bool = False) -> None:
    kind = row["type"]
    matches = [env.ref(xmlid, raise_if_not_found=False) for xmlid in row.get("xmlids", [])]
    matches = [record for record in matches if record]
    if matches and (len({record.id for record in matches}) != 1 or any(record._name != kind for record in matches)):
        raise ProbeUnreached("Moved or ambiguous action class", code="moved-action-class")
    if matches and replace_native:
        return
    if matches and not row.get("noupdate"):
        raise ProbeUnreached(
            "Matched stock action requires a source-qualified upgrade disposition", code="stock-upgrade-disposition-required"
        )
    if not all(literal_expression(row.get(name)) for name in ("context", "domain")):
        raise ProbeUnreached("Action expression needs data or execution", code="action-expression-unexecuted")
    if kind not in {"ir.actions.act_window", "ir.actions.act_window_close"}:
        raise ProbeUnreached("Action behavior is not executed", code="action-behavior-unexecuted")
    if row.get("withheld"):
        raise ProbeUnreached("Action input is withheld", code="action-input-withheld")
    if row.get("res_id"):
        raise ProbeUnreached("Action requires retained record data", code="action-record-data-unavailable")
    if row.get("view_references"):
        raise ProbeUnreached("Action view identity is unavailable", code="action-view-identity-unavailable")
    values = {key: row[key] for key in ("path", "view_mode", "target", "context", "domain") if key in row}
    values.update(name="census:" + row["key"], type=kind)
    if kind == "ir.actions.act_window":
        values["res_model"] = row.get("model", row.get("res_model", ""))
    try:
        if matches:
            matches[0].write(values)
        else:
            env[kind].create(values)
    except Exception as error:
        raise ViewFailure(row["key"]) from error
    if row.get("context") or row.get("domain"):
        raise ProbeUnreached("Action creation does not validate context/domain semantics", code="action-expression-unvalidated")


def run_probe(session: DisposableCensus, components: list[dict[str, Any]], probe: Any, *, label: str = "structure") -> None:
    log = NativeLog()
    outcomes = {component["key"]: "unreached" for component in components}
    for component in components:
        if session.conditional:
            component["findings"].append(finding("reduced-module-set", state="conditional"))
    logging.getLogger().addHandler(log)
    try:
        with session.probe() as env:
            additional = probe(env) or []
    except TimeoutError:
        for component in components:
            component["findings"].append(finding("deadline-unreached", state="data-dependent/unreached"))
    except ViewFailure as error:
        _logger.exception("Native family failed during %s", error.attribution)
        if error.attribution == "family":
            session.report.setdefault("family_failures", []).append(
                {
                    "members": [component["key"] for component in components],
                    "validation_record": error.key,
                    "case": label,
                    "state": "native-reproduced",
                }
            )
        for component in components:
            if component["key"] == error.key and error.attribution == "operation":
                outcomes[component["key"]] = "native-reproduced"
                component["findings"].append(finding("native-validation-error", error.attribution, state="native-reproduced"))
            else:
                component["findings"].append(finding("family-validation-unreached", state="dependency-blocked"))
    except ProbeUnreached as error:
        for component in components:
            component["findings"].append(finding(error.code, state="data-dependent/unreached"))
    except Exception as error:
        _logger.exception("Native probe was not reached")
        for component in components:
            component["findings"].append(finding("native-probe-unreached", type(error).__name__, state="data-dependent/unreached"))
    else:
        for component in components:
            if component.get("facts", {}).get("active") is not False:
                outcomes[component["key"]] = "checked"
            component["findings"].extend(additional)
    finally:
        logging.getLogger().removeHandler(log)
        for component in components:
            component.setdefault("native_checks", []).append({"case": label, "state": outcomes[component["key"]]})
            states = {check["state"] for check in component["native_checks"]}
            component["native"] = (
                "native-reproduced" if "native-reproduced" in states else "checked" if states == {"checked"} else "unreached"
            )
            if log.events:
                component.setdefault("native_logs", []).extend(log.events)
            if any(item["severity"] == "WARNING" for item in log.events):
                component["findings"].append(finding("native-warning", state="warning"))
        session.checkpoint()


def unreached_report(inventory: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    report = {
        "inventory": digest(inventory),
        "source": inventory["binding"],
        "candidate": digest(candidate["artifact"]),
        "coverage": {},
        "target_surface": "unavailable",
        "automation_totals": inventory.get("automation_totals", {}),
        "schema_gaps": inventory.get("schema_gaps", {}),
        "out_of_scope": inventory.get("out_of_scope", {}),
        "components": [
            {"bucket": bucket, "key": row["key"], "checked": False, "native": "unreached", "findings": []}
            for bucket, rows in inventory["records"].items()
            for row in rows
        ],
    }
    recount(report, inventory)
    return report


def collect_native(
    inventory: dict[str, Any],
    candidate: dict[str, Any],
    roots: list[Path],
    *,
    declaration: dict[str, Any],
    deadline: float,
    output: Path | None = None,
) -> dict[str, Any]:
    from odoo import SUPERUSER_ID, api

    validate_inventory(inventory)
    artifact = candidate["artifact"]
    margin = min(30.0, max(0.0, deadline - time.monotonic()) / 10)
    session = DisposableCensus(deadline - margin, output=output)
    session.inventory = inventory
    session.report = unreached_report(inventory, candidate)
    session.checkpoint(force=True)
    try:
        try:
            verify_image_declaration(declaration, artifact["release_compatibility"], platform=candidate["platform"])
            migration_sources = check_native_sources(
                declared_files(declaration, candidate["platform"], roots), candidate["migration_sources"]
            )
            graph = module_graph(roots)
            declared = {row["name"]: set(row["depends"]) for row in artifact["release_compatibility"]["modules"]}
            if artifact["release_compatibility"].get("complete") is not True or declared != graph:
                raise ValueError("Candidate module graph differs from actual addon paths")
        except (ValueError, OSError, KeyError, TypeError):
            for row in session.report["components"]:
                row["findings"].append(finding("candidate-source-binding-unavailable", state="dependency-blocked"))
            raise
        try:
            registry = session.build(set(candidate["intended_modules"]), graph)
        except Exception as error:
            _logger.exception("No healthy target is available")
            for row in session.report["components"]:
                row["findings"].append(finding("target-registry-unavailable", type(error).__name__, state="dependency-blocked"))
            return session.report
        with registry.cursor() as cursor:
            target = registry_surface(api.Environment(cursor, SUPERUSER_ID, {}), candidate, graph, migration_sources)
        blocked_modules = {row["module"] for row in session.bootstrap if row.get("module") and row.get("state") != "checked"}
        evidence = dict(
            native_sources=migration_sources,
            bootstrap=session.bootstrap,
            intended_modules=sorted(candidate["intended_modules"]),
            source_install_roots=artifact.get("odoo_install_modules"),
            actual_installed_modules=sorted(target["modules"]),
        )

        def target_with_baseline(surface: dict[str, Any], manual_unavailable: set[tuple[str, str]]) -> dict[str, Any]:
            return {
                **surface,
                "blocked_modules": blocked_modules,
                "conditional": session.conditional,
                "blocked_models": [
                    row["model"]
                    for row in inventory["records"].get("models", [])
                    if set(row.get("owners", [])) & blocked_modules or ("models", row["key"]) in manual_unavailable
                ],
                "blocked_fields": [
                    row["model"] + "." + row["name"]
                    for row in inventory["records"].get("fields", [])
                    if ("fields", row["key"]) in manual_unavailable or set(row.get("owners", [])) & blocked_modules
                ],
            }

        pending = {
            (bucket, row["key"])
            for bucket in ("models", "fields")
            for row in inventory["records"].get(bucket, [])
            if row.get("state") == "manual"
        }
        if pending:
            session.report = collect_static(inventory, target_with_baseline(target, pending))
            session.report.update(evidence)
            for component in session.report["components"]:
                if (component["bucket"], component["key"]) in pending:
                    component["findings"].append(finding("manual-baseline-pending", state="data-dependent/unreached"))
            session.checkpoint(force=True)
            try:
                registry = session.manual_baseline(registry, inventory)
            except TimeoutError:
                _logger.exception("Manual baseline deadline reached; the healthy pre-manual static census is retained")
                for component in session.report["components"]:
                    for item in component["findings"]:
                        if item["code"] == "manual-baseline-pending":
                            item["code"] = "manual-baseline-deadline-unreached"
                    if component["bucket"] in {"views", "actions"}:
                        component["findings"].append(finding("deadline-unreached", state="data-dependent/unreached"))
                return session.report
            with registry.cursor() as cursor:
                target = registry_surface(api.Environment(cursor, SUPERUSER_ID, {}), candidate, graph, migration_sources)
        target = target_with_baseline(target, session.manual_blocked)
        session.report = report = collect_static(inventory, target)
        report.update(evidence)
        by_key = {(row["bucket"], row["key"]): row for row in report["components"]}
        for bucket, key in session.manual_blocked:
            by_key[bucket, key]["findings"].append(finding("manual-baseline-unavailable", state="dependency-blocked"))
        view_families, _cyclic = families(inventory["records"].get("views", []))
        for family in view_families:
            components = [by_key["views", row["key"]] for row in family]
            replacements = {row["key"] for row in family if native_replacement(row, target, "ir.ui.view")}
            blocked = any(item["state"] == "dependency-blocked" for component in components for item in component["findings"])
            if blocked or any(
                (row.get("withheld") or row.get("website_scope")) and row["key"] not in replacements for row in family
            ):
                for component in components:
                    component["findings"].append(
                        finding("family-baseline-unavailable", state="dependency-blocked" if blocked else "data-dependent/unreached")
                    )
                session.checkpoint()
                continue
            languages = set().union(*(row.get("architectures", {}) for row in family if row["key"] not in replacements)) or {"en_US"}
            for language in sorted(languages):
                run_probe(
                    session,
                    components,
                    partial(load_view_family, rows=family, language=language, replacements=replacements),
                    label=language,
                )
        for row in inventory["records"].get("actions", []):
            if row.get("type") not in {"ir.actions.act_window", "ir.actions.act_window_close"}:
                continue
            if any(item["state"] == "dependency-blocked" for item in by_key["actions", row["key"]]["findings"]):
                continue
            replace_native = native_replacement(row, target, row["type"])
            run_probe(session, [by_key["actions", row["key"]]], partial(load_action, row=row, replace_native=replace_native))
        return report
    finally:
        try:
            session.close()
            session.report["cleanup"] = "complete"
        except Exception:
            session.report["cleanup"] = "incomplete"
            _logger.exception("Native census cleanup failed; retained findings are checkpointed")
            raise
        finally:
            recount(session.report, inventory)
            session.checkpoint(force=True)
