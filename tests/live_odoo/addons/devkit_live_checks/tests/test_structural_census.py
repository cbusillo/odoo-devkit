from __future__ import annotations

import inspect
import json
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import odoo.addons
import odoo.addons.base.models.ir_actions as actions
import odoo.addons.base.models.ir_ui_view as views
import odoo.modules.loading as loading
import odoo.orm.environments as environments
import odoo.orm.registry as registry_source
import odoo.sql_db as sql_db
import odoo.tools.convert as convert
import odoo_structural_census as census
import odoo_structural_extract as extract
import odoo_structural_native as native
from odoo import SUPERUSER_ID, api, release
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import config
from odoo_release_inventory import build_inventory, classify_files, module_graph
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
        fixture = Path("/opt/extra_addons/ci_census_good/actions.xml")
        paths = [
            Path(inspect.getfile(module)) for module in (registry_source, loading, convert, views, actions, environments, sql_db)
        ] + [fixture.parent / "__manifest__.py", fixture]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        evidence = Path(temporary.name)
        files = classify_files({path.resolve().as_posix().lstrip("/"): path.read_bytes() for path in paths})[0]
        (evidence / "base-runtime-inventory.json").write_text(json.dumps(files))
        self.declaration = build_inventory(
            {
                "addon_paths": list(map(str, self.roots)),
                "sources": [
                    {
                        "input_name": "base:runtime",
                        "repository": "fixture/native",
                        "commit": "synthetic-fixture",
                        "roots": list(map(str, self.roots)),
                        "files": files,
                    }
                ],
            }
        )
        original_declared_files = native.declared_files
        binding_patch = patch.object(
            native,
            "declared_files",
            side_effect=lambda declaration, platform, roots: original_declared_files(
                declaration, platform, roots, evidence=evidence
            ),
        )
        binding_patch.start()
        self.addCleanup(binding_patch.stop)
        self.artifact: dict[str, Any] = {
            "artifact_id": "synthetic-fixture",
            "release_compatibility": self.declaration,
            "odoo_install_modules": ["ci_census_good", "ci_census_dependent"],
        }
        self.candidate: dict[str, Any] = {
            "artifact": self.artifact,
            "platform": "linux/amd64",
            "intended_modules": ["ci_census_good", "ci_census_dependent"],
            "migration_sources": [str(fixture)],
        }

    def _collect_native(self, source: dict[str, Any], *, intended_modules: list[str] | None = None) -> dict[str, Any]:
        candidate = self.candidate if intended_modules is None else {**self.candidate, "intended_modules": intended_modules}
        with patch.dict(config.options, {"test_enable": False}):
            return native.collect_native(
                source, candidate, self.roots, declaration=self.declaration, deadline=time.monotonic() + 180
            )

    def _qualify_stock(self, row: dict[str, Any]) -> None:
        row["candidate_coverage"] = [
            {
                "candidate": census.digest(self.artifact),
                "record_sha256": census.digest(row),
                "source_sha256": native.verify_file(
                    Path(self.candidate["migration_sources"][0]),
                    native.declared_files(self.declaration, self.candidate["platform"], self.roots),
                ),
                "guard_receipt": "synthetic-root-stock-qualification",
                "code": "native20-stock-replacement",
                "reference": row["xmlids"][0],
            }
        ]

    def test_real_loader_failures_manual_reload_view_families_and_actions_collect_together(self) -> None:
        self.candidate["intended_modules"].append("ci_census_missing")
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
                view("hr.retained", '<hierarchy><field name="name"/></hierarchy>', type="form"),
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
                view("manual.blocked", '<form><field name="x_census_code"/></form>'),
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
        self.assertEqual(components["manual.blocked"]["native"], "unreached")
        self.assertTrue(any(row.get("module") == "ci_census_missing" and row["state"] == "missing" for row in report["bootstrap"]))
        self.assertTrue(any(item["code"] == "manual-baseline-unavailable" for item in components["manual.code"]["findings"]))

    def test_stock20_root_is_preserved_so_custom_extension_failure_is_not_masked(self) -> None:
        root = view(
            "stock.old",
            '<form><field name="name"/><field name="login"/></form>',
            xmlids=["ci_census_good.stock_view"],
            noupdate=False,
            arch_updated=True,
            customization_present=True,
        )
        self._qualify_stock(root)
        source = packet(
            views=[
                root,
                view(
                    "custom.extension",
                    '<xpath expr="//field[@name=\'login\']" position="after"><field name="name"/></xpath>',
                    parent="stock.old",
                    mode="extension",
                ),
            ]
        )
        report = self._collect_native(source, intended_modules=["ci_census_good"])
        components = {row["key"]: row for row in report["components"]}
        self.assertEqual(components["custom.extension"]["native"], "native-reproduced")
        self.assertTrue(any(item["code"] == "native20-stock-replacement" for item in components["stock.old"]["findings"]))

    def test_transaction_rollback_and_registry_cache_cannot_contaminate_next_family(self) -> None:
        session = native.DisposableCensus(time.monotonic() + 180)
        source = packet(
            fields=[{"key": "manual.safe", "model": "res.users", "name": "x_census_isolation", "state": "manual", "ttype": "char"}]
        )
        try:
            with patch.dict(config.options, {"test_enable": False}):
                registry = session.build({"base"}, module_graph(self.roots))
                prepare = native.prepare_manual
                committed = []

                def observe_committed_registry(declaration_env: Any, selected: dict[str, Any]) -> set[tuple[str, str]]:
                    blocked = prepare(declaration_env, selected)
                    declaration_env.cr.postcommit.add(lambda: committed.append(registry_source.Registry(session.baseline)))
                    return blocked

                with patch.object(native, "prepare_manual", side_effect=observe_committed_registry):
                    prepared = session.manual_baseline(registry, source)
                self.assertIs(prepared, committed[-1])
                self.assertIs(prepared, registry_source.Registry(session.baseline))
                with session.probe() as env:
                    created = env["ir.ui.view"].create(
                        {"name": "probe only", "model": "res.users", "arch": '<form><field name="name"/></form>'}
                    )
                    env["ir.model.data"].create(
                        {"module": "census_fixture", "name": "ephemeral", "model": "ir.ui.view", "res_id": created.id}
                    )
                    self.assertEqual(env.ref("census_fixture.ephemeral").id, created.id)
                    self.assertIn("x_census_isolation", env["res.users"]._fields)
                with session.probe() as env:
                    self.assertFalse(env.ref("census_fixture.ephemeral", raise_if_not_found=False))
                    self.assertFalse(env["ir.ui.view"].search([("name", "=", "probe only")]))
                    self.assertIn("x_census_isolation", env["res.users"]._fields)
        finally:
            session.close()
        self.assertFalse(session.owned)

    def test_reparented_stock_roots_probe_their_customizations_as_one_actual_family(self) -> None:
        roots = [
            view("old.first", "<form/>", xmlids=["ci_census_good.stock_view"], source_order=2),
            view("old.second", "<form/>", xmlids=["ci_census_good.reparented_stock_view"], source_order=1),
        ]
        for row in roots:
            self._qualify_stock(row)
        source = packet(
            views=[
                *roots,
                view(
                    "custom.first",
                    '<xpath expr="//field[@name=\'name\']" position="replace"/>',
                    parent="old.first",
                    mode="extension",
                    source_order=3,
                    priority=8,
                ),
                view(
                    "custom.second",
                    '<xpath expr="//field[@name=\'name\']" position="attributes"><attribute name="string">census</attribute></xpath>',
                    parent="old.second",
                    mode="extension",
                    source_order=4,
                ),
            ]
        )
        report = self._collect_native(source, intended_modules=["ci_census_good"])
        components = {row["key"]: row for row in report["components"]}
        self.assertEqual(components["custom.second"]["native"], "native-reproduced")
        self.assertEqual(components["custom.first"]["native"], "unreached")

    def test_extraction_reads_only_allowlisted_structure_and_does_not_allow_writes(self) -> None:
        session = native.DisposableCensus(time.monotonic() + 180)
        try:
            with patch.dict(config.options, {"test_enable": False}):
                registry = session.build({"base"}, module_graph(self.roots))
                excluded_name = "synthetic_nontechnical_marker"
                with registry.cursor() as cursor:
                    env = api.Environment(cursor, SUPERUSER_ID, {})
                    env["ir.model.data"].create(
                        {
                            "module": "census_fixture",
                            "name": excluded_name,
                            "model": "res.partner",
                            "res_id": env.ref("base.main_partner").id,
                        }
                    )
                    cursor.commit()
                with registry.cursor() as cursor:
                    exported = extract.extract_private_technical(cursor, "synthetic-owned-empty")
                    self.assertFalse(exported["screened"])
                    self.assertNotIn("res_partner", exported["tables"])
                    self.assertNotIn("name", exported["tables"]["ir_ui_view"]["rows"][0])
                    self.assertNotIn(excluded_name, {row["name"] for row in exported["tables"]["ir_model_data"]["rows"]})
                    self.assertGreater(exported["out_of_scope"]["nontechnical_xmlids"], 0)
                    with self.assertRaises(ReadOnlySqlTransaction), cursor.savepoint(flush=False):
                        cursor.execute("UPDATE ir_ui_view SET priority=priority")
        finally:
            session.close()
