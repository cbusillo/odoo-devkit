from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
                view("hr.legacy", '<form><field name="employee_id"/></form>'),
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
        self.assertEqual(report["coverage"]["views"]["static-only"], 3)

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
                view("attribute", '<data><attribute name="domain">[("gone", "=", False)]</attribute></data>'),
                view("button", '<form><button name="1234" type="action"/></form>'),
            ]
        )
        report = census.collect_static(packet, target())
        codes = {item["code"] for row in report["components"] for item in row["findings"]}
        self.assertTrue({"template-expression-unexecuted", "missing-field", "numeric-action-binding-unavailable"} <= codes)

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
            files = {source.as_posix().lstrip("/"): hashlib.sha256(source.read_bytes()).hexdigest()}
            native.verify_file(source, files)
            source.write_text("changed source")
            with self.assertRaises(ValueError):
                native.verify_file(source, files)
            session = native.DisposableCensus(time.monotonic() + 60, output=root / "report.json")
            session.report = {"components": [{"key": "already-collected"}]}
            session.checkpoint()
            with self.assertRaises(ValueError):
                session.drop("foreign")
            import json

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


if __name__ == "__main__":
    unittest.main()
