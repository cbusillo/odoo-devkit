from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock
from xml.etree import ElementTree

import test_core_references_20 as core

bridge = core.bridge


class CalendarCursor(core.Cursor):
    def execute(self, query: str, parameters: tuple = ()) -> Any:
        if query.startswith("LOCK TABLE"):
            return None
        if "to_regclass" in query:
            query = (
                "SELECT (SELECT name FROM sqlite_master WHERE name='calendar_calendar'), "
                "(SELECT name FROM sqlite_master WHERE name='calendar_user')"
            )
        return super().execute(query, parameters)


class CalendarUsersTests(core.CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = CalendarCursor(self.connection)
        self.connection.executescript(
            "CREATE TABLE ir_ui_view(id INTEGER PRIMARY KEY, inherit_id INTEGER, name TEXT, model TEXT, type TEXT, mode TEXT, "
            "active BOOLEAN, priority INTEGER, arch_updated BOOLEAN, arch_fs TEXT, is_seo_optimized BOOLEAN, arch_db JSON, "
            "arch_prev TEXT, write_uid INTEGER, custom_value TEXT);"
            "CREATE TABLE ir_ui_view_custom(id INTEGER, ref_id INTEGER);"
            "CREATE TABLE ir_ui_view_group_rel(view_id INTEGER, group_id INTEGER);"
            "CREATE TABLE res_users(id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE res_users_settings(id INTEGER PRIMARY KEY, user_id INTEGER, calendar_default_privacy TEXT, other TEXT);"
            "CREATE TABLE ir_config_parameter(key TEXT, value TEXT);"
            "CREATE TABLE calendar_event(id INTEGER PRIMARY KEY, privacy TEXT);"
            "CREATE TABLE calendar_recurrence(id INTEGER PRIMARY KEY);"
            "CREATE TABLE ir_rule(domain_force TEXT);"
            "CREATE TABLE ir_act_window(context TEXT, domain TEXT);"
            "CREATE TABLE ir_act_server(code TEXT);"
            "CREATE TABLE ir_filters(context TEXT, domain TEXT);"
            "INSERT INTO ir_module_module VALUES ('calendar','installed','19.0.1.1');"
            "INSERT INTO res_users VALUES (1,'Internal'),(2,'Archived'),(3,'No settings');"
            "INSERT INTO res_users_settings VALUES (1,1,'public','retained'),(2,2,'public','retained');"
            "INSERT INTO ir_config_parameter VALUES ('calendar.default_privacy','public');"
        )
        self.stock = bridge._calendar_stock_views("19")
        previous = bridge._calendar_stock_views("18")
        self.views = []
        for index, (name, fields) in enumerate(self.stock.items()):
            parent_id, view_id = 100 + index, 200 + index
            self.views.append(view_id)
            self.xmlid("base", fields["inherit_id"].attrib["ref"].removeprefix("base."), "ir.ui.view", parent_id, False)
            self.xmlid("calendar", name, "ir.ui.view", view_id, False)
            self.connection.execute("INSERT INTO ir_ui_view(id,model,arch_db) VALUES (?,'res.users',?)", (parent_id, "{}"))
            self.connection.execute(
                "INSERT INTO ir_ui_view(id,inherit_id,name,model,type,mode,active,priority,arch_updated,arch_fs,"
                "is_seo_optimized,arch_db,arch_prev,write_uid) VALUES (?,?,?,'res.users','form','extension',1,16,0,"
                "'calendar/views/res_users_views.xml',0,?,?,67)",
                (
                    view_id,
                    parent_id,
                    fields["name"].text,
                    json.dumps({"en_US": self.render(fields["arch"])}),
                    self.render(previous[name]["arch"]),
                ),
            )

    @staticmethod
    def render(field: ElementTree.Element) -> str:
        return "<data>" + ElementTree.tostring(field[0], encoding="unicode") + "</data>"

    def snapshot(self) -> dict[str, list[tuple]]:
        tables = [row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: self.connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall() for table in tables}

    def assert_refused(self, statement: str, parameters: tuple = ()) -> None:
        with self.subTest(statement=statement, parameters=parameters):
            self.connection.execute("SAVEPOINT unsupported")
            self.connection.execute(statement, parameters)
            before = self.snapshot()
            with self.assertRaises(RuntimeError):
                self.apply()
            self.assertEqual(self.snapshot(), before)
            self.connection.execute("ROLLBACK TO unsupported")
            self.connection.execute("RELEASE unsupported")

    def test_stock_preferences_stop_referencing_removed_field_and_preserve_history_and_data(self) -> None:
        before = self.snapshot()
        self.apply()
        after = self.snapshot()
        for table in before.keys() - {"ir_ui_view", "ir_model_data"}:
            self.assertEqual(after[table], before[table])
        self.assertEqual([row for row in after["ir_model_data"] if row in before["ir_model_data"]], before["ir_model_data"])
        for old, new in zip(before["ir_ui_view"], after["ir_ui_view"], strict=True):
            self.assertEqual(old[:11] + old[12:], new[:11] + new[12:])
            if old[0] in self.views:
                self.assertTrue(ElementTree.fromstring(json.loads(old[11])["en_US"]).findall(".//field"))
                architecture = ElementTree.fromstring(json.loads(new[11])["en_US"])
                self.assertEqual(list(architecture), [])
            else:
                self.assertEqual(old, new)
        self.apply()
        self.assertEqual(self.snapshot(), after)
        bridge.migrate(self.cursor, "20.0.1.0")
        self.assertEqual(self.snapshot(), after)
        self.assertTrue(self.connection.in_transaction)

    def test_matching_nonpublic_fallback_and_missing_or_null_settings_are_preserved(self) -> None:
        for privacy in ("public", "private", "confidential"):
            with self.subTest(privacy=privacy):
                self.connection.execute("SAVEPOINT preferences")
                self.connection.execute("UPDATE ir_config_parameter SET value=?", (privacy,))
                self.connection.execute("UPDATE res_users_settings SET calendar_default_privacy=? WHERE id=1", (privacy,))
                self.connection.execute("UPDATE res_users_settings SET calendar_default_privacy=NULL WHERE id=2")
                before = self.snapshot()
                self.apply()
                for table in ("res_users", "res_users_settings", "ir_config_parameter"):
                    self.assertEqual(self.snapshot()[table], before[table])
                self.connection.execute("ROLLBACK TO preferences")
                self.connection.execute("RELEASE preferences")
        self.connection.execute("DELETE FROM ir_config_parameter")
        self.apply()

    def test_divergence_ambiguous_preferences_and_unqualified_calendar_data_refuse_before_effects(self) -> None:
        for statement in (
            "UPDATE res_users_settings SET calendar_default_privacy='private' WHERE id=1",
            "UPDATE res_users_settings SET calendar_default_privacy='confidential' WHERE id=1",
            "UPDATE res_users_settings SET calendar_default_privacy='custom' WHERE id=1",
            "UPDATE ir_config_parameter SET value='private'",
            "UPDATE ir_config_parameter SET value=''",
            "INSERT INTO ir_config_parameter VALUES ('calendar.default_privacy','public')",
            "INSERT INTO res_users_settings VALUES (3,1,'public','duplicate')",
            "UPDATE res_users_settings SET user_id=99 WHERE id=1",
            "INSERT INTO calendar_event VALUES (1,'private')",
            "INSERT INTO calendar_recurrence VALUES (1)",
            "CREATE TABLE calendar_calendar(id INTEGER PRIMARY KEY)",
            "CREATE TABLE calendar_user(id INTEGER PRIMARY KEY)",
        ):
            self.assert_refused(statement)

    def test_custom_ownership_architectures_history_dependencies_and_metadata_refuse_before_effects(self) -> None:
        for identity in self.views:
            for column, value in (
                ("arch_db", json.dumps({"en_US": '<data><field name="custom"/></data>'})),
                ("arch_db", json.dumps({"en_US": "<data/>", "fr_FR": "<data/>"})),
                ("arch_db", json.dumps({"en_US": "invalid"})),
                ("arch_prev", '<data><field name="custom_history"/></data>'),
                ("custom_value", "retained"),
                ("mode", "primary"),
                ("active", 0),
                ("arch_updated", 1),
                ("priority", 17),
                ("inherit_id", -1),
                ("name", "custom"),
                ("model", "res.partner"),
            ):
                self.assert_refused('UPDATE ir_ui_view SET "' + column + '"=? WHERE id=?', (value, identity))
            for statement in (
                "INSERT INTO ir_ui_view_custom VALUES (1,?)",
                "INSERT INTO ir_ui_view_group_rel VALUES (?,1)",
                "INSERT INTO ir_ui_view(id,inherit_id,arch_db) VALUES (300,?,'{}')",
                "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','alias','ir.ui.view',?)",
                "UPDATE ir_model_data SET noupdate=1 WHERE model='ir.ui.view' AND res_id=?",
                "UPDATE ir_model_data SET model='custom' WHERE model='ir.ui.view' AND res_id=?",
                "UPDATE ir_model_data SET res_id=-1 WHERE model='ir.ui.view' AND res_id=?",
                "DELETE FROM ir_ui_view WHERE id=?",
            ):
                self.assert_refused(statement, (identity,))
        architecture = self.render(next(iter(self.stock.values()))["arch"])
        for altered in (architecture.replace('string="Privacy"', 'string="Custom"'), architecture.replace("after", "before")):
            self.assert_refused("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": altered}), self.views[1]))

    def test_external_preference_references_refuse_before_effects(self) -> None:
        for statement in (
            "INSERT INTO ir_rule VALUES (?)",
            "INSERT INTO ir_act_window(context) VALUES (?)",
            "INSERT INTO ir_act_window(domain) VALUES (?)",
            "INSERT INTO ir_act_server VALUES (?)",
            "INSERT INTO ir_filters(context) VALUES (?)",
            "INSERT INTO ir_filters(domain) VALUES (?)",
            "INSERT INTO ir_ui_view(id,arch_db) VALUES (400,?)",
        ):
            self.assert_refused(statement, ("calendar_default_privacy",))

    def test_late_planner_refusals_preserve_calendar_architectures(self) -> None:
        before = self.snapshot()
        for planner in ("_hr_org_chart_action_plan", "_hr_org_chart_link_plan", "_admit_mail"):
            with self.subTest(planner=planner):
                with (
                    core.patch_attr(bridge, "_hr_org_chart_action_plan", Mock(return_value=object())),
                    core.patch_attr(bridge, "_hr_org_chart_link_plan", Mock(return_value=None)),
                    core.patch_attr(bridge, planner, Mock(side_effect=RuntimeError("unsupported retained input"))) as refusal,
                ):
                    with self.assertRaises(RuntimeError):
                        self.apply()
                    refusal.assert_called_once()
                self.assertEqual(self.snapshot(), before)
