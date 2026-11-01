from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

SCRIPTS = Path(__file__).resolve().parents[1] / "docker/scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import odoo_structural_census as census
    import odoo_structural_extract as extract
    import odoo_structural_native as native
finally:
    sys.path.pop(0)


def inventory(**buckets: list[dict[str, Any]]) -> dict[str, Any]:
    records = {bucket: [] for bucket in census.BUCKETS} | buckets
    return {"binding": "synthetic-source", "records": records, "totals": {key: len(rows) for key, rows in records.items()}}


def target() -> dict[str, Any]:
    return {
        "binding": "synthetic-community20",
        "modules": {"base": []},
        "xmlids": {},
        "view_types": ["form", "list", "qweb"],
        "models": {
            "res.users": {"fields": {"name": {}, "partner_id": {"relation": "res.partner"}}},
            "res.partner": {"fields": {"name": {}}},
            "ir.actions.act_window": {"fields": {}},
        },
        "migration_sources": {"migration.py": "synthetic-source-hash"},
    }


def view(key: str, architecture: str, **values: Any) -> dict[str, Any]:
    return {
        "key": key,
        "model": "res.users",
        "type": "form",
        "mode": "primary",
        "active": True,
        "architectures": {"en_US": architecture},
        **values,
    }


class StaticCensusTests(unittest.TestCase):
    def test_independent_calendar_and_hr_failures_and_two_fields_in_one_view(self) -> None:
        packet = inventory(
            views=[
                view("calendar.legacy", '<form><field name="calendar_default_privacy"/><field name="missing_second"/></form>'),
                view("hr.legacy", '<hierarchy><field name="name"/></hierarchy>', type="hierarchy"),
                view("custom.valid", '<form><field name="name"/></form>'),
            ]
        )
        report = census.collect_static(packet, target())
        components = report["components"]
        references = {item["reference"] for item in components[0]["findings"]}
        self.assertIn("res.users.calendar_default_privacy", references)
        self.assertIn("res.users.missing_second", references)
        self.assertTrue(components[1]["findings"])
        self.assertFalse(components[2]["findings"])
        self.assertEqual(report["coverage"]["views"]["checked"], len(packet["records"]["views"]))

    def test_families_include_primary_siblings_cycles_and_orphans_remain_visible(self) -> None:
        rows = [
            view("root", "<form/>"),
            view("extension", "<data/>", parent="root", mode="extension"),
            view("primary", "<data/>", parent="extension"),
            view("cycle1", "<data/>", parent="cycle2"),
            view("cycle2", "<data/>", parent="cycle1"),
            view("orphan", "<data/>", parent="absent"),
        ]
        groups, cyclic = census.families(rows)
        self.assertEqual({row["key"] for row in groups[0]}, {"root", "extension", "primary"})
        self.assertEqual(cyclic, {"cycle1", "cycle2"})
        report = census.collect_static(inventory(views=rows), target())
        self.assertEqual(report["coverage"]["views"]["dependency-blocked"], len(cyclic) + 1)

    def test_moved_action_and_cross_class_path_conflict(self) -> None:
        selected = target()
        selected["action_paths"] = {"old-settings": ["base.moved_action"]}
        packet = inventory(
            actions=[
                {
                    "key": "mail.legacy_action",
                    "type": "ir.actions.act_window",
                    "path": "old-settings",
                    "model": "res.users",
                    "view_mode": "tree,form,form",
                },
                {"key": "custom.server", "type": "ir.actions.server", "path": "old-settings", "code": "record.name"},
            ]
        )
        report = census.collect_static(packet, selected)
        codes = {item["code"] for row in report["components"] for item in row["findings"]}
        self.assertTrue({"action-path-conflict", "unsupported-view-mode", "duplicate-view-mode", "stored-code-unexecuted"} <= codes)
        self.assertEqual(report["coverage"]["actions"]["inventoried"], len(packet["records"]["actions"]))

    def test_nested_relation_fields_domains_context_and_manual_dependencies(self) -> None:
        packet = inventory(
            views=[
                view(
                    "relation", '<form><field name="partner_id"><form><field name="name"/><field name="gone"/></form></field></form>'
                )
            ],
            fields=[
                {
                    "key": "manual.field",
                    "model": "res.users",
                    "name": "x_manual",
                    "state": "manual",
                    "ttype": "char",
                    "related": "partner_id.gone",
                    "depends": ["partner_id.name"],
                    "compute": "record.deleted",
                }
            ],
            filters=[
                {
                    "key": "filter",
                    "model": "res.users",
                    "domain": "[('partner_id.gone', '=', False)]",
                    "context": "{'default_gone': 1, 'group_by': 'partner_id:month'}",
                }
            ],
        )
        report = census.collect_static(packet, target())
        references = {
            item["reference"] for row in report["components"] for item in row["findings"] if item["code"] == "missing-field"
        }
        self.assertTrue(
            {"res.partner.gone", "res.users.x_manual", "res.users.partner_id.gone", "res.users.deleted", "res.users.gone"}
            <= references
        )
        self.assertNotIn("res.users.partner_id.name", references)

    def test_withheld_translated_inactive_auxiliary_and_missing_totals_are_not_passes(self) -> None:
        packet = inventory(
            views=[view("inactive", "<form/>", active=False, changed_languages=["en_US"])],
            defaults=[{"key": "default", "withheld": ["business-value"]}],
        )
        packet["totals"]["views"] += 2
        packet["records"].pop("templates")
        packet["totals"].pop("templates")
        report = census.collect_static(packet, target())
        self.assertEqual(report["coverage"]["views"]["withheld"], 2)
        self.assertEqual(report["coverage"]["views"]["data-dependent/unreached"], 1)
        self.assertEqual(report["coverage"]["templates"]["gap"], "source-total-missing")

    def test_stored_code_and_arbitrary_expressions_are_never_executed(self) -> None:
        packet = inventory(
            automation=[
                {
                    "key": "stored",
                    "model": "res.users",
                    "code": "raise AssertionError('executed')",
                    "domain": "__import__('builtins').print('must-not-run')",
                }
            ]
        )
        report = census.collect_static(packet, target())
        codes = {item["code"] for row in report["components"] for item in row["findings"]}
        self.assertTrue({"stored-code-unexecuted", "unexecuted-expression-call"} <= codes)

    def test_candidate_coverage_is_bound_to_guard_source_record_and_candidate(self) -> None:
        row = view("calendar.legacy", '<form><field name="gone"/></form>')
        selected = target()
        row["candidate_coverage"] = [
            {
                "candidate": selected["binding"],
                "record_sha256": census.digest(row),
                "source_sha256": next(iter(selected["migration_sources"].values())),
                "guard_receipt": "root-qualified-fixture",
                "code": "missing-field",
                "reference": "res.users.gone",
            }
        ]
        report = census.collect_static(inventory(views=[row]), selected)
        self.assertEqual(report["coverage"]["views"]["candidate-covered"], 1)
        row["architectures"]["en_US"] = '<form><field name="another_gone"/></form>'
        report = census.collect_static(inventory(views=[row]), selected)
        self.assertNotIn("candidate-covered", report["coverage"]["views"])

    def test_source_packet_is_not_mutated_and_invalid_counts_refuse(self) -> None:
        packet = inventory(views=[view("untouched", "<form/>")])
        original = copy.deepcopy(packet)
        census.collect_static(packet, target())
        self.assertEqual(packet, original)
        packet["totals"]["views"] = 0
        with self.assertRaises(ValueError):
            census.collect_static(packet, target())


