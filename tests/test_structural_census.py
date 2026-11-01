from __future__ import annotations

import copy
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "docker/scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import odoo_structural_census as census
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
        self.assertFalse(report["clear"])

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
        self.assertFalse(report["clear"])

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
        class Session:
            conditional = False

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
        self.assertTrue(native.manual_supported({"ttype": "char"}))
        with self.assertRaises(TimeoutError):
            native.DisposableCensus(0).allocate()
        with self.assertRaises(ValueError):
            native.DisposableCensus(0).drop("foreign-database")

    def test_unreached_variant_cannot_be_overwritten_by_later_success(self) -> None:
        class Session:
            conditional = False

            @staticmethod
            @contextmanager
            def probe() -> Any:
                yield object()

        component = [{"key": "translated", "native": "unreached", "findings": []}]

        def withheld(_env: Any) -> None:
            raise native.ProbeUnreached("withheld translation")

        with self.assertLogs("odoo_structural_native", level="ERROR"):
            native.run_probe(Session(), component, withheld, label="fr_FR")
        native.run_probe(Session(), component, lambda _env: None, label="en_US")
        self.assertEqual(component[0]["native"], "unreached")


if __name__ == "__main__":
    unittest.main()
