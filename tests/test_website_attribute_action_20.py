from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from test_core_references_20 import CoreReferenceFixture, bridge
from test_hr_org_chart_action_20 import WindowActionCursor


class WebsiteAttributeActionTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = WindowActionCursor(self.connection)
        self.name = bridge.WEBSITE_ATTRIBUTE_ACTION
        self.native = {
            "name": "Attribute categories",
            "res_model": "product.attribute.category",
            "path": "attribute-categories",
            "view_mode": "list",
        }
        self.help = "<p>Organize product attributes.</p>"
        root = ElementTree.Element("odoo")
        record = ElementTree.SubElement(root, "record", id=self.name, model="ir.actions.act_window")
        for name, value in self.native.items():
            ElementTree.SubElement(record, "field", name=name).text = value
        help_field = ElementTree.SubElement(record, "field", name="help", type="html")
        help_field.extend(ElementTree.fromstring("<div>" + self.help + "</div>"))
        self.target = Path(self.directory.name) / "category.xml"
        ElementTree.ElementTree(root).write(self.target)
        patch.dict(vars(bridge), {"WEBSITE_ATTRIBUTE_DATA_FILE": self.target}).start()
        self.connection.executescript(
            "INSERT INTO ir_module_module VALUES ('website_sale_comparison','installed','19.0.1.0');"
            "INSERT INTO ir_module_module VALUES ('website_sale','installed','19.0.1.0');"
            "CREATE TABLE ir_act_window (id INTEGER PRIMARY KEY, name TEXT, type TEXT, res_model TEXT, path TEXT UNIQUE, "
            "view_mode TEXT, help TEXT, context TEXT, write_uid INTEGER);"
            "CREATE TABLE other_actions (storage TEXT, id INTEGER, path TEXT);"
            "CREATE VIEW ir_actions AS SELECT 'ir_act_window' AS storage,id,path FROM ir_act_window UNION ALL SELECT * FROM other_actions;"
            "CREATE TABLE action_links (action INTEGER REFERENCES ir_act_window(id), xmlid TEXT, url TEXT);"
        )
        self.action = self.connection.execute(
            "INSERT INTO ir_act_window(name,type,res_model,path,view_mode,help,context,write_uid) "
            "VALUES (?,'ir.actions.act_window',?,?,?,?,?,59)",
            (
                json.dumps({"en_US": self.native["name"]}),
                self.native["res_model"],
                self.native["path"],
                self.native["view_mode"],
                json.dumps({"en_US": self.help}),
                "{'retained_business_context': True}",
            ),
        ).lastrowid
        assert self.action is not None
        self.xmlid("website_sale_comparison", self.name, "ir.actions.act_window", self.action, False)
        self.xmlid("custom", "retained_category", "ir.actions.act_window", self.action, True)
        self.connection.execute(
            "INSERT INTO action_links VALUES (?,?,?)",
            (self.action, f"website_sale_comparison.{self.name}", "/odoo/" + self.native["path"]),
        )

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("ir_act_window", "other_actions", "action_links")
        }

    def native_load(self) -> None:
        target = bridge._reference(self.cursor, "website_sale", self.name)
        if target is None:
            self.connection.execute("INSERT INTO ir_act_window(path) VALUES (?)", (self.native["path"],))
            return
        self.assertEqual(target.record_id, self.action)
        self.assertFalse(target.noupdate)

    def test_native_loading_reuses_action_preserves_links_custom_alias_and_replays(self) -> None:
        before = self.snapshot()
        self.apply()
        self.native_load()
        prepared = self.snapshot()
        self.assertEqual(prepared["ir_act_window"], before["ir_act_window"])
        self.assertEqual(prepared["action_links"], before["action_links"])
        source = bridge._reference(self.cursor, "website_sale_comparison", self.name)
        self.assertEqual(source.record_id, self.action)
        self.assertTrue(source.noupdate)
        self.apply()
        self.assertEqual(self.snapshot(), prepared)

    def test_conflicting_destination_changed_native_values_and_protected_source_keep_all_state(self) -> None:
        changes = (
            ("UPDATE ir_act_window SET name=?", (json.dumps({"en_US": "My categories"}),)),
            ("UPDATE ir_act_window SET help=?", (json.dumps({"en_US": "<p>Keep this help.</p>"}),)),
            ("UPDATE ir_act_window SET res_model='custom.category'", ()),
            ("UPDATE ir_act_window SET view_mode='form'", ()),
            ("UPDATE ir_act_window SET path='retained-path'", ()),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='website_sale_comparison'", ()),
            ("UPDATE ir_model_data SET model='ir.actions.server' WHERE module='website_sale_comparison'", ()),
            ("UPDATE ir_model_data SET res_id=-1 WHERE module='website_sale_comparison'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('website_sale',?,'ir.actions.act_window',999,0)",
                (self.name,),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('website_sale',?,'ir.actions.server',?,0)",
                (self.name, self.action),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('website_sale',?,'ir.actions.act_window',?,1)",
                (self.name, self.action),
            ),
        )
        for query, values in changes:
            with self.subTest(query=query):
                self.connection.execute("SAVEPOINT modified")
                self.connection.execute(query, values)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO modified")
                self.connection.execute("RELEASE modified")

    def test_same_path_in_another_action_type_is_not_a_window_collision(self) -> None:
        self.connection.execute("INSERT INTO other_actions VALUES ('ir_act_server',999,?)", (self.native["path"],))
        self.apply()
        self.native_load()

    def test_native_definition_change_is_reported_before_effects(self) -> None:
        document = ElementTree.parse(self.target)
        record = document.find("record")
        assert record is not None
        ElementTree.SubElement(record, "field", name="domain").text = "[]"
        document.write(self.target)
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_existing_translations_and_uninstalled_source_are_preserved(self) -> None:
        self.connection.execute(
            "UPDATE ir_act_window SET name=?,help=?",
            (
                json.dumps({"en_US": self.native["name"], "fr_FR": "Mes catégories"}),
                json.dumps({"en_US": self.help, "fr_FR": "<p>Texte conservé.</p>"}),
            ),
        )
        before = self.snapshot()["ir_act_window"]
        self.connection.execute("UPDATE ir_module_module SET state='uninstalled' WHERE name='website_sale_comparison'")
        self.apply()
        self.assertIsNone(bridge._reference(self.cursor, "website_sale", self.name))
        self.connection.execute("UPDATE ir_module_module SET state='installed' WHERE name='website_sale_comparison'")
        self.apply()
        self.native_load()
        self.assertEqual(self.snapshot()["ir_act_window"], before)