class NativeOrchestrationTests(unittest.TestCase):
    def test_missing_module_blocks_reverse_dependencies_without_losing_independent_closure(self) -> None:
        graph = {"base": set(), "good": {"base"}, "bad": {"missing_enterprise"}, "dependent": {"bad"}}
        intended = native.closure({"good", "dependent"}, graph)
        blocked = native.reverse_closure(intended - graph.keys(), graph)
        self.assertEqual(intended - blocked, {"base", "good"})
        self.assertTrue({"dependent", "bad", "missing_enterprise"} <= blocked)

    def test_failure_does_not_stop_later_component_and_family_is_not_called_passed(self) -> None:
        class Session(native.DisposableCensus):
            def __init__(self) -> None:
                super().__init__(time.monotonic() + 60)

            @staticmethod
            @contextmanager
            def probe() -> Any:
                yield object()

        session = Session()
        failed: list[dict[str, Any]] = [
            {"key": key, "native": "unreached", "findings": []} for key in ("parent", "bad", "dependent")
        ]
        later: list[dict[str, Any]] = [{"key": "valid", "native": "unreached", "findings": []}]

        def broken(_env: Any) -> None:
            raise native.ViewFailure("bad")

        with self.assertLogs("odoo_structural_native", level="ERROR"):
            native.run_probe(session, failed, broken)
        native.run_probe(session, later, lambda _env: None)
        self.assertEqual(failed[1]["native"], "native-reproduced")
        self.assertEqual(failed[0]["findings"][0]["state"], "dependency-blocked")
        self.assertEqual(later[0]["native"], "checked")

    def test_manual_code_never_enters_baseline_and_deadline_is_finite(self) -> None:
        self.assertFalse(native.manual_supported({"ttype": "char", "compute": "raise AssertionError"}))
        self.assertFalse(native.manual_supported({"ttype": "selection", "selection": "arbitrary()"}))
        self.assertTrue(native.manual_supported({"ttype": "selection", "selection": [["technical", "screened label"]]}))
        self.assertTrue(native.manual_supported({"ttype": "char"}))
        with self.assertRaises(TimeoutError):
            native.DisposableCensus(0).allocate()
        with self.assertRaises(ValueError):
            native.DisposableCensus(0).drop("foreign-database")

    def test_unreached_variant_cannot_be_overwritten_by_later_success(self) -> None:
        class Session(native.DisposableCensus):
            def __init__(self) -> None:
                super().__init__(time.monotonic() + 60)

            @staticmethod
            @contextmanager
            def probe() -> Any:
                yield object()

        component = [{"key": "translated", "native": "unreached", "findings": []}]

        def withheld(_env: Any) -> None:
            raise native.ProbeUnreached("withheld translation")

        native.run_probe(Session(), component, withheld, label="fr_FR")
        native.run_probe(Session(), component, lambda _env: None, label="en_US")
        self.assertEqual(component[0]["native"], "unreached")


