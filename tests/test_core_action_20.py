from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from xml.etree import ElementTree

from test_core_references_20 import CoreReferenceFixture, Cursor, bridge


class ActionCursor(Cursor):
    def execute(self, query: str, parameters: tuple = ()) -> Any:
        return super().execute(query.replace("tableoid::regclass::text", "storage").replace("%%", "%"), parameters)

    def fetchall(self) -> list[tuple]:
        return [
            tuple(
                json.loads(value) if column[0] == "name" and isinstance(value, str) and value.startswith("{") else value
                for column, value in zip(self.cursor.description, row, strict=True)
            )
            for row in super().fetchall()
        ]


class StockValuationActionTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = ActionCursor(self.connection)
        self.action_name = "action_report_stock_valuation"
        self.old_model = "stock_account.stock.valuation.report"
        self.new_model = "account.stock.valuation.report"
        self.native = {
            "name": "Inventory Valuation",
            "tag": "stock_valuation_report",
            "res_model": self.new_model,
            "path": "stock-valuation-closing",
            "context": repr({"model": self.new_model}),
        }
        target = Path(self.directory.name) / "action.xml"
        root = ElementTree.Element("odoo")
        record = ElementTree.SubElement(root, "record", id=self.action_name, model="ir.actions.client")
        for name, value in self.native.items():
            ElementTree.SubElement(record, "field", name=name).text = value
        ElementTree.ElementTree(root).write(target)
        patch.dict(vars(bridge), {"STOCK_VALUATION_DATA_FILE": target}).start()
        self.connection.executescript(
            "INSERT INTO ir_module_module VALUES ('stock_account','installed','19.0.1.0');"
            "CREATE TABLE ir_act_client (id INTEGER PRIMARY KEY, name TEXT, type TEXT, tag TEXT, res_model TEXT, context TEXT, "
            "path TEXT UNIQUE, target TEXT, params_store TEXT, help TEXT, binding_model_id INTEGER, binding_type TEXT, "
            "binding_view_types TEXT, create_uid INTEGER, write_uid INTEGER);"
            "CREATE TABLE other_actions (storage TEXT, id INTEGER, path TEXT);"
            "CREATE VIEW ir_actions AS SELECT 'ir_act_client' AS storage,id,path FROM ir_act_client UNION ALL SELECT * FROM other_actions;"
            "CREATE TABLE action_links (action INTEGER REFERENCES ir_act_client(id), xmlid TEXT, url TEXT);"
        )
        self.action = self.connection.execute(
            "INSERT INTO ir_act_client(name,type,tag,res_model,context,path,target,binding_type,binding_view_types,create_uid,write_uid) "
            "VALUES (?,'ir.actions.client',?,?,?,?,'current','action','list,form',43,59)",
            (
                json.dumps({"en_US": self.native["name"]}),
                self.native["tag"],
                self.old_model,
                repr({"model": self.old_model}),
                self.native["path"],
            ),
        ).lastrowid
        assert self.action is not None
        self.xmlid("stock_account", self.action_name, "ir.actions.client", self.action, False)
        self.connection.execute(
            "INSERT INTO action_links VALUES (?,?,?)",
            (self.action, f"stock_account.{self.action_name}", f"/odoo/{self.native['path']}"),
        )

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("ir_act_client", "other_actions", "action_links")
        }

    def native_load(self) -> None:
        target = bridge._reference(self.cursor, "account", self.action_name)
        if target is None:
            self.connection.execute("INSERT INTO ir_act_client(path) VALUES (?)", (self.native["path"],))
            return
        self.assertFalse(target.noupdate)
        self.connection.execute(
            "UPDATE ir_act_client SET res_model=?,context=? WHERE id=?",
            (self.native["res_model"], self.native["context"], target.record_id),
        )

    def native_cleanup(self) -> None:
        loaded = f"account.{self.action_name}"
        for alias_id, module, name, model, record in self.connection.execute(
            "SELECT id,module,name,model,res_id FROM ir_model_data WHERE module IN ('account','stock_account') "
            "AND COALESCE(noupdate,0)!=1 ORDER BY id DESC"
        ).fetchall():
            if f"{module}.{name}" == loaded:
                continue
            others = self.connection.execute(
                "SELECT 1 FROM ir_model_data WHERE model=? AND res_id=? AND id!=?", (model, record, alias_id)
            ).fetchall()
            if not others:
                self.connection.execute("DELETE FROM ir_act_client WHERE id=?", (record,))
            self.connection.execute("DELETE FROM ir_model_data WHERE id=?", (alias_id,))

    def test_native_load_and_cleanup_preserve_action_identity_links_and_replay(self) -> None:
        for model in ("ir.actions.act_window.view", "ir.actions.todo"):
            self.xmlid("unrelated", model.replace(".", "_"), model, self.action, False)
        before = self.snapshot()
        self.apply()
        prepared = self.snapshot()
        self.assertEqual(prepared["ir_act_client"], before["ir_act_client"])
        self.assertEqual(prepared["action_links"], before["action_links"])
        self.apply()
        self.assertEqual(self.snapshot(), prepared)
        self.native_load()
        self.native_cleanup()
        for module in ("stock_account", "account"):
            self.assertEqual(bridge._reference(self.cursor, module, self.action_name).record_id, self.action)
        action = self.connection.execute("SELECT id,path FROM ir_act_client").fetchone()
        self.assertEqual(action, (self.action, self.native["path"]))
        self.assertEqual(self.snapshot()["action_links"], before["action_links"])

    def test_custom_values_ownership_destinations_and_storage_refuse_before_all_effects(self) -> None:
        changes = (
            ("UPDATE ir_act_client SET name=?", (json.dumps({"en_US": "My valuation"}),)),
            ("UPDATE ir_act_client SET name=?", (json.dumps({"en_US": self.native["name"], "fr_FR": "Edited"}),)),
            ("UPDATE ir_act_client SET context=?", (repr({"model": self.old_model, "company_id": 7}),)),
            ("UPDATE ir_act_client SET context='dict(model=unknown)'", ()),
            ("UPDATE ir_act_client SET res_model=?", (self.new_model,)),
            ("UPDATE ir_act_client SET tag='custom'", ()),
            ("UPDATE ir_act_client SET path='custom-path'", ()),
            ("UPDATE ir_act_client SET target='new'", ()),
            ("UPDATE ir_act_client SET params_store='{}'", ()),
            ("UPDATE ir_act_client SET help='edited'", ()),
            ("UPDATE ir_act_client SET binding_model_id=7", ()),
            ("UPDATE ir_act_client SET binding_type='report'", ()),
            ("UPDATE ir_act_client SET binding_view_types='form'", ()),
            ("INSERT INTO other_actions VALUES ('ir_act_window',?,?)", (self.action, "other-path")),
            ("INSERT INTO other_actions VALUES ('ir_act_window',999,?)", (self.native["path"],)),
            ("UPDATE ir_model_data SET res_id=-1 WHERE module='stock_account'", ()),
            ("DELETE FROM ir_model_data WHERE module='stock_account'", ()),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='stock_account'", ()),
            ("UPDATE ir_model_data SET model='ir.actions.act_window' WHERE module='stock_account'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','alias','ir.actions.client',?,0)",
                (self.action,),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','parent','ir.actions.actions',?,0)",
                (self.action,),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('account',?,'ir.actions.client',999,0)",
                (self.action_name,),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('account',?,'wrong',?,0)",
                (self.action_name, self.action),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('account',?,'ir.actions.client',?,1)",
                (self.action_name, self.action),
            ),
        )
        for query, parameters in changes:
            with self.subTest(query=query, parameters=parameters):
                self.connection.execute("SAVEPOINT custom")
                self.connection.execute(query, parameters)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO custom")
                self.connection.execute("RELEASE custom")

    def test_other_preflight_failure_leaves_action_and_aliases_unchanged(self) -> None:
        self.xmlid("website", "policy", "ir.rule", 991, False)
        self.xmlid("website", "policy_ir_rule", "ir.access", 992, False)
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_protected_native_target_refuses_before_any_effect(self) -> None:
        target = bridge.STOCK_VALUATION_DATA_FILE
        original = target.read_bytes()
        for level in ("root", "record"):
            document = ElementTree.fromstring(original)
            protected = document if level == "root" else document.find("record")
            assert protected is not None
            protected.set("noupdate", "1")
            ElementTree.ElementTree(document).write(target)
            before = self.snapshot()
            with self.subTest(level=level), self.assertRaises(RuntimeError):
                self.apply()
            self.assertEqual(self.snapshot(), before)
        target.write_bytes(original)

    def test_absent_stock_account_does_not_adopt_actions_by_path(self) -> None:
        self.connection.execute("UPDATE ir_module_module SET state='uninstalled' WHERE name='stock_account'")
        self.apply()
        self.assertIsNone(bridge._reference(self.cursor, "account", self.action_name))
