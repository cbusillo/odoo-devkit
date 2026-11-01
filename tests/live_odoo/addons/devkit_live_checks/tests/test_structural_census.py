from __future__ import annotations

import hashlib
import inspect
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import odoo.addons
import odoo.addons.base.models.ir_actions as actions
import odoo.addons.base.models.ir_ui_view as views
import odoo.modules.loading as loading
import odoo.orm.registry as registry_source
import odoo.tools.convert as convert
import odoo_structural_census as census
import odoo_structural_extract as extract
import odoo_structural_native as native
from odoo import release
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import config
from odoo_release_inventory import module_graph
from psycopg2.errors import ReadOnlySqlTransaction


def packet(**buckets: list[dict[str, Any]]) -> dict[str, Any]:
    records = {bucket: [] for bucket in census.BUCKETS} | buckets
    return {"binding": "synthetic-native-source", "records": records, "totals": {key: len(rows) for key, rows in records.items()}}


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


@tagged("post_install", "-at_install")
class TestStructuralCensus(TransactionCase):
    def setUp(self) -> None:
        super().setUp()
        if release.version_info[0] != 20:
            self.skipTest("Native structural census targets Community20")
        self.roots = [Path(path) for path in odoo.addons.__path__]
        graph = module_graph(self.roots)
        self.declaration = {
            "complete": True,
            "modules": [{"name": key, "depends": sorted(value)} for key, value in graph.items()],
            "sources": [],
        }
        self.candidate = {
            "binding": "synthetic-native-target",
            "platform": "linux/amd64",
            "release_compatibility": self.declaration,
            "intended_modules": ["ci_census_good", "ci_census_dependent"],
            "migration_sources": {},
            "native_sources": {
                key: hashlib.sha256(Path(inspect.getfile(module)).read_bytes()).hexdigest()
                for key, module in zip(native.NATIVE_ROLES, (registry_source, loading, convert, views, actions), strict=True)
            },
        }

    def test_real_loader_failures_manual_reload_view_families_and_actions_collect_together(self) -> None:
        source = packet(
            modules=[{"key": name} for name in self.candidate["intended_modules"]],
            fields=[
                {"key": "manual.safe", "model": "res.users", "name": "x_census_baseline", "state": "manual", "ttype": "char"},
                {
                    "key": "manual.code",
                    "model": "res.users",
                    "name": "x_census_code",
                    "state": "manual",
                    "ttype": "char",
                    "compute": "raise AssertionError('stored compute executed')",
                },
            ],
            views=[
                view(
                    "calendar.retained", '<form><field name="calendar_default_privacy"/><field name="census_second_missing"/></form>'
                ),
                view("hr.retained", '<form><field name="employee_id"/></form>'),
                view("family.root", '<form><field name="name"/></form>', source_order=1),
                view(
                    "family.broken",
                    '<xpath expr="//field[@name=\'absent\']" position="after"><field name="name"/></xpath>',
                    parent="family.root",
                    mode="extension",
                    source_order=2,
                ),
                view("family.descendant", "<data/>", parent="family.broken", mode="extension", source_order=3),
                view("later.valid", '<form><field name="x_census_baseline"/></form>'),
            ],
            actions=[
                {
                    "key": "mail.legacy_action",
                    "type": "ir.actions.act_window",
                    "model": "res.users",
                    "path": "census-canonical",
                    "view_mode": "list,form",
                },
                {
                    "key": "stored.server",
                    "type": "ir.actions.server",
                    "model": "res.users",
                    "code": "raise AssertionError('stored server action executed')",
                },
            ],
        )
        with patch.dict(config.options, {"test_enable": False}):
            report = native.collect_native(
                source, self.candidate, self.roots, declaration=self.declaration, deadline=time.monotonic() + 300
            )
        self.assertFalse(report["clear"])
        self.assertTrue(
            any(row.get("module") == "ci_census_bad" and row["state"] == "native-reproduced" for row in report["bootstrap"])
        )
        self.assertTrue(
            any(row.get("module") == "ci_census_dependent" and row["state"] == "dependency-blocked" for row in report["bootstrap"])
        )
        components = {row["key"]: row for row in report["components"]}
        self.assertEqual(components["calendar.retained"]["native"], "native-reproduced")
        self.assertEqual(sum(item["code"] == "missing-field" for item in components["calendar.retained"]["findings"]), 2)
        self.assertEqual(components["hr.retained"]["native"], "native-reproduced")
        self.assertEqual(components["family.broken"]["native"], "native-reproduced")
        self.assertEqual(components["family.descendant"]["native"], "unreached")
        self.assertEqual(components["later.valid"]["native"], "checked")
        self.assertTrue(any(item["state"] == "conditional" for item in components["later.valid"]["findings"]))
        self.assertEqual(components["mail.legacy_action"]["native"], "native-reproduced")
        self.assertEqual(components["stored.server"]["native"], "unreached")
        self.assertTrue(any(item["code"] == "manual-baseline-unavailable" for item in components["manual.code"]["findings"]))

    def test_clone_rollback_and_registry_cache_cannot_contaminate_next_family(self) -> None:
        session = native.DisposableCensus(time.monotonic() + 180)
        source = packet(
            fields=[{"key": "manual.safe", "model": "res.users", "name": "x_census_isolation", "state": "manual", "ttype": "char"}]
        )
        try:
            with patch.dict(config.options, {"test_enable": False}):
                session.build({"base"}, source, module_graph(self.roots))
                with session.probe() as env:
                    env["ir.ui.view"].create(
                        {"name": "probe only", "model": "res.users", "arch": '<form><field name="name"/></form>'}
                    )
                    self.assertIn("x_census_isolation", env["res.users"]._fields)
                with session.probe() as env:
                    self.assertFalse(env["ir.ui.view"].search([("name", "=", "probe only")]))
                    self.assertIn("x_census_isolation", env["res.users"]._fields)
        finally:
            session.close()
        self.assertFalse(session.owned)

    def test_extraction_reads_only_allowlisted_structure_and_does_not_allow_writes(self) -> None:
        session = native.DisposableCensus(time.monotonic() + 180)
        try:
            with patch.dict(config.options, {"test_enable": False}):
                registry = session.build({"base"}, packet(), module_graph(self.roots))
                with registry.cursor() as cursor:
                    exported = extract.extract_private_technical(cursor, "synthetic-owned-empty")
                    self.assertFalse(exported["screened"])
                    self.assertNotIn("res_partner", exported["tables"])
                    self.assertNotIn("name", exported["tables"]["ir_ui_view"]["rows"][0])
                    with self.assertRaises(ReadOnlySqlTransaction), cursor.savepoint(flush=False):
                        cursor.execute("UPDATE ir_ui_view SET priority=priority")
        finally:
            session.close()