class CoverageRegressionTests(unittest.TestCase):
    def test_relational_domain_and_inheritance_fragments_do_not_guess_the_wrong_model(self) -> None:
        selected = target()
        selected["models"]["res.partner"]["fields"]["ref"] = {}
        packet = inventory(
            views=[
                view("domain", "<form><field name=\"partner_id\" domain=\"[('ref', '=', False)]\"/></form>"),
                view("anchor", '<field name="partner_id" position="after"><field name="ref"/></field>'),
                view("xpath", '<xpath expr="//field[@name=\'partner_id\']/form" position="inside"><field name="ref"/></xpath>'),
            ]
        )
        report = census.collect_static(packet, selected)
        rows = {row["key"]: row for row in report["components"]}
        self.assertFalse(rows["domain"]["findings"])
        self.assertFalse(rows["anchor"]["checked"])
        self.assertFalse(rows["xpath"]["checked"])
        self.assertFalse(any(item["code"] == "missing-field" for row in rows.values() for item in row["findings"]))

    def test_withheld_content_and_static_blockers_have_separate_counts(self) -> None:
        packet = inventory(
            views=[
                view("bad", '<form><field name="gone"/></form>'),
                view("good", "<form/>"),
                view("hidden", "<form/>", changed_languages=["en_US"]),
            ]
        )
        report = census.collect_static(packet, target())
        self.assertEqual(report["coverage"]["views"]["checked"], 2)
        self.assertEqual(report["coverage"]["views"]["static-findings"], 1)
        self.assertEqual(report["coverage"]["views"]["native-unreached"], 3)

    def test_reduced_and_missing_manual_baselines_stay_blocked(self) -> None:
        selected = target()
        selected.update(
            blocked_modules={"hr"}, blocked_models=["hr.employee"], blocked_fields=["res.users.x_code"], conditional=True
        )
        packet = inventory(
            views=[
                view("hr.owned", "<form/>", model="hr.employee", owners=["hr"]),
                view("manual.use", '<form><field name="x_code"/></form>'),
            ]
        )
        report = census.collect_static(packet, selected)
        self.assertEqual(report["coverage"]["views"]["dependency-blocked"], 2)
        self.assertEqual(report["coverage"]["views"]["conditional"], 2)

    def test_template_attributes_attribute_text_and_numeric_action_ids_are_visible(self) -> None:
        packet = inventory(
            views=[
                view("template", '<kanban><t t-out="record.name"/></kanban>'),
                view(
                    "attribute",
                    '<field name="partner_id" position="attributes"><attribute name="domain">[("gone", "=", False)]</attribute></field>',
                ),
                view("button", '<form><button name="1234" type="action"/></form>'),
            ]
        )
        report = census.collect_static(packet, target())
        codes = {item["code"] for row in report["components"] for item in row["findings"]}
        self.assertTrue({"template-expression-unexecuted", "field-model-unresolved", "numeric-action-binding-unavailable"} <= codes)

    def test_private_projector_keeps_identity_topology_flags_classes_and_unknown_totals(self) -> None:
        extracted = {
            "binding": "synthetic-extraction",
            "tables": {
                "ir_model_data": {
                    "count": 2,
                    "rows": [
                        {"id": 1, "module": "custom", "name": "root", "model": "ir.ui.view", "res_id": 12, "noupdate": True},
                        {"id": 2, "module": "custom", "name": "group", "model": "res.groups", "res_id": 3, "noupdate": False},
                    ],
                },
                "ir_ui_view": {
                    "count": 2,
                    "rows": [
                        {
                            "id": 12,
                            "model": "res.users",
                            "type": "form",
                            "mode": "primary",
                            "active": True,
                            "arch_db": {"en_US": "<form/>"},
                            "priority": 17,
                            "history_present": True,
                        },
                        {
                            "id": 13,
                            "model": "res.users",
                            "type": "qweb",
                            "inherit_id": 12,
                            "website_id": 7,
                            "arch_db": {"en_US": "<data/>"},
                        },
                    ],
                },
                "ir_ui_view_custom": {"count": 1, "rows": [{"id": 1, "ref_id": 12}]},
                "ir_ui_view_group_rel": {"count": 1, "rows": [{"view_id": 12, "group_id": 3}]},
                "ir_actions": {"count": 2, "rows": [{"id": 4, "type": "ir.actions.server"}, {"id": 5, "type": "custom.action"}]},
                "ir_act_server": {"count": 1, "rows": [{"id": 4, "code": True}]},
            },
        }
        projected = extract.project_private_inventory(extracted)
        root, extension = projected["records"]["views"]
        self.assertEqual(extension["parent"], root["key"])
        self.assertEqual(root["xmlids"], ["custom.root"])
        self.assertTrue(root["noupdate"])
        self.assertTrue(root["customization_present"])
        self.assertTrue(root["history_present"])
        self.assertEqual(root["source_order"], 12)
        self.assertEqual(root["groups"], ["custom.group"])
        self.assertEqual(extension["website_scope"], "website:7")
        self.assertEqual(projected["action_totals"], {"ir.actions.server": 1, "custom.action": 1})
        self.assertIsNone(projected["totals"]["fields"])
        self.assertFalse(projected["screened"])
        self.assertEqual(projected["totals"]["qweb"], 1)
        census.collect_static(projected, target())

    def test_artifact_binding_rejects_file_drift_and_checkpoints_survive_a_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("original source")
            files = {("fixture", source.as_posix().lstrip("/")): hashlib.sha256(source.read_bytes()).hexdigest()}
            native.verify_file(source, files)
            source.write_text("changed source")
            with self.assertRaises(ValueError):
                native.verify_file(source, files)
            session = native.DisposableCensus(time.monotonic() + 60, output=root / "report.json")
            session.report = {"components": [{"key": "already-collected"}]}
            session.checkpoint()
            with self.assertRaises(ValueError):
                session.drop("foreign")
            self.assertEqual(json.loads((root / "report.json").read_text())["components"], session.report["components"])

    def test_field_metadata_booleans_are_not_expressions(self) -> None:
        packet = inventory(fields=[{"key": "base.field", "model": "res.users", "name": "name", "readonly": True, "required": True}])
        report = census.collect_static(packet, target())
        self.assertFalse(report["components"][0]["findings"])
        self.assertTrue(report["components"][0]["checked"])

    def test_native_stock_disposition_needs_exact_record_candidate_and_target_class(self) -> None:
        selected = target()
        selected["xmlids"]["base.stock"] = {"model": "ir.ui.view"}
        row = view("old.stock", "<form/>", xmlids=["base.stock"])
        row["candidate_coverage"] = [
            {
                "candidate": selected["binding"],
                "record_sha256": census.digest(row),
                "source_sha256": next(iter(selected["migration_sources"].values())),
                "guard_receipt": "qualified-root-disposition",
                "code": "native20-stock-replacement",
                "reference": "base.stock",
            }
        ]
        self.assertTrue(native.native_replacement(row, selected, "ir.ui.view"))
        selected["xmlids"]["base.stock"]["model"] = "ir.actions.act_window"
        self.assertFalse(native.native_replacement(row, selected, "ir.ui.view"))
        self.assertTrue(native.literal_expression("{'default_name': False}"))
        self.assertFalse(native.literal_expression("{'default_name': callable() }"))


class NativeView:
    _name = "ir.ui.view"

    def __init__(self, record_id: int, *, fail_validation: bool = False) -> None:
        self.id = record_id
        self.values: dict[str, Any] = {}
        self.fail_validation = fail_validation

    def write(self, values: dict[str, Any]) -> None:
        self.values.update(values)

    @property
    def mode(self) -> str:
        return self.values.get("mode", "primary")

    @property
    def priority(self) -> int:
        return self.values.get("priority", 16)

    @property
    def active(self) -> bool:
        return self.values.get("active", True)

    @property
    def inherit_id(self) -> Any:
        parent_id = self.values.get("inherit_id")
        return SimpleNamespace(id=parent_id) if parent_id else False

    def _check_xml(self) -> None:
        if self.fail_validation:
            raise ValueError("Invalid final combined family")


class ViewEnvironment:
    def __init__(self, references: dict[str, Any] | None = None, *, languages: set[str] | None = None) -> None:
        self.context: dict[str, Any] = {}
        self.references = references or {}
        self.created: list[NativeView] = []
        self.languages = {"en_US"} if languages is None else languages

    def __call__(self, *, context: dict[str, Any]) -> ViewEnvironment:
        self.context = context
        return self

    def __getitem__(self, model: str) -> Any:
        if model == "res.lang":
            return SimpleNamespace(search_count=lambda domain: int(domain[0][2] in self.languages))
        if model == "ir.ui.view":
            return SimpleNamespace(create=self.create)
        raise KeyError(model)

    def ref(self, xmlid: str, *, raise_if_not_found: bool = True) -> Any:
        record = self.references.get(xmlid)
        if record is None and raise_if_not_found:
            raise ValueError(xmlid)
        return record

    def create(self, values: dict[str, Any]) -> NativeView:
        record = NativeView(100 + len(self.created))
        record.write(values)
        self.created.append(record)
        return record


