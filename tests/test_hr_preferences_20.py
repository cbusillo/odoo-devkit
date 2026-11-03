from __future__ import annotations

import json
import runpy
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

from test_core_references_20 import CoreReferenceFixture, Cursor, bridge, patch_attr


class PreferencesCursor(Cursor):
    def execute(self, query: str, parameters: tuple = ()) -> object:
        if query.startswith("LOCK TABLE"):
            return None
        return super().execute(query, parameters)


class HrPreferencesTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = PreferencesCursor(self.connection)
        self.helpers = runpy.run_path(str(Path(bridge.__file__).with_name("mail_view_20.py")))
        self.connection.executescript(
            "CREATE TABLE ir_ui_view(id INTEGER PRIMARY KEY,inherit_id INTEGER,name TEXT,model TEXT,type TEXT,mode TEXT,"
            "active BOOLEAN,priority INTEGER,arch_updated BOOLEAN,arch_fs TEXT,arch_db JSON,arch_prev TEXT,"
            "customize_show BOOLEAN,is_seo_optimized BOOLEAN,track BOOLEAN,visibility TEXT,key TEXT,website_id INTEGER,"
            "write_uid INTEGER,custom_value TEXT);"
            "CREATE TABLE ir_ui_view_custom(ref_id INTEGER);CREATE TABLE ir_ui_view_group_rel(view_id INTEGER,group_id INTEGER);"
            "CREATE TABLE view_links(view_id INTEGER REFERENCES ir_ui_view(id),preferences TEXT,role TEXT);"
            "INSERT INTO ir_module_module VALUES ('hr','installed','19.0.1.0'),('mail_bot_hr','installed','19.0.1.0');"
            "INSERT INTO ir_ui_view(id,model,type,mode,active,arch_db) VALUES (100,'res.users','form','primary',1,'{}');"
        )
        self.xmlid("base", "view_users_form_simple_modif", "ir.ui.view", 100, False)
        self.primary = "res_users_view_form_simple_modif"
        self.child = "res_users_view_form_preferences"
        self.identities = {
            f"hr.{self.primary}": 200,
            f"hr.{self.child}": 201,
            f"mail_bot_hr.{self.primary}": 300,
            f"mail_bot_hr.{self.child}": 301,
        }
        self.stock = {}
        for key, identity in self.identities.items():
            module, name = key.split(".")
            source = Path(bridge.__file__).with_name("legacy") / module
            source /= "res_users_19.xml" if module == "hr" else "res_users_views_19.xml"
            fields = bridge._hr_native_fields(source, name, "ir.ui.view")
            self.stock[key] = fields
            parent = fields["inherit_id"].attrib["ref"]
            parent = parent if "." in parent else f"{module}.{parent}"
            self.xmlid(module, name, "ir.ui.view", identity, False)
            architecture = self.helpers["_render_architecture"](fields["arch"])
            self.connection.execute(
                "INSERT INTO ir_ui_view(id,inherit_id,name,model,type,mode,active,priority,arch_updated,arch_fs,arch_db,"
                "arch_prev,customize_show,is_seo_optimized,write_uid) VALUES (?,?,?,'res.users','form',?,1,?,0,?,?,?,0,0,67)",
                (
                    identity,
                    100 if parent.startswith("base.") else self.identities[parent],
                    fields["name"].text,
                    fields["mode"].text if "mode" in fields else "extension",
                    int(fields["priority"].text or "") if "priority" in fields else 16,
                    f"{module}/views/" + ("res_users.xml" if module == "hr" else "res_users_views.xml"),
                    json.dumps({"en_US": architecture}),
                    architecture,
                ),
            )
            self.connection.execute("INSERT INTO view_links VALUES (?,'retained preference','retained role')", (identity,))
        source = Path(bridge.__file__).with_name("legacy") / "hr/res_users_19.xml"
        document = ElementTree.parse(source, parser=ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True)))
        child = document.find(f".//record[@id='{self.child}']")
        assert child is not None
        replacements = {
            "//h5[@name='h5_email']": "//div[@name='email']",
            "//h5[@name='h5_phone']": "//div[@name='phone']",
            "//div[hasclass('oe_title')]": "//*[hasclass('o_form_header_details')]",
        }
        for element in child.findall("field[@name='arch']/xpath"):
            if element.get("expr") in replacements:
                element.set("expr", replacements[element.attrib["expr"]])
        self.target = Path(self.directory.name) / "hr-users.xml"
        document.write(self.target)
        patch_attr(bridge, "HR_USERS_DATA_FILE", self.target).start()

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("ir_ui_view", "ir_ui_view_custom", "ir_ui_view_group_rel", "view_links")
        }

    def assert_refused(self, statement: str, parameters: tuple = ()) -> None:
        with self.subTest(statement=statement, parameters=parameters):
            self.connection.execute("SAVEPOINT unsupported")
            try:
                self.connection.execute(statement, parameters)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
            finally:
                self.connection.execute("ROLLBACK TO unsupported")
                self.connection.execute("RELEASE unsupported")

    def test_stages_selected_target_child_and_preserves_every_other_column_and_dependent_row(self) -> None:
        before = self.snapshot()
        self.apply()
        after = self.snapshot()
        target = bridge._hr_native_fields(self.target, self.child, "ir.ui.view")
        expected = self.helpers["_normalized_architecture"](self.helpers["_render_architecture"](target["arch"]))
        for old, new in zip(before["ir_ui_view"], after["ir_ui_view"], strict=True):
            if old[0] == self.identities[f"hr.{self.child}"]:
                self.assertEqual(old[:10] + old[11:], new[:10] + new[11:])
                actual = self.helpers["_normalized_architecture"](json.loads(new[10])["en_US"])
                self.assertEqual(actual, expected)
                self.assertNotEqual(old[10], new[10])
            else:
                self.assertEqual(old, new)
        for table in ("view_links", "ir_ui_view_custom", "ir_ui_view_group_rel"):
            self.assertEqual(after[table], before[table])
        self.apply()
        self.assertEqual(self.snapshot(), after)
        bridge.migrate(self.cursor, "20.0.1.0")
        self.assertEqual(self.snapshot(), after)

    def test_preserves_retiring_homeworking_descendant_without_rebasing_it(self) -> None:
        self.connection.execute(
            "INSERT INTO ir_ui_view(id,inherit_id,model,arch_db,arch_prev) VALUES (400,?,'res.users',?,?)",
            (self.identities[f"hr.{self.child}"], json.dumps({"en_US": "<data retained='1'/>"}), "retained history"),
        )
        self.xmlid("hr_homeworking", "res_useurs_view_form_profile", "ir.ui.view", 400, False)
        before = self.connection.execute("SELECT * FROM ir_ui_view WHERE id=400").fetchone()
        self.apply()
        self.assertEqual(self.connection.execute("SELECT * FROM ir_ui_view WHERE id=400").fetchone(), before)
        self.assert_refused(
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('base','outside','ir.ui.view',400,0)"
        )

    def test_source_without_mailbot_is_supported_without_creating_descendants(self) -> None:
        self.connection.execute("DELETE FROM view_links WHERE view_id IN (300,301)")
        self.connection.execute("DELETE FROM ir_ui_view WHERE id IN (300,301)")
        self.connection.execute("DELETE FROM ir_model_data WHERE module='mail_bot_hr'")
        self.connection.execute("DELETE FROM ir_module_module WHERE name='mail_bot_hr'")
        self.apply()
        self.assertEqual(self.connection.execute("SELECT count(*) FROM ir_ui_view").fetchone(), (3,))

    def test_absent_cohort_leaves_unrelated_historical_hr_preferences_alone(self) -> None:
        self.connection.execute("DELETE FROM ir_model_data WHERE module='hr' AND name=?", (self.child,))
        before = self.snapshot()
        self.assertIsNone(bridge._hr_preferences_view_plan(self.cursor))
        self.assertEqual(self.snapshot(), before)

    def test_customized_protected_restricted_rebound_and_unknown_dependent_states_refuse_without_effects(self) -> None:
        for identity in self.identities.values():
            for column, value in (
                ("arch_db", json.dumps({"en_US": "<data custom='1'/>"})),
                ("arch_db", json.dumps({"en_US": "<data/>", "fr_FR": "<data/>"})),
                ("arch_prev", "<data custom='1'/>"),
                ("active", 0),
                ("arch_updated", 1),
                ("inherit_id", -1),
                ("custom_value", "preserve"),
                ("website_id", 1),
                ("visibility", "connected"),
            ):
                self.assert_refused('UPDATE ir_ui_view SET "' + column + '"=? WHERE id=?', (value, identity))
            for statement in (
                "INSERT INTO ir_ui_view_custom VALUES (?)",
                "INSERT INTO ir_ui_view_group_rel VALUES (?,1)",
                "INSERT INTO ir_ui_view(id,inherit_id) VALUES (999,?)",
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','alias','ir.ui.view',?,0)",
                "UPDATE ir_model_data SET noupdate=1 WHERE model='ir.ui.view' AND res_id=?",
                "UPDATE ir_model_data SET res_id=-1 WHERE model='ir.ui.view' AND res_id=?",
            ):
                self.assert_refused(statement, (identity,))

    def test_comment_and_attribute_changes_are_not_normalized_into_stock(self) -> None:
        identity = self.identities[f"hr.{self.child}"]
        arch = self.helpers["_render_architecture"](self.stock[f"hr.{self.child}"]["arch"])
        for attribute in (False, True):
            document = ElementTree.fromstring(
                arch, parser=ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
            )
            if attribute:
                document.set("custom_attribute", "preserve")
            else:
                document.append(ElementTree.Comment("customized"))
            changed = ElementTree.tostring(document, encoding="unicode")
            self.assert_refused("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": changed}), identity))

    def test_changed_native_binding_or_primary_refuses_and_late_mail_failure_keeps_all_rows(self) -> None:
        original = self.target.read_bytes()
        for name, field, value in ((self.child, "model", "res.partner"), (self.primary, "arch", "changed")):
            document = ElementTree.fromstring(original)
            node = document.find(f".//record[@id='{name}']/field[@name='{field}']")
            assert node is not None
            if field == "arch":
                node[0].set("custom", value)
            else:
                node.text = value
            self.target.write_bytes(ElementTree.tostring(document))
            try:
                self.assert_refused("SELECT 1")
            finally:
                self.target.write_bytes(original)
        before = self.snapshot()
        with patch_attr(bridge, "_admit_mail", Mock(side_effect=RuntimeError("unqualified Mail"))):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.snapshot(), before)
