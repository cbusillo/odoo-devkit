from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

from test_core_references_20 import CoreReferenceFixture, Cursor, bridge, patch_attr


class StockCursor(Cursor):
    def execute(self, query: str, parameters: tuple = ()) -> object:
        if query.startswith("LOCK TABLE"):
            return None
        return super().execute(query, parameters)


class StockProductViewTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = StockCursor(self.connection)
        patch_attr(bridge, "_stock_valuation_action_plan", lambda _cr: None).start()
        self.connection.executescript(
            "CREATE TABLE ir_ui_view (id INTEGER PRIMARY KEY, name TEXT, model TEXT, inherit_id INTEGER, "
            "type TEXT, mode TEXT, active BOOLEAN, priority INTEGER, arch_updated BOOLEAN, arch_fs TEXT, "
            "customize_show BOOLEAN, is_seo_optimized BOOLEAN, key TEXT, website_id INTEGER, theme_template_id INTEGER, "
            "arch_db JSON, arch_prev TEXT, write_uid INTEGER, extra_value TEXT);"
            "CREATE TABLE ir_ui_view_custom(ref_id INTEGER);"
            "CREATE TABLE ir_ui_view_group_rel(view_id INTEGER, group_id INTEGER);"
            "CREATE TABLE retained_view_links(view_id INTEGER REFERENCES ir_ui_view(id), value TEXT);"
            "CREATE TABLE ir_act_server(id INTEGER PRIMARY KEY);"
        )
        self.ancestor = self.connection.execute(
            "INSERT INTO ir_ui_view(model,type,active) VALUES ('product.template','form',1)"
        ).lastrowid
        self.action = self.connection.execute("INSERT INTO ir_act_server DEFAULT VALUES").lastrowid
        assert self.ancestor is not None and self.action is not None
        self.xmlid("product", "product_template_form_view", "ir.ui.view", self.ancestor, False)
        self.xmlid("stock", "action_open_routes", "ir.actions.server", self.action, False)
        self.views = {}
        self.native_files = {}
        for view in bridge.STOCK_VIEWS:
            self.connection.execute("INSERT INTO ir_module_module VALUES (?,'installed','19.0.1.0')", (view.module,))
            fields = self.stock_fields(view.module, view.source, "19")
            architecture = self.render(fields)
            identity = self.connection.execute(
                "INSERT INTO ir_ui_view(name,model,inherit_id,type,mode,active,priority,arch_updated,arch_fs,"
                "customize_show,is_seo_optimized,arch_db,arch_prev,write_uid) "
                "VALUES (?,'product.template',?,'form','extension',1,16,0,?,0,0,?,?,?)",
                (
                    fields["name"].text,
                    self.ancestor if view.module == "stock" else self.views["stock"],
                    f"{view.module}/views/{bridge.STOCK_PRODUCT_DATA_FILES[view.module].name}",
                    json.dumps({"en_US": architecture}),
                    architecture,
                    67,
                ),
            ).lastrowid
            assert identity is not None
            self.views[view.module] = identity
            self.xmlid(view.module, view.source, "ir.ui.view", identity, False)
            self.connection.execute("INSERT INTO retained_view_links VALUES (?, 'preserved')", (identity,))
            target = Path(self.directory.name) / view.module / bridge.STOCK_PRODUCT_DATA_FILES[view.module].name
            target.parent.mkdir()
            target.write_bytes((Path(__file__).parent / "fixtures/stock_product_views_20.xml").read_bytes())
            self.native_files[view.module] = target
        patch_attr(bridge, "STOCK_PRODUCT_DATA_FILES", self.native_files).start()

    @staticmethod
    def stock_fields(module: str, name: str, series: str) -> dict[str, ElementTree.Element]:
        return bridge._hr_native_fields(
            Path(bridge.__file__).parent / "legacy" / module / f"product_views_{series}.xml", name, "ir.ui.view"
        )

    def render(self, fields: dict[str, ElementTree.Element]) -> str:
        source = ElementTree.tostring(fields["arch"], encoding="unicode").replace("%(action_open_routes)d", str(self.action))
        parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
        return bridge.runpy.run_path(str(Path(bridge.__file__).with_name("mail_view_20.py")))["_render_architecture"](
            ElementTree.fromstring(source, parser=parser)
        )

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("ir_ui_view", "ir_ui_view_custom", "ir_ui_view_group_rel", "retained_view_links", "ir_act_server")
        }

    def assert_refuses_without_effects(self) -> None:
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_new_native_aliases_resolve_original_parent_and_child_without_view_writes_and_reentry_is_noop(self) -> None:
        before = self.snapshot()
        self.apply()
        after = self.snapshot()
        for table in ("ir_ui_view", "retained_view_links", "ir_act_server"):
            self.assertEqual(after[table], before[table])
        for view in bridge.STOCK_VIEWS:
            expected = [("ir.ui.view", self.views[view.module], 0)]
            self.assertEqual(
                self.connection.execute(
                    "SELECT model,res_id,noupdate FROM ir_model_data WHERE module=? AND name=?", (view.module, view.target)
                ).fetchall(),
                expected,
            )
        self.apply()
        self.assertEqual(self.snapshot(), after)

    def test_each_supported_current_and_independent_18_history_survives_unchanged(self) -> None:
        parent = bridge.STOCK_VIEWS[0]
        child = bridge.STOCK_VIEWS[1]
        for current in ("19", "19_early"):
            with self.subTest(current=current):
                self.connection.execute("SAVEPOINT variant")
                try:
                    self.connection.execute(
                        "UPDATE ir_ui_view SET arch_db=?,arch_prev=? WHERE id=?",
                        (
                            json.dumps({"en_US": self.render(self.stock_fields(parent.module, parent.source, current))}),
                            self.render(self.stock_fields(parent.module, parent.source, "18")),
                            self.views[parent.module],
                        ),
                    )
                    self.connection.execute(
                        "UPDATE ir_ui_view SET arch_prev=? WHERE id=?",
                        (self.render(self.stock_fields(child.module, child.source, "18")), self.views[child.module]),
                    )
                    before = self.snapshot()["ir_ui_view"]
                    self.apply()
                    self.assertEqual(self.snapshot()["ir_ui_view"], before)
                finally:
                    self.connection.execute("ROLLBACK TO variant")
                    self.connection.execute("RELEASE variant")

    def test_optional_stock_account_and_delivery_do_not_require_uninstalled_modules(self) -> None:
        for module in ("stock_account", "stock_delivery"):
            self.connection.execute("DELETE FROM retained_view_links WHERE view_id=?", (self.views[module],))
            self.connection.execute("DELETE FROM ir_model_data WHERE module=?", (module,))
            self.connection.execute("DELETE FROM ir_ui_view WHERE id=?", (self.views[module],))
            self.connection.execute("DELETE FROM ir_module_module WHERE name=?", (module,))
        self.apply()
        parent = bridge.STOCK_VIEWS[0]
        self.assertEqual(bridge._reference(self.cursor, parent.module, parent.target).record_id, self.views[parent.module])

    def test_either_target_collision_or_protection_refuses_before_any_alias_effect(self) -> None:
        for view in bridge.STOCK_VIEWS[:2]:
            for model, identity, protection in (
                ("wrong.model", self.views[view.module], False),
                ("ir.ui.view", self.ancestor, False),
                ("ir.ui.view", self.views[view.module], True),
            ):
                with self.subTest(module=view.module, model=model, protection=protection):
                    self.connection.execute("SAVEPOINT target")
                    try:
                        self.xmlid(view.module, view.target, model, identity, protection)
                        self.assert_refuses_without_effects()
                    finally:
                        self.connection.execute("ROLLBACK TO target")
                        self.connection.execute("RELEASE target")

    def test_custom_translated_protected_and_rebound_source_states_refuse_before_effects(self) -> None:
        changes = (
            ("UPDATE ir_ui_view SET arch_db=? WHERE id=?", json.dumps({"en_US": "<data><field name='custom'/></data>"})),
            ("UPDATE ir_ui_view SET arch_db=? WHERE id=?", json.dumps({"en_US": "<data/>", "fr_FR": "<data/>"})),
            ("UPDATE ir_ui_view SET arch_prev=? WHERE id=?", "<data><field name='custom_history'/></data>"),
            ("UPDATE ir_ui_view SET arch_prev=? WHERE id=?", "invalid XML"),
            ("UPDATE ir_ui_view SET active=? WHERE id=?", False),
            ("UPDATE ir_ui_view SET arch_updated=? WHERE id=?", True),
            ("UPDATE ir_ui_view SET extra_value=? WHERE id=?", "custom value"),
            ("UPDATE ir_ui_view SET inherit_id=? WHERE id=?", self.ancestor),
            ("UPDATE ir_model_data SET noupdate=? WHERE model='ir.ui.view' AND res_id=?", True),
            ("INSERT INTO ir_ui_view_custom(ref_id) VALUES (?)", None),
            ("INSERT INTO ir_ui_view_group_rel(view_id) VALUES (?)", None),
        )
        for query, value in changes:
            with self.subTest(change=query):
                self.connection.execute("SAVEPOINT customized")
                try:
                    identity = self.views["stock_account"]
                    self.connection.execute(query, (identity,) if value is None else (value, identity))
                    self.assert_refuses_without_effects()
                finally:
                    self.connection.execute("ROLLBACK TO customized")
                    self.connection.execute("RELEASE customized")

    def test_extra_alias_and_outside_descendant_refuse_without_effects(self) -> None:
        for kind in ("alias", "descendant"):
            with self.subTest(kind=kind):
                self.connection.execute("SAVEPOINT outside")
                try:
                    if kind == "alias":
                        self.xmlid("custom", "preserved_view", "ir.ui.view", self.views["stock"], False)
                    else:
                        self.connection.execute("INSERT INTO ir_ui_view(inherit_id) VALUES (?)", (self.views["stock_account"],))
                    self.assert_refuses_without_effects()
                finally:
                    self.connection.execute("ROLLBACK TO outside")
                    self.connection.execute("RELEASE outside")

    def test_native_definition_parent_change_and_missing_routes_action_refuse(self) -> None:
        target = self.native_files["stock_account"]
        original = target.read_bytes()
        document = ElementTree.parse(target)
        parent = document.find(".//record[@id='product_template_form_view_stock_account']/field[@name='inherit_id']")
        assert parent is not None
        parent.set("ref", "custom.changed_parent")
        document.write(target)
        self.assert_refuses_without_effects()
        target.write_bytes(original)
        self.connection.execute("DELETE FROM ir_act_server WHERE id=?", (self.action,))
        self.assert_refuses_without_effects()

    def test_late_global_admission_failure_keeps_both_aliases_unbound(self) -> None:
        before = self.snapshot()
        with patch_attr(bridge, "_admit_mail", Mock(side_effect=RuntimeError("unqualified Mail"))):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.snapshot(), before)