class ReviewedRegressionTests(unittest.TestCase):
    def test_missing_manual_relations_and_related_paths_do_not_poison_a_safe_batch(self) -> None:
        created: list[dict[str, Any]] = []
        models = {
            "res.users": SimpleNamespace(_fields={"partner_id": SimpleNamespace(type="many2one", comodel_name="res.partner")}),
            "res.partner": SimpleNamespace(_fields={"name": SimpleNamespace(type="char")}),
            "ir.model": SimpleNamespace(_get_id=lambda _name: 1),
            "ir.model.fields": SimpleNamespace(create=created.extend),
        }

        class Environment:
            @staticmethod
            def __contains__(model: str) -> bool:
                return model in models

            @staticmethod
            def __getitem__(model: str) -> Any:
                return models[model]

        fields = [
            {"key": "removed", "name": "x_removed", "ttype": "many2one", "relation": "removed.model"},
            {"key": "gone_path", "name": "x_gone", "ttype": "char", "related": "partner_id.gone"},
            {"key": "pending_path", "name": "x_pending", "ttype": "char", "related": "x_partner.name"},
            {"key": "partner", "name": "x_partner", "ttype": "many2one", "relation": "res.partner"},
            {"key": "safe_path", "name": "x_name", "ttype": "char", "related": "partner_id.name"},
        ]
        source = inventory(fields=[{**row, "state": "manual", "model": "res.users"} for row in fields])
        blocked = native.prepare_manual(Environment(), source)
        self.assertEqual(blocked, {("fields", "removed"), ("fields", "gone_path")})
        self.assertEqual({row["name"] for row in created}, {"x_pending", "x_partner", "x_name"})

    def test_manual_row_successes_do_not_repeat_registry_reload_after_batch_failure(self) -> None:
        registry = MagicMock()
        factory = SimpleNamespace(delete=MagicMock(), new=MagicMock(return_value=registry))
        modules = {
            "odoo": SimpleNamespace(SUPERUSER_ID=1, api=SimpleNamespace(Environment=lambda *_args: object())),
            "odoo.orm.registry": SimpleNamespace(Registry=factory),
        }
        source = inventory(fields=[{"key": str(index), "state": "manual"} for index in range(5)])
        session = native.DisposableCensus(time.monotonic() + 60)
        session.baseline = "synthetic"
        applied = []

        def apply(_env: Any, selected: dict[str, Any]) -> set[tuple[str, str]]:
            if len(selected["records"]["fields"]) > 1:
                raise ValueError("Synthetic batched declaration failure")
            applied.extend(row["key"] for row in selected["records"]["fields"])
            return set()

        with patch.dict(sys.modules, modules), patch.object(native, "prepare_manual", side_effect=apply):
            with self.assertLogs("odoo_structural_native", level="ERROR"):
                self.assertIs(session.manual_baseline(registry, source), registry)
        self.assertEqual(applied, [row["key"] for row in source["records"]["fields"]])
        self.assertEqual(factory.new.call_count, 2)
        self.assertFalse(session.manual_blocked)

    def test_manual_timeout_is_not_a_declaration_failure_or_a_registry_reload(self) -> None:
        registry = MagicMock()
        factory = SimpleNamespace(delete=MagicMock(), new=MagicMock(return_value=registry))
        modules = {
            "odoo": SimpleNamespace(SUPERUSER_ID=1, api=SimpleNamespace(Environment=lambda *_args: object())),
            "odoo.orm.registry": SimpleNamespace(Registry=factory),
        }
        source = inventory(fields=[{"key": "manual", "state": "manual"}])
        for failures, reloads in (([TimeoutError("deadline")], 0), ([ValueError("batch"), TimeoutError("deadline")], 1)):
            session = native.DisposableCensus(time.monotonic() + 60)
            session.baseline = "synthetic"
            factory.new.reset_mock()
            factory.delete.reset_mock()
            with patch.dict(sys.modules, modules), patch.object(native, "prepare_manual", side_effect=failures):
                if reloads:
                    with self.assertLogs("odoo_structural_native", level="ERROR"), self.assertRaises(TimeoutError):
                        session.manual_baseline(registry, source)
                else:
                    with self.assertRaises(TimeoutError):
                        session.manual_baseline(registry, source)
            self.assertEqual(factory.new.call_count, reloads)
            self.assertFalse(session.manual_blocked)
        session = native.DisposableCensus(time.monotonic() + 60)
        session.baseline = "synthetic"
        factory.new.reset_mock()
        with (
            patch.dict(sys.modules, modules),
            patch.object(native, "prepare_manual", side_effect=ValueError("batch")),
            patch.object(session, "check_deadline", side_effect=[None, TimeoutError("deadline")]),
            self.assertLogs("odoo_structural_native", level="ERROR"),
            self.assertRaises(TimeoutError),
        ):
            session.manual_baseline(registry, source)
        factory.new.assert_not_called()

    def test_manual_deadline_retains_healthy_static_diagnosis_without_probing_mutated_registry(self) -> None:
        source = inventory(
            fields=[{"key": "manual", "state": "manual", "name": "x_safe", "model": "res.users", "ttype": "char"}],
            views=[view("bad", '<form><field name="gone"/></form>')],
        )
        artifact = {"release_compatibility": {"complete": True, "modules": [{"name": "base", "depends": []}]}}
        candidate = {"artifact": artifact, "platform": "linux/amd64", "intended_modules": ["base"], "migration_sources": []}
        registry = MagicMock()
        modules = {"odoo": SimpleNamespace(SUPERUSER_ID=1, api=SimpleNamespace(Environment=lambda *_args: object()))}
        report_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "report.json"

        def manual_deadline(_registry: Any, _source: Any) -> Any:
            saved = json.loads(report_path.read_text())
            checkpoint_components = {row["key"]: row for row in saved["components"]}
            self.assertIn("missing-field", {item["code"] for item in checkpoint_components["bad"]["findings"]})
            self.assertIn("manual-baseline-pending", {item["code"] for item in checkpoint_components["manual"]["findings"]})
            raise TimeoutError("deadline")

        with (
            patch.dict(sys.modules, modules),
            patch.object(native, "verify_image_declaration"),
            patch.object(native, "declared_files", return_value={}),
            patch.object(native, "check_native_sources", return_value={}),
            patch.object(native, "module_graph", return_value={"base": set()}),
            patch.object(native.DisposableCensus, "build", return_value=registry),
            patch.object(native.DisposableCensus, "manual_baseline", side_effect=manual_deadline),
            patch.object(native, "registry_surface", return_value=target()) as surface,
            patch.object(native, "run_probe") as probe,
            self.assertLogs("odoo_structural_native", level="ERROR"),
        ):
            report = native.collect_native(source, candidate, [], declaration={}, deadline=time.monotonic() + 60, output=report_path)
        surface.assert_called_once()
        probe.assert_not_called()
        components = {row["key"]: row for row in report["components"]}
        self.assertIn("missing-field", {item["code"] for item in components["bad"]["findings"]})
        self.assertIn("manual-baseline-deadline-unreached", {item["code"] for item in components["manual"]["findings"]})
        self.assertNotIn("manual-baseline-unavailable", {item["code"] for item in components["manual"]["findings"]})
        self.assertEqual(report["cleanup"], "complete")

    def test_typed_stale_xmlids_are_structural_only_when_the_technical_inventory_is_complete(self) -> None:
        row = view(
            "typed",
            '<form groups="x.stale_group"><button name="%(x.stale_action)d" type="action"/></form>',
            context="{'default_name': ref('x.business_or_stale')}",
        )
        source = inventory(views=[row])
        source["out_of_scope"] = {"nontechnical_xmlids": 12}
        report = census.collect_static(source, target())
        findings = {item["reference"]: item for item in report["components"][0]["findings"] if item["reference"]}
        for name in ("x.stale_group", "x.stale_action"):
            self.assertEqual(findings[name]["code"], "missing-xmlid")
        self.assertEqual(findings["x.business_or_stale"]["code"], "xmlid-scope-unresolved")
        source["totals"]["xmlids"] = 1
        report = census.collect_static(source, target())
        self.assertFalse(any(item["code"] == "missing-xmlid" for item in report["components"][0]["findings"]))
        source["records"]["xmlids"] = [{"key": "base.hidden", "withheld": ["name"]}]
        report = census.collect_static(source, target())
        view_component = next(component for component in report["components"] if component["bucket"] == "views")
        self.assertFalse(any(item["code"] == "missing-xmlid" for item in view_component["findings"]))

    def test_unavailable_target_language_does_not_claim_fallback_was_probed(self) -> None:
        row = view("translated", "<form/>")
        row["architectures"]["fr_FR"] = "<form/>"
        with self.assertRaises(native.ProbeUnreached) as error:
            native.load_view_family(ViewEnvironment(), [row], "fr_FR")
        self.assertEqual(error.exception.code, "target-language-unavailable")

    def test_stock_action_disposition_precedes_other_unreached_reasons(self) -> None:
        row = {
            "key": "stock",
            "type": "ir.actions.act_window",
            "xmlids": ["base.stock"],
            "context": "ref('x.dynamic')",
            "withheld": ["domain"],
            "view_references": ["base.view"],
        }
        env = ViewEnvironment({"base.stock": SimpleNamespace(id=1, _name="ir.actions.act_window")})
        with self.assertRaises(native.ProbeUnreached) as error:
            native.load_action(env, row)
        self.assertEqual(error.exception.code, "stock-upgrade-disposition-required")

    def test_literal_action_expressions_remain_untested_after_native_creation(self) -> None:
        created = []

        class Environment:
            @staticmethod
            def __getitem__(_model: str) -> Any:
                return SimpleNamespace(create=created.append)

        for attribute in ("context", "domain"):
            row = {"key": "custom.action", "type": "ir.actions.act_window", "model": "res.users", attribute: "5"}
            report = census.collect_static(inventory(actions=[row]), target())
            component = report["components"][0]
            self.assertFalse(component["checked"])
            self.assertIn(
                ("action-expression-unvalidated", attribute, "data-dependent/unreached"),
                {(item["code"], item["reference"], item["state"]) for item in component["findings"]},
            )
            with self.assertRaises(native.ProbeUnreached):
                native.load_action(Environment(), row)
        self.assertEqual(len(created), 2)

    def test_aggregate_base_receipts_are_namespaced_and_bind_actual_files(self) -> None:
        from odoo_release_inventory import build_inventory
        from odoo_release_inventory import inventory as source_inventory

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for role in ("runtime", "devtools"):
                code = root / role
                code.mkdir()
                (code / "README.md").write_text(role)
                if role == "runtime":
                    module = code / "fixture_module"
                    peer = code / "fixture_peer"
                    module.mkdir()
                    (peer / "static").mkdir(parents=True)
                    (module / "__manifest__.py").write_text(repr({"depends": [], "data": ["views.xml"]}))
                    (peer / "__manifest__.py").write_text(repr({"depends": []}))
                    (module / "views.xml").write_text(
                        '<odoo><record id="menu" model="ir.ui.menu"><field name="web_icon">fixture_peer,static/icon.png</field></record></odoo>'
                    )
                    (peer / "static/icon.png").write_bytes(b"synthetic resource")
                files = source_inventory([code])[0]
                (root / f"base-{role}-inventory.json").write_text(json.dumps(files))
                sources.append(
                    {
                        "input_name": "base:" + role,
                        "repository": "fixture/" + role,
                        "commit": role,
                        "roots": [str(root)],
                        "files": files,
                    }
                )
            addon = root / "addon"
            addon.mkdir()
            migration = addon / "migration.py"
            migration.write_text("synthetic migration source")
            sources.append({"input_name": "addon:fixture", "repository": "fixture/addon", "commit": "addon", "roots": [str(addon)]})
            source = root / "runtime/README.md"
            roots = [root / "runtime"]
            declaration = build_inventory({"addon_paths": list(map(str, roots)), "sources": sources})
            files = native.declared_files(declaration, "linux/amd64", roots, evidence=root)
            native.verify_file(root / "runtime/fixture_peer/static/icon.png", files)
            with self.assertRaises(ValueError):
                native.declared_files(declaration, "linux/amd64", [root], evidence=root)
            native.verify_file(source, files)
            native.verify_file(migration, files)
            migration.write_text("drifted addon migration")
            with self.assertRaises(ValueError):
                native.verify_file(migration, files)
            source.write_text("changed after immutable receipt")
            with self.assertRaises(ValueError):
                native.verify_file(source, files)
            receipt = root / "base-runtime-inventory.json"
            values = json.loads(receipt.read_text())
            values[0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            receipt.write_text(json.dumps(values))
            with self.assertRaises(ValueError):
                native.declared_files(declaration, "linux/amd64", roots, evidence=root)

    def test_extraction_counts_business_xmlids_without_reading_their_names(self) -> None:
        class SQLText:
            def __init__(self, value: str) -> None:
                self.value = value

            def __str__(self) -> str:
                return self.value

            def format(self, *args: Any) -> SQLText:
                return SQLText(self.value.format(*map(str, args)))

            def join(self, iterable: Any) -> SQLText:
                return SQLText(self.value.join(map(str, iterable)))

        class Cursor:
            rows: list[tuple[Any, ...]]

            def execute(self, query: Any, parameters: Any = None) -> None:
                statement = str(query)
                bindings = tuple(parameters or ())
                if "information_schema" in statement:
                    if not bindings:
                        raise AssertionError("Table binding is required")
                    table = bindings[0]
                    columns = {
                        "ir_model_data": ["module", "name", "model", "res_id", "noupdate"],
                        "ir_actions": ["id", "type"],
                        "ir_model_fields": ["id", "model", "name", "state", "ttype", "relation", "domain", "on_delete", "size"],
                        "ir_cron": ["id", "ir_actions_server_id", "active", "priority", "cron_name", "user_id", "nextcall"],
                    }
                    self.rows = [(column,) for column in columns.get(table, [])]
                elif "count(*)" in statement:
                    self.rows = [(8,)]
                elif "FROM ir_actions" in statement:
                    self.rows = [(5, "custom.action")]
                elif "FROM ir_model_fields" in statement:
                    self.rows = [
                        (
                            10,
                            "res.users",
                            "x_relation",
                            "manual",
                            "many2one",
                            "res.partner",
                            "[('gone', '=', False)]",
                            "restrict",
                            64,
                        )
                    ]
                elif "FROM ir_cron" in statement:
                    if any(name in statement for name in ("cron_name", "user_id", "nextcall")):
                        raise AssertionError("Scheduled action business/operator values must not be selected")
                    self.rows = [(1, 5, True, 7)]
                elif "FROM ir_model_data" in statement:
                    if not bindings:
                        raise AssertionError("Technical model scope is required")
                    if "custom.action" not in bindings[0] or "res.partner" in bindings[0]:
                        raise AssertionError("Business XMLID names must never be selected")
                    self.rows = [("fixture", "action", "custom.action", 5, False)]
                else:
                    self.rows = []

            def fetchall(self) -> list[tuple[Any, ...]]:
                return self.rows

            def fetchone(self) -> tuple[Any, ...]:
                return self.rows[0]

        sql = SimpleNamespace(SQL=SQLText, Identifier=SQLText)
        with patch.dict(sys.modules, {"psycopg2": SimpleNamespace(sql=sql)}):
            extracted = extract.extract_private_technical(Cursor(), "synthetic")
        projected = extract.project_private_inventory(extracted)
        self.assertEqual(projected["out_of_scope"]["nontechnical_xmlids"], 8)
        self.assertEqual([row["key"] for row in projected["records"]["xmlids"]], ["fixture.action"])
        self.assertEqual(projected["totals"]["xmlids"], 1)
        field = projected["records"]["fields"][0]
        created: list[dict[str, Any]] = []

        class ManualEnvironment:
            @staticmethod
            def __contains__(_model: str) -> bool:
                return True

            @staticmethod
            def __getitem__(model: str) -> Any:
                return {
                    "res.users": SimpleNamespace(_fields={}),
                    "ir.model": SimpleNamespace(_get_id=lambda _name: 1),
                    "ir.model.fields": SimpleNamespace(create=created.extend),
                }[model]

        self.assertFalse(native.prepare_manual(ManualEnvironment(), projected))
        self.assertEqual(
            {name: created[0][name] for name in ("domain", "on_delete", "size")},
            {name: field[name] for name in ("domain", "on_delete", "size")},
        )
        self.assertEqual(projected["records"]["automation"][0]["source_action"], "action:5")

    def test_filter_domain_uses_search_model_and_domain_uses_comodel(self) -> None:
        selected = target()
        row = view(
            "search",
            "<search><field name=\"partner_id\" filter_domain=\"[('partner_id', 'child_of', raw_value)]\" domain=\"[('name', '=', False)]\"/></search>",
        )
        results = census.collect_static(inventory(views=[row]), selected)["components"][0]["findings"]
        self.assertFalse(any(item["code"] == "missing-field" for item in results))
        row["architectures"]["en_US"] = (
            "<search><field name=\"partner_id\" filter_domain=\"[('name_on_partner_only', '=', False)]\"/></search>"
        )
        selected["models"]["res.partner"]["fields"]["name_on_partner_only"] = {}
        results = census.collect_static(inventory(views=[row]), selected)["components"][0]["findings"]
        self.assertIn("res.users.name_on_partner_only", {item["reference"] for item in results})

    def test_stock_edit_flags_do_not_authorize_overlay_and_noupdate_does(self) -> None:
        for flags in ({"arch_updated": True}, {"customization_present": True}, {"noupdate": False}):
            stock = NativeView(5)
            env = ViewEnvironment({"base.stock": stock})
            row = view("stock", "<form/>", xmlids=["base.stock"], **flags)
            with self.assertRaises(native.ProbeUnreached):
                native.load_view_family(env, [row], "en_US")
            self.assertFalse(stock.values)
        stock = NativeView(5)
        row = view("retained", "<form/>", xmlids=["base.stock"], noupdate=True, arch_fs="old/file.xml", arch_updated=False)
        native.load_view_family(ViewEnvironment({"base.stock": stock}), [row], "en_US")
        self.assertEqual(stock.values["arch"], row["architectures"]["en_US"])
        self.assertNotIn("arch_fs", stock.values)
        self.assertNotIn("arch_updated", stock.values)

    def test_qualified_stock_replacement_drops_old_arch_findings_and_old_language_requirements(self) -> None:
        selected = target()
        selected["xmlids"]["base.stock"] = {"model": "ir.ui.view"}
        row = view("stock", '<form attrs="{}"><field name="obsolete"/></form>', xmlids=["base.stock"])
        row["architectures"] = {"old_language": row["architectures"]["en_US"]}
        row["candidate_coverage"] = [
            {
                "candidate": selected["binding"],
                "record_sha256": census.digest(row),
                "source_sha256": next(iter(selected["migration_sources"].values())),
                "guard_receipt": "qualified",
                "code": "native20-stock-replacement",
                "reference": "base.stock",
            }
        ]
        report = census.collect_static(inventory(views=[row]), selected)
        self.assertEqual({item["code"] for item in report["components"][0]["findings"]}, {"native20-stock-replacement"})
        stock = NativeView(5)
        native.load_view_family(ViewEnvironment({"base.stock": stock}), [row], "en_US", replacements={row["key"]})
        self.assertFalse(stock.values)
        row["architectures"]["old_language"] += " "
        report = census.collect_static(inventory(views=[row]), selected)
        self.assertIn("obsolete-view-attribute", {item["code"] for item in report["components"][0]["findings"]})

    def test_numeric_actions_bind_by_technical_identity_without_mutating_original_architecture(self) -> None:
        arch = '<form><button name="1234" type="action"/><field name="name"/></form>'
        extracted = {
            "binding": "synthetic",
            "out_of_scope": {"nontechnical_xmlids": 0},
            "tables": {
                "ir_model_data": {
                    "count": 2,
                    "rows": [
                        {"module": "base", "name": "action", "model": "ir.actions.act_window", "res_id": 1234},
                        {"module": "__export__", "name": "private_business_identifier", "model": "res.partner", "res_id": 1234},
                    ],
                },
                "ir_actions": {"count": 1, "rows": [{"id": 1234, "type": "ir.actions.act_window"}]},
                "ir_ui_view": {"count": 1, "rows": [{"id": 10, "model": "res.users", "arch_db": arch, "type": "form"}]},
            },
        }
        projected = extract.project_private_inventory(extracted)
        row = projected["records"]["views"][0]
        self.assertEqual(row["action_bindings"], {"1234": ["base.action"]})
        self.assertNotIn("private_business_identifier", json.dumps(projected))
        self.assertEqual(projected["out_of_scope"]["nontechnical_xmlids"], 1)
        self.assertEqual(projected["totals"]["xmlids"], 1)
        action = SimpleNamespace(id=7, _name="ir.actions.act_window")
        env = ViewEnvironment({"base.action": action})
        bound_arch = native.bind_view_actions(env, row, arch)
        button = ElementTree.fromstring(bound_arch).find("button")
        assert button is not None
        self.assertEqual(button.get("name"), str(action.id))
        self.assertEqual(row["architectures"]["en_US"], arch)
        with self.assertRaises(native.ProbeUnreached):
            native.bind_view_actions(ViewEnvironment(), row, arch)
        selected = target()
        report = census.collect_static(projected, selected)
        self.assertIn(
            "numeric-action-target-unavailable",
            {item["code"] for component in report["components"] if component["bucket"] == "views" for item in component["findings"]},
        )

    def test_final_family_failure_is_observed_without_claiming_culprit_and_checked_counts_update(self) -> None:
        class Session(native.DisposableCensus):
            @staticmethod
            @contextmanager
            def probe() -> Any:
                yield object()

        source = inventory(views=[view("root", "<form/>"), view("child", "<form/>")])
        report = census.collect_static(source, target())
        session = Session(time.monotonic() + 60)
        session.report = report

        def fail(_env: Any) -> None:
            raise native.ViewFailure("root", attribution="family")

        with self.assertLogs("odoo_structural_native", level="ERROR"):
            native.run_probe(session, report["components"], fail)
        census.recount(report, source)
        self.assertEqual({row["native"] for row in report["components"]}, {"unreached"})
        self.assertEqual(report["family_failures"][0]["state"], "native-reproduced")
        self.assertEqual(report["family_failures"][0]["validation_record"], "root")
        self.assertEqual(set(report["family_failures"][0]["members"]), {"root", "child"})
        self.assertEqual(report["coverage"]["views"]["checked"], 0)
        self.assertEqual(report["coverage"]["views"]["native-unreached"], 2)
        report["components"][0]["findings"].append(census.finding("deadline-unreached", state="data-dependent/unreached"))
        census.recount(report, source)
        self.assertEqual(report["coverage"]["views"]["checked"], 0)

    def test_replaced_action_uses_target_without_old_expression_or_path_blockers(self) -> None:
        selected = target()
        selected["xmlids"]["base.action"] = {"model": "ir.actions.act_window"}
        selected["action_paths"] = {"old-conflict": ["base.other_action"]}
        row: dict[str, Any] = {
            "key": "old.action",
            "xmlids": ["base.action"],
            "type": "ir.actions.act_window",
            "model": "res.users",
            "context": "ref('old.missing')",
            "path": "old-conflict",
            "view_mode": "tree",
            "res_id": 42,
        }
        row["candidate_coverage"] = [
            {
                "candidate": selected["binding"],
                "record_sha256": census.digest(row),
                "source_sha256": next(iter(selected["migration_sources"].values())),
                "guard_receipt": "qualified",
                "code": "native20-stock-replacement",
                "reference": "base.action",
            }
        ]
        report = census.collect_static(inventory(actions=[row]), selected)
        self.assertEqual({item["code"] for item in report["components"][0]["findings"]}, {"native20-stock-replacement"})
        env = ViewEnvironment({"base.action": SimpleNamespace(_name="ir.actions.act_window", id=5)})
        native.load_action(env, row, replace_native=True)
        self.assertFalse(env.created)

    def test_extension_order_compares_created_and_native_siblings_at_actual_parent_and_priority(self) -> None:
        architecture = '<field name="name" position="attributes"><attribute name="string">Synthetic</attribute></field>'
        root = view("root", '<form><field name="name"/></form>', source_order=1)
        native_row = view(
            "stock", architecture, xmlids=["base.stock"], noupdate=True, parent="root", source_order=3, mode="extension"
        )
        created_row = view("new", architecture, parent="root", source_order=2, mode="extension")
        env = ViewEnvironment({"base.stock": NativeView(5)})
        findings = native.load_view_family(env, [root, created_row, native_row], "en_US")
        self.assertEqual([(item["code"], item["state"]) for item in findings], [("relative-view-order-unreproduced", "conditional")])
        stock = NativeView(5)
        stock.values.update(mode="extension", active=True, inherit_id=100, priority=32)
        native_row["priority"] = 16
        created_row["priority"] = 32
        env = ViewEnvironment({"base.stock": stock})
        self.assertTrue(native.load_view_family(env, [root, created_row, native_row], "en_US", replacements={"stock"}))
        stock.values["priority"] = 24
        env = ViewEnvironment({"base.stock": stock})
        self.assertFalse(native.load_view_family(env, [root, created_row, native_row], "en_US", replacements={"stock"}))

    def test_unannotated_rows_do_not_hash_for_candidate_coverage(self) -> None:
        with patch.object(census, "digest", side_effect=AssertionError("unannotated row was hashed")):
            self.assertFalse(census.qualified_coverage(view("unannotated", "<form/>"), target()))

    def test_checkpoint_throttles_normal_progress_and_forces_latest_error_or_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            session = native.DisposableCensus(time.monotonic() + 60, output=path)
            report: dict[str, Any] = {"components": []}
            session.report = report
            with patch.object(native.time, "monotonic", return_value=100.0):
                session.checkpoint(force=True)
            report["progress"] = "pending"
            with patch.object(native.time, "monotonic", return_value=100.0 + native.CHECKPOINT_INTERVAL / 2):
                session.checkpoint()
                self.assertNotIn("progress", json.loads(path.read_text()))
                session.checkpoint(force=True)
                self.assertEqual(json.loads(path.read_text())["progress"], "pending")
            report["progress"] = "complete"
            with patch.object(native.time, "monotonic", return_value=100.0 + native.CHECKPOINT_INTERVAL * 2):
                session.checkpoint()
            self.assertEqual(json.loads(path.read_text())["progress"], "complete")

    def test_missing_xmlid_scope_is_unknown_without_identifying_excluded_business_rows(self) -> None:
        unscoped: dict[str, Any] = {"binding": "synthetic", "tables": {"ir_model_data": {"count": 0}}}
        self.assertIsNone(extract.project_private_inventory(unscoped)["out_of_scope"]["nontechnical_xmlids"])
        unscoped["out_of_scope"] = {"nontechnical_xmlids": None}
        self.assertIsNone(extract.project_private_inventory(unscoped)["out_of_scope"]["nontechnical_xmlids"])
        for count in (None, 9, 0):
            source = inventory(menus=[{"key": "menu", "references": [{"kind": "xmlid", "name": "synthetic.absent"}]}])
            source["out_of_scope"] = {"nontechnical_xmlids": count}
            findings = census.collect_static(source, target())["components"][0]["findings"]
            codes = {item["code"] for item in findings}
            self.assertEqual("xmlid-scope-unresolved" in codes, count != 0)
            self.assertEqual("missing-xmlid" in codes, count == 0)
        selected = target()
        selected["nontechnical_xmlids"] = None
        selected["blocked_modules"] = ["blocked"]
        selected["retained_xmlids"] = {"technical.missing"}
        self.assertEqual(census.xmlid_findings(selected, "blocked.reference")[0]["state"], "dependency-blocked")
        self.assertEqual(census.xmlid_findings(selected, "technical.missing")[0]["code"], "retained-xmlid-native-unavailable")

    def test_unqualified_stock_keeps_old_structural_findings_and_distinct_disposition(self) -> None:
        selected = target()
        selected["xmlids"]["base.stock"] = {"model": "ir.ui.view"}
        row = view("stock", '<form attrs="{}"><field name="gone"/></form>', xmlids=["base.stock"], noupdate=False)
        report = census.collect_static(inventory(views=[row]), selected)
        codes = {item["code"] for item in report["components"][0]["findings"]}
        self.assertTrue({"stock-upgrade-disposition-required", "missing-field", "obsolete-view-attribute"} <= codes)
        self.assertNotIn("static-only", report["coverage"]["views"])
        with self.assertRaises(native.ProbeUnreached) as error:
            native.load_view_family(ViewEnvironment({"base.stock": NativeView(5)}), [row], "en_US")
        self.assertEqual(error.exception.code, "stock-upgrade-disposition-required")

    def test_translation_fallback_uses_unchanged_base_and_keeps_withheld_languages_unreached(self) -> None:
        root = view("root", "<form/>")
        root["architectures"]["fr_FR"] = "<form/>"
        extension = view("extension", "<data/>", parent="root", mode="extension")
        original = copy.deepcopy(extension)
        env = ViewEnvironment(languages={"en_US", "fr_FR"})
        native.load_view_family(env, [root, extension], "fr_FR")
        self.assertEqual(env.created[1].values["arch"], extension["architectures"]["en_US"])
        self.assertEqual(extension, original)
        for values in (
            {"changed_languages": ["fr_FR"]},
            {"changed_languages": ["en_US"]},
            {"withheld": ["architecture"]},
            {"architectures": {"en_US": "<data/>", "fr_FR": None}},
        ):
            with self.assertRaises(native.ProbeUnreached) as error:
                native.view_architecture({**extension, **values}, "fr_FR")
            self.assertEqual(error.exception.code, "view-architecture-unavailable")

    def test_field_domains_use_relation_and_unknown_expression_shapes_stay_unvalidated(self) -> None:
        selected = target()
        selected["models"]["res.users"]["fields"]["x_relation"] = {"relation": "res.partner"}
        field = {
            "key": "manual.relation",
            "model": "res.users",
            "name": "x_relation",
            "ttype": "many2one",
            "state": "manual",
            "relation": "res.partner",
            "domain": "[('gone', '=', False)]",
            "context": "{'default_name': False}",
        }
        report = census.collect_static(inventory(fields=[field]), selected)
        self.assertEqual(
            {item["reference"] for item in report["components"][0]["findings"] if item["code"] == "missing-field"},
            {"res.partner.gone"},
        )
        field["domain"] = "5"
        report = census.collect_static(inventory(fields=[field]), selected)
        self.assertFalse(report["components"][0]["checked"])
        self.assertIn("expression-shape-unvalidated", {item["code"] for item in report["components"][0]["findings"]})
        self.assertFalse(native.manual_supported(field))
        field["domain"] = "[('name', '=', False)]"
        self.assertTrue(native.manual_supported(field))

    def test_partial_automation_keeps_scheduled_action_links_and_each_source_total(self) -> None:
        extracted: dict[str, Any] = {
            "binding": "synthetic",
            "tables": {
                "base_automation": {"count": None, "gap": "table-unavailable"},
                "ir_cron": {
                    "count": 2,
                    "rows": [
                        {"id": 1, "ir_actions_server_id": 4, "active": True, "priority": 5},
                        {"id": 2, "ir_actions_server_id": 5, "active": False, "priority": 9},
                    ],
                },
                "ir_model_data": {
                    "count": 1,
                    "rows": [{"module": "base", "name": "technical_server", "model": "ir.actions.server", "res_id": 4}],
                },
            },
        }
        source = extract.project_private_inventory(extracted)
        selected = target()
        selected["xmlids"]["base.technical_server"] = {"model": "ir.actions.server"}
        report = census.collect_static(source, selected)
        self.assertEqual(report["automation_totals"], {"base_automation": None, "ir_cron": 2})
        unavailable = native.unreached_report(source, {"artifact": {"synthetic": True}})
        self.assertEqual(unavailable["automation_totals"], report["automation_totals"])
        self.assertEqual(unavailable["schema_gaps"], source["schema_gaps"])
        coverage = report["coverage"]["automation"]
        self.assertIsNone(coverage["total"])
        self.assertIsNone(coverage["withheld"])
        self.assertEqual(coverage["inventoried"], len(source["records"]["automation"]))
        self.assertEqual(coverage["gap"], "source-total-missing")
        rows = source["records"]["automation"]
        self.assertEqual(rows[0]["references"], [{"kind": "xmlid", "name": "base.technical_server"}])
        self.assertEqual(rows[1]["source_action"], "action:5")
        self.assertIn(
            "unsupported-reference",
            {item["code"] for row in report["components"] if row["bucket"] == "automation" for item in row["findings"]},
        )
        extracted["tables"]["base_automation"]["count"] = 0
        complete_counts = extract.project_private_inventory(extracted)
        self.assertEqual(complete_counts["totals"]["automation"], len(rows))

    def test_probe_unreached_causes_keep_their_specific_codes(self) -> None:
        cases = [
            (
                lambda: native.load_view_family(ViewEnvironment(), [view("orphan", "<data/>", parent="absent")], "en_US"),
                "retained-parent-unavailable",
            ),
            (
                lambda: native.load_view_family(ViewEnvironment(), [view("website", "<form/>", website_scope="synthetic")], "en_US"),
                "website-variant-unbound",
            ),
            (
                lambda: native.load_action(ViewEnvironment(), {"key": "action", "type": "ir.actions.act_window", "res_id": 5}),
                "action-record-data-unavailable",
            ),
            (
                lambda: native.load_action(
                    ViewEnvironment(), {"key": "action", "type": "ir.actions.act_window", "view_references": ["absent"]}
                ),
                "action-view-identity-unavailable",
            ),
        ]
        for operation, code in cases:
            with self.assertRaises(native.ProbeUnreached) as error:
                operation()
            self.assertEqual(error.exception.code, code)


if __name__ == "__main__":
    unittest.main()
