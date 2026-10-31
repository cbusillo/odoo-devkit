from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "prepare_core_references_20.py"
spec = importlib.util.spec_from_file_location("core_references_20", SCRIPT)
assert spec is not None and spec.loader is not None
bridge: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def patch_attr(target: Any, name: str, value: Any) -> Any:
    return patch.object(target, name, value)


class Cursor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.cursor = connection.cursor()

    @property
    def description(self) -> Any:
        return self.cursor.description

    def execute(self, query: str, parameters: tuple = ()) -> Any:
        return self.cursor.execute(query.replace("%s", "?").replace(" FOR UPDATE", "").replace("::jsonb", ""), parameters)

    def fetchone(self) -> Any:
        return self.cursor.fetchone()

    def fetchall(self) -> list[tuple]:
        return [
            tuple(
                json.loads(value)
                if column[0] == "arch_db"
                else datetime.fromisoformat(value)
                if column[0] == "create_date" and value is not None
                else value
                for column, value in zip(self.cursor.description, row, strict=True)
            )
            for row in self.cursor.fetchall()
        ]


class MailAdmissionWrapperTests(unittest.TestCase):
    def test_installed_mail_delegates_native_target_and_absent_mail_skips(self) -> None:
        cursor = Mock()
        admit_mail = bridge._admit_mail
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "views/ir_actions_server_views.xml"
            source.parent.mkdir()
            source.write_text("selected native Mail source")
            module = ModuleType("odoo.modules.module")
            resolver = Mock(return_value=str(root))
            module.get_module_path = resolver
            observed = []

            def admit(candidate: Any, target: Path) -> None:
                self.assertIs(candidate, cursor)
                observed.append(target.read_text())

            with (
                patch.dict(
                    sys.modules,
                    {"odoo": ModuleType("odoo"), "odoo.modules": ModuleType("odoo.modules"), "odoo.modules.module": module},
                ),
                patch.object(bridge.runpy, "run_path", return_value={"admit_before_effects": admit}) as load,
            ):
                for state in (None, ("uninstalled",)):
                    cursor.fetchone.return_value = state
                    admit_mail(cursor)
                    load.assert_not_called()
                for state in (("installed",), ("to upgrade",)):
                    cursor.fetchone.return_value = state
                    admit_mail(cursor)
            self.assertEqual(observed, [source.read_text(), source.read_text()])
            self.assertEqual(resolver.call_count, len(observed))


class CoreReferenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE ir_model_data (id INTEGER PRIMARY KEY, module TEXT, name TEXT, model TEXT, res_id INTEGER, "
            "noupdate BOOLEAN, create_uid INTEGER, write_uid INTEGER, UNIQUE(module,name));"
            "CREATE TABLE res_country (id INTEGER PRIMARY KEY, code TEXT);"
            "CREATE TABLE res_country_state (id INTEGER PRIMARY KEY, country_id INTEGER, code TEXT, name TEXT, write_uid INTEGER);"
            "CREATE TABLE website (id INTEGER PRIMARY KEY, name TEXT, domain TEXT, company_id INTEGER);"
            "CREATE TABLE ir_module_module (name TEXT PRIMARY KEY, state TEXT, latest_version TEXT);"
            "CREATE TABLE res_partner (id INTEGER PRIMARY KEY, state_id INTEGER REFERENCES res_country_state(id));"
            "CREATE TABLE carrier_state_rel (carrier_id INTEGER, state_id INTEGER REFERENCES res_country_state(id));"
            "PRAGMA foreign_keys=ON;"
        )
        self.cursor = Cursor(self.connection)
        self.country = self.connection.execute("INSERT INTO res_country(code) VALUES ('IN')").lastrowid
        self.state = self.connection.execute(
            "INSERT INTO res_country_state(country_id,code,name,write_uid) VALUES (?, 'OD', 'Native state', 37)",
            (self.country,),
        ).lastrowid
        self.website = self.connection.execute(
            "INSERT INTO website(name,domain,company_id) VALUES ('Retained store','https://store.invalid',91)"
        ).lastrowid
        assert self.country is not None
        assert self.state is not None
        assert self.website is not None
        self.xmlid("base", "in", "res.country", self.country, True)
        self.xmlid("base", bridge.SOURCE_STATE, "res.country.state", self.state, False)
        self.xmlid("website", "default_website", "website", self.website, True)
        self.xmlid("custom", "retained_state", "res.country.state", self.state, True)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('website','installed','19.0.1.0')")
        self.connection.execute("INSERT INTO res_partner(state_id) VALUES (?)", (self.state,))
        self.connection.execute("INSERT INTO carrier_state_rel VALUES (73,?)", (self.state,))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        target = Path(self.directory.name) / "states.csv"
        target.write_text(f"id,country_id:id,name,code\n{bridge.TARGET_STATE},in,Native state,OD\n")
        self.addCleanup(patch.stopall)
        patch_attr(bridge, "STATE_DATA_FILE", target).start()
        patch_attr(bridge, "_admit_mail", lambda _cr: None).start()

    def xmlid(self, module: str, name: str, model: str, record: int, noupdate: bool) -> None:
        self.connection.execute(
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate,create_uid,write_uid) VALUES (?,?,?,?,?,47,53)",
            (module, name, model, record, noupdate),
        )

    def snapshot(self) -> dict[str, list[tuple]]:
        tables = (
            "ir_model_data",
            "res_country",
            "res_country_state",
            "website",
            "ir_module_module",
            "res_partner",
            "carrier_state_rel",
        )
        return {table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall() for table in tables}

    def apply(self) -> None:
        bridge.migrate(self.cursor, "19.0.1.3")


class CoreReferenceTests(CoreReferenceFixture):
    def test_mail_family_refusal_precedes_every_bridge_effect(self) -> None:
        before = self.snapshot()
        with patch.object(bridge, "_admit_mail", side_effect=RuntimeError("custom Mail family")), self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_bridge_binds_target_to_used_source_without_rewriting_source(self) -> None:
        before = self.snapshot()
        self.apply()
        after = self.snapshot()
        new = [row for row in after["ir_model_data"] if row not in before["ir_model_data"]]
        self.assertEqual(len(new), 2)
        self.assertEqual([row for row in after["ir_model_data"] if row in before["ir_model_data"]], before["ir_model_data"])
        self.assertEqual(
            {k: v for k, v in before.items() if k != "ir_model_data"}, {k: v for k, v in after.items() if k != "ir_model_data"}
        )
        for name, model, record in (
            (bridge.TARGET_STATE, "res.country.state", self.state),
            ("default_website", "website", self.website),
        ):
            self.assertEqual(
                self.connection.execute("SELECT model,res_id FROM ir_model_data WHERE module='base' AND name=?", (name,)).fetchone(),
                (model, record),
            )
        self.apply()
        self.assertEqual(self.snapshot(), after)

    def test_target_metadata_is_preserved_on_replay(self) -> None:
        self.xmlid("base", bridge.TARGET_STATE, "res.country.state", self.state, True)
        self.xmlid("base", "default_website", "website", self.website, True)
        before = self.snapshot()
        self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_missing_dangling_and_rebound_sources_refuse_without_effects(self) -> None:
        for name in ("in", bridge.SOURCE_STATE):
            for change in (
                "DELETE FROM ir_model_data WHERE module='base' AND name=?",
                "UPDATE ir_model_data SET model='wrong' WHERE module='base' AND name=?",
                "UPDATE ir_model_data SET res_id=-1 WHERE module='base' AND name=?",
            ):
                with self.subTest(name=name, change=change):
                    self.connection.execute("SAVEPOINT control")
                    self.connection.execute(change, (name,))
                    before = self.snapshot()
                    with self.assertRaises(RuntimeError):
                        self.apply()
                    self.assertEqual(self.snapshot(), before)
                    self.connection.execute("ROLLBACK TO control")
                    self.connection.execute("RELEASE control")

    def test_website_failure_and_conflicting_targets_refuse_before_any_alias(self) -> None:
        changes = (
            ("DELETE FROM ir_model_data WHERE module='website'", ()),
            ("UPDATE website SET id=-1", ()),
            ("UPDATE ir_model_data SET model='wrong' WHERE module='website'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('base',?,'res.country.state',-1,0)",
                (bridge.TARGET_STATE,),
            ),
            ("INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('base','default_website','website',-1,1)", ()),
            ("UPDATE res_country_state SET code='other'", ()),
            ("UPDATE res_country SET code='other'", ()),
            ("UPDATE res_country_state SET name='Custom name'", ()),
            ("UPDATE ir_model_data SET noupdate=0 WHERE module='website'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('base','default_website','website',?,0)",
                (self.website,),
            ),
        )
        for statement, parameters in changes:
            with self.subTest(statement=statement):
                self.connection.execute("SAVEPOINT control")
                self.connection.execute(statement, parameters)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO control")
                self.connection.execute("RELEASE control")

    def test_protected_custom_state_name_survives(self) -> None:
        self.connection.execute("UPDATE res_country_state SET name='Custom name'")
        self.connection.execute("UPDATE ir_model_data SET noupdate=1 WHERE name=?", (bridge.SOURCE_STATE,))
        self.apply()
        target = self.connection.execute("SELECT noupdate FROM ir_model_data WHERE name=?", (bridge.TARGET_STATE,)).fetchone()
        self.assertIsNotNone(target)
        self.assertTrue(target[0])
        self.assertEqual(
            self.connection.execute("SELECT name FROM res_country_state WHERE id=?", (self.state,)).fetchone()[0], "Custom name"
        )

    def test_unprotected_target_cannot_replace_a_protected_state_binding(self) -> None:
        self.connection.execute("UPDATE ir_model_data SET noupdate=1 WHERE name=?", (bridge.SOURCE_STATE,))
        self.xmlid("base", bridge.TARGET_STATE, "res.country.state", self.state, False)
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_no_website_installed_does_not_require_or_create_website_binding(self) -> None:
        self.connection.execute("DELETE FROM ir_module_module WHERE name='website'")
        self.connection.execute("DELETE FROM ir_model_data WHERE module='website'")
        self.apply()
        self.assertIsNone(self.connection.execute("SELECT id FROM ir_model_data WHERE name='default_website'").fetchone())

    def test_same_major_is_noop_and_other_crossings_refuse(self) -> None:
        before = self.snapshot()
        bridge.migrate(self.cursor, "20.0.1.3")
        self.assertEqual(self.snapshot(), before)
        for version in (None, "", "18.0.1.3", "21.0.1.3"):
            with self.subTest(version=version), self.assertRaises(RuntimeError):
                bridge.migrate(self.cursor, version)
            self.assertEqual(self.snapshot(), before)


class PolicyXMLIDTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.connection.executescript(
            "INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.3');"
            "INSERT INTO ir_module_module VALUES ('studio_customization','installed','19.0.1.0');"
            "CREATE TABLE ir_model_access (id INTEGER PRIMARY KEY, name TEXT, perm_read BOOLEAN, perm_write BOOLEAN);"
            "CREATE TABLE ir_rule (id INTEGER PRIMARY KEY, domain_force TEXT, active BOOLEAN);"
            "CREATE TABLE ir_access (id INTEGER PRIMARY KEY, operation TEXT);"
            "INSERT INTO ir_model_access VALUES (81,'Retained access',1,0),(82,'Unowned access',0,0);"
            "INSERT INTO ir_rule VALUES (91,'Retained domain',0);"
        )
        self.xmlid("base", "access_decimal_precision_config", "ir.model.access", 81, False)
        self.xmlid("website", "retained_rule", "ir.rule", 91, True)
        self.xmlid("__custom__", "custom_access", "ir.model.access", 81, True)
        self.xmlid("__export__", "exported_rule", "ir.rule", 91, False)
        self.xmlid("studio_customization", "studio_rule", "ir.rule", 91, True)
        self.xmlid("missing_owner", "unowned_access", "ir.model.access", 82, False)

    def policies(self) -> dict[str, list[tuple]]:
        return {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY id").fetchall()
            for table in ("ir_model_access", "ir_rule", "ir_access")
        }

    def test_releases_native_names_preserving_legacy_rows_alias_metadata_and_replay(self) -> None:
        before = self.snapshot()
        policies = self.policies()
        self.assertEqual(bridge._reference(self.cursor, "base", "access_decimal_precision_config").model, "ir.model.access")
        self.apply()
        for module, name, model, record in (
            ("base", "access_decimal_precision_config", "ir.model.access", 81),
            ("website", "retained_rule", "ir.rule", 91),
        ):
            self.assertIsNone(bridge._reference(self.cursor, module, name))
            old = next(row for row in before["ir_model_data"] if row[1:3] == (module, name))
            retained = self.connection.execute("SELECT * FROM ir_model_data WHERE id=?", (old[0],)).fetchone()
            self.assertEqual(retained[:2] + retained[3:], old[:2] + old[3:])
            self.assertEqual(bridge._reference(self.cursor, module, retained[2]).record_id, record)
            self.assertEqual(retained[3], model)
        for old in before["ir_model_data"]:
            if old[1] in {"__custom__", "__export__", "studio_customization", "missing_owner"}:
                self.assertEqual(self.connection.execute("SELECT * FROM ir_model_data WHERE id=?", (old[0],)).fetchone(), old)
        self.assertEqual(self.policies(), policies)
        after = self.snapshot()
        self.apply()
        self.assertEqual(self.snapshot(), after)
        self.connection.execute("INSERT INTO ir_access VALUES (101,'ru')")
        self.xmlid("base", "access_decimal_precision_config", "ir.access", 101, False)
        after = self.snapshot()
        policies = self.policies()
        self.apply()
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(self.policies(), policies)

    def test_destination_collision_refuses_before_every_effect(self) -> None:
        for model, record, protected in (("ir.model.access", 81, False), ("ir.model.access", 82, True), ("ir.access", 101, False)):
            self.connection.execute("SAVEPOINT collision")
            with self.subTest(model=model, record=record, protected=protected):
                self.xmlid("base", "access_decimal_precision_config_ir_model_access", model, record, protected)
                before = self.snapshot()
                policies = self.policies()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.policies(), policies)
            self.connection.execute("ROLLBACK TO collision")
            self.connection.execute("RELEASE collision")

    def test_other_reference_refusals_leave_policy_names_untouched(self) -> None:
        self.connection.execute("UPDATE website SET id=-1")
        before = self.snapshot()
        policies = self.policies()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.policies(), policies)


class InheritedViewTests(CoreReferenceFixture):
    module, view_name, model, parent_name, old_selector, new_selector = bridge.INHERITED_VIEWS[0]

    def architecture(self, label: str) -> str:
        return f"<data><xpath {self.old_selector}/><separator string='{label}'/></data>"

    def inherited_view(self, state: str = "installed") -> int:
        self.connection.execute(
            "CREATE TABLE ir_ui_view (id INTEGER PRIMARY KEY, model TEXT, inherit_id INTEGER, mode TEXT, arch_db TEXT, "
            "arch_prev TEXT, arch_updated BOOLEAN, active BOOLEAN, priority INTEGER, write_uid INTEGER)"
        )
        parent = self.connection.execute("INSERT INTO ir_ui_view(model) VALUES (?)", (self.model,)).lastrowid
        assert parent is not None
        self.xmlid("base", self.parent_name, "ir.ui.view", parent, False)
        child = self.connection.execute(
            "INSERT INTO ir_ui_view VALUES (NULL,?,?,'primary',?,'Retained history',1,1,14,71)",
            (
                self.model,
                parent,
                json.dumps(
                    {
                        "en_US": self.architecture("Custom"),
                        "fr_FR": self.architecture("Translated"),
                    }
                ),
            ),
        ).lastrowid
        assert child is not None
        self.xmlid(self.module, self.view_name, "ir.ui.view", child, False)
        self.xmlid("custom", "retained_view", "ir.ui.view", child, True)
        self.connection.execute("INSERT INTO ir_module_module VALUES (?,?,'19.0.1.0')", (self.module, state))
        return child

    def view_snapshot(self) -> list[tuple]:
        return self.connection.execute("SELECT * FROM ir_ui_view ORDER BY id").fetchall()

    def test_selector_transition_preserves_custom_languages_metadata_and_replay(self) -> None:
        child = self.inherited_view()
        before = self.view_snapshot()
        aliases = self.connection.execute("SELECT * FROM ir_model_data WHERE model='ir.ui.view'").fetchall()
        self.apply()
        after = self.view_snapshot()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1][:4] + after[1][5:], before[1][:4] + before[1][5:])
        for language, arch in json.loads(before[1][4]).items():
            self.assertEqual(json.loads(after[1][4])[language], arch.replace(self.old_selector, self.new_selector))
        self.assertEqual(self.connection.execute("SELECT * FROM ir_model_data WHERE model='ir.ui.view'").fetchall(), aliases)
        self.assertEqual(self.connection.execute("SELECT id FROM ir_ui_view WHERE inherit_id IS NOT NULL").fetchone()[0], child)
        first = self.snapshot()
        self.apply()
        self.assertEqual(self.view_snapshot(), after)
        self.assertEqual(self.snapshot(), first)

    def test_unsupported_view_sources_refuse_before_any_effect(self) -> None:
        child = self.inherited_view()
        changes = (
            ("UPDATE ir_model_data SET noupdate=1 WHERE module=?", (self.module,)),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='base' AND name=?", (self.parent_name,)),
            ("DELETE FROM ir_model_data WHERE module='base' AND name=?", (self.parent_name,)),
            ("DELETE FROM ir_model_data WHERE module=?", (self.module,)),
            ("UPDATE ir_model_data SET model='wrong' WHERE module=?", (self.module,)),
            ("UPDATE ir_model_data SET res_id=-1 WHERE module=?", (self.module,)),
            ("UPDATE ir_ui_view SET inherit_id=NULL WHERE id=?", (child,)),
            ("UPDATE ir_ui_view SET mode='extension' WHERE id=?", (child,)),
            ("UPDATE ir_ui_view SET model='wrong' WHERE id=?", (child,)),
        )
        for statement, parameters in changes:
            with self.subTest(statement=statement):
                self.connection.execute("SAVEPOINT view_change")
                self.connection.execute(statement, parameters)
                before, rows = self.snapshot(), self.view_snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.view_snapshot(), rows)
                self.connection.execute("ROLLBACK TO view_change")
                self.connection.execute("RELEASE view_change")

    def test_uninstalled_module_leaves_its_view_unchanged(self) -> None:
        self.inherited_view(state="uninstalled")
        before = self.view_snapshot()
        self.apply()
        self.assertEqual(self.view_snapshot(), before)

    def test_missing_duplicate_and_mixed_language_selectors_refuse_without_effects(self) -> None:
        child = self.inherited_view()
        old = self.architecture("Custom")
        new = old.replace(self.old_selector, self.new_selector)
        for architectures in ({}, {"en_US": "<data/>"}, {"en_US": old + old}, {"en_US": old + new}, {"en_US": old, "fr_FR": new}):
            with self.subTest(architectures=architectures):
                self.connection.execute("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps(architectures), child))
                before, rows = self.snapshot(), self.view_snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.view_snapshot(), rows)


class CalendarViewTests(InheritedViewTests):
    module, view_name, model, parent_name, old_selector, new_selector = bridge.INHERITED_VIEWS[1]

    def architecture(self, label: str) -> str:
        return f"<data>{self.old_selector}<filter name='type_team' string='{label}' domain='[]'/></filter></data>"


class DiscussCursor(Cursor):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.connection = connection

    def execute(self, query: str, parameters: tuple = ()) -> Any:
        if "FROM pg_constraint" in query:
            rows = []
            for (table,) in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for foreign_key in self.connection.execute('PRAGMA foreign_key_list("' + table + '")'):
                    rows.append(("public", table, foreign_key[3], foreign_key[2], foreign_key[4], 1))
            return self._catalog(rows, 6)
        if "FROM information_schema.columns" in query:
            rows = []
            for (table,) in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for column in self.connection.execute('PRAGMA table_info("' + table + '")'):
                    rows.append((table, column[1], "text" if column[2] == "TEXT" else "jsonb" if column[2] == "JSON" else "integer"))
            return self._catalog(rows, 3)
        if "FROM pg_class" in query:
            return self.cursor.execute("SELECT 1 FROM materialized_references")
        if "FROM pg_trigger" in query:
            query = "SELECT 1 FROM deletion_triggers"
            parameters = ()
        return super().execute(query, parameters)

    def _catalog(self, rows: list[tuple], width: int) -> Any:
        if not rows:
            return self.cursor.execute("SELECT 1 WHERE 0")
        placeholders = ",".join("?" for _ in range(width))
        return self.cursor.execute(" UNION ALL ".join("SELECT " + placeholders for _ in rows), sum(rows, ()))

    def identified(self, cr: Any, query: str, identifiers: tuple[str, ...], parameters: tuple = ()) -> None:
        for identifier in identifiers:
            query = query.replace("%s", '"' + identifier.replace('"', '""') + '"', 1)
        if "to_jsonb(record)" in query:
            columns = [row[1] for row in self.connection.execute('PRAGMA table_info("' + identifiers[0] + '")')]
            expression = "json_object(" + ",".join("'" + name + "',record." + name for name in columns) + ")"
            query = query.replace("to_jsonb(record)->>%s", "CAST(" + expression + "->>%s AS TEXT)")
            query = query.replace("value.value", "CAST(value.value AS TEXT)")
            query = query.replace("to_jsonb(record)", expression).replace("jsonb_each_text", "json_each")
        query = query.replace("= ANY(", "IN (")
        expanded = []
        for value in parameters:
            if isinstance(value, list):
                query = query.replace("(%s)", "(" + ",".join("?" for _ in value) + ")", 1)
                expanded.extend(value)
            else:
                query = query.replace("%s", "?", 1)
                expanded.append(value)
        cr.execute(query, tuple(expanded))


class EmptyChatFixture(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.connection.executescript(
            "CREATE TABLE discuss_channel (id INTEGER PRIMARY KEY, name TEXT, channel_type TEXT, active BOOLEAN, uuid TEXT, "
            "description TEXT, last_interest_dt TEXT, create_date TEXT, write_date TEXT, custom_value TEXT);"
            "CREATE TABLE discuss_channel_member (id INTEGER PRIMARY KEY, channel_id INTEGER REFERENCES discuss_channel(id), "
            "partner_id INTEGER REFERENCES res_partner(id), guest_id INTEGER, custom_channel_name TEXT, fold_state TEXT, "
            "custom_notifications TEXT, mute_until_dt TEXT, fetched_message_id INTEGER, seen_message_id INTEGER, "
            "new_message_separator INTEGER, unpin_dt TEXT, last_interest_dt TEXT, last_seen_dt TEXT, create_date TEXT);"
            "CREATE TABLE ir_model (id INTEGER PRIMARY KEY, model TEXT);"
            "CREATE TABLE ir_model_fields (name TEXT, ttype TEXT, store BOOLEAN, company_dependent BOOLEAN, relation TEXT, model TEXT, relation_table TEXT, column2 TEXT, column1 TEXT);"
            "CREATE TABLE mail_message (id INTEGER PRIMARY KEY, model TEXT, res_id INTEGER, body TEXT);"
            "CREATE TABLE deletion_triggers (name TEXT);"
            "CREATE TABLE materialized_references (name TEXT);"
            "ALTER TABLE res_partner ADD COLUMN name TEXT;"
            "INSERT INTO res_partner(id,name) VALUES (101,'Alice'),(202,'Bob');"
            "INSERT INTO ir_module_module VALUES ('mail','installed','19.0');"
            "INSERT INTO ir_model VALUES (701,'discuss.channel'),(702,'discuss.channel.member');"
            "INSERT INTO discuss_channel VALUES (41,'Alice, Bob','chat',1,'first',NULL,'2025-01-01T10:00:00',"
            "'2025-01-01T10:00:01','2025-01-01T10:00:01',NULL);"
            "INSERT INTO discuss_channel SELECT 42,name,channel_type,active,'second',description,last_interest_dt,"
            "create_date,write_date,custom_value FROM discuss_channel WHERE id=41;"
            "INSERT INTO discuss_channel_member (id,channel_id,partner_id,fold_state,new_message_separator,last_interest_dt,create_date) "
            "VALUES (51,41,101,'closed',0,'2025-01-01T10:00:00','2025-01-01T10:00:01'),"
            "(52,41,202,'closed',0,'2025-01-01T10:00:00','2025-01-01T10:00:01'),"
            "(53,42,101,'closed',0,'2025-01-01T10:00:00','2025-01-01T10:00:01'),"
            "(54,42,202,'closed',0,'2025-01-01T10:00:00','2025-01-01T10:00:01');"
        )
        self.connection.create_function("jsonb_path_exists", 3, self.json_reference)
        self.cursor = DiscussCursor(self.connection)
        patch_attr(bridge, "_identified", self.cursor.identified).start()

    @staticmethod
    def json_reference(value: str | None, _path: str, variables: str) -> bool:
        targets = json.loads(variables)["targets"]

        def contains(item: Any) -> bool:
            if isinstance(item, dict):
                return any(contains(child) for child in item.values())
            if isinstance(item, list):
                return any(contains(child) for child in item)
            return item in targets

        return contains(json.loads(value)) if value else False

    def all_rows(self) -> dict[str, list[tuple]]:
        tables = [row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: self.connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall() for table in tables}

    def livechat_projections(self) -> None:
        self.connection.executescript(
            "ALTER TABLE discuss_channel ADD COLUMN livechat_week_day TEXT;"
            "ALTER TABLE discuss_channel ADD COLUMN livechat_start_hour REAL;"
            "UPDATE discuss_channel SET create_date='2025-01-01T10:37:59',livechat_week_day='2',livechat_start_hour=10 WHERE id=41;"
            "UPDATE discuss_channel SET create_date='2025-01-05T00:01:00',livechat_week_day='6',livechat_start_hour=0 WHERE id=42;"
        )


class EmptyChatTests(EmptyChatFixture):
    def test_creation_projections_preserve_survivor_and_replay(self) -> None:
        self.livechat_projections()
        self.test_retains_survivor_and_audience_unchanged_and_replays()

    def test_mismatched_creation_projections_refuse_before_every_effect(self) -> None:
        self.livechat_projections()
        for change in (
            "UPDATE discuss_channel SET livechat_week_day='3' WHERE id=41",
            "UPDATE discuss_channel SET livechat_week_day='7' WHERE id=42",
            "UPDATE discuss_channel SET livechat_start_hour=0 WHERE id=41",
            "UPDATE discuss_channel SET livechat_start_hour=0.5 WHERE id=42",
            "UPDATE discuss_channel SET livechat_start_hour=24 WHERE id=42",
            "UPDATE discuss_channel SET create_date=NULL WHERE id=42",
        ):
            with self.subTest(change=change):
                self.connection.execute("SAVEPOINT projection")
                self.connection.execute(change)
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)
                self.connection.execute("ROLLBACK TO projection")
                self.connection.execute("RELEASE projection")

    def test_livechat_configuration_and_history_refuse_with_valid_projections(self) -> None:
        self.livechat_projections()
        for name, sql_type, value in (
            ("livechat_channel_id", "INTEGER", 9),
            ("livechat_operator_id", "INTEGER", 9),
            ("chatbot_current_step_id", "INTEGER", 9),
            ("livechat_active", "BOOLEAN", True),
            ("livechat_visitor_id", "INTEGER", 9),
            ("livechat_lang_id", "INTEGER", 9),
            ("livechat_agent_requesting_help_history", "INTEGER", 9),
            ("livechat_agent_providing_help_history", "INTEGER", 9),
            ("livechat_status", "TEXT", "need_help"),
            ("livechat_looking_for_help_since_dt", "TEXT", "2025-01-01T11:00:00"),
            ("livechat_outcome", "TEXT", "no_failure"),
            ("livechat_failure", "TEXT", "no_failure"),
            ("livechat_note", "TEXT", "Retain this note"),
            ("livechat_rating", "TEXT", "3"),
            ("livechat_is_escalated", "BOOLEAN", True),
            ("livechat_end_dt", "TEXT", "2025-01-01T11:00:00"),
        ):
            with self.subTest(field=name):
                self.connection.execute("SAVEPOINT livechat")
                self.connection.execute("ALTER TABLE discuss_channel ADD COLUMN " + name + " " + sql_type)
                if sql_type == "BOOLEAN":
                    self.connection.execute("UPDATE discuss_channel SET " + name + "=0")
                self.connection.execute("SAVEPOINT default_state")
                self.apply()
                self.connection.execute("ROLLBACK TO default_state")
                self.connection.execute("RELEASE default_state")
                self.connection.execute("UPDATE discuss_channel SET " + name + "=? WHERE id=42", (value,))
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)
                self.connection.execute("ROLLBACK TO livechat")
                self.connection.execute("RELEASE livechat")

    def test_retains_survivor_and_audience_unchanged_and_replays(self) -> None:
        before = self.all_rows()
        self.apply()
        after = self.all_rows()
        self.assertEqual(after["discuss_channel"], before["discuss_channel"][:1])
        self.assertEqual(after["discuss_channel_member"], before["discuss_channel_member"][:2])
        for table in before.keys() - {"ir_model_data", "discuss_channel", "discuss_channel_member"}:
            self.assertEqual(after[table], before[table])
        self.apply()
        self.assertEqual(self.all_rows(), after)

    def test_reference_and_state_refusals_precede_every_bridge_effect(self) -> None:
        self.livechat_projections()
        changes = (
            "INSERT INTO mail_message VALUES (1,'discuss.channel',42,'history')",
            "INSERT INTO mail_message VALUES (1,'discuss.channel',41,'survivor history')",
            "INSERT INTO mail_message VALUES (1,'discuss.channel.member',53,'member history')",
            "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','chat','discuss.channel',42)",
            "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','member','discuss.channel.member',51)",
            "UPDATE discuss_channel SET description='custom'",
            "UPDATE discuss_channel SET custom_value='custom' WHERE id=42",
            "UPDATE discuss_channel SET name='custom'",
            "UPDATE discuss_channel SET active=0 WHERE id=42",
            "UPDATE discuss_channel_member SET custom_channel_name='custom'",
            "UPDATE discuss_channel_member SET custom_notifications='mentions' WHERE id=53",
            "UPDATE discuss_channel_member SET fold_state='open' WHERE id=54",
            "UPDATE discuss_channel_member SET mute_until_dt='2027-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET seen_message_id=1 WHERE id=51",
            "UPDATE discuss_channel_member SET fetched_message_id=1 WHERE id=53",
            "UPDATE discuss_channel_member SET last_seen_dt='2025-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET unpin_dt='2026-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET partner_id=NULL,guest_id=303 WHERE partner_id=101",
            "INSERT INTO ir_model_fields(name,ttype,store,company_dependent,relation) VALUES ('custom','many2one',1,1,'discuss.channel')",
            "INSERT INTO ir_model_fields(name,ttype,store,company_dependent) VALUES ('custom','reference',1,1)",
            "INSERT INTO deletion_triggers VALUES ('custom_delete')",
        )
        for change in changes:
            with self.subTest(change=change):
                self.connection.execute("SAVEPOINT candidate")
                self.connection.execute(change)
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)
                self.connection.execute("ROLLBACK TO candidate")
                self.connection.execute("RELEASE candidate")

    def test_custom_fk_reference_and_reference_field_refuse(self) -> None:
        self.connection.execute(
            "CREATE TABLE custom_owner (id INTEGER PRIMARY KEY, member_id INTEGER REFERENCES discuss_channel_member(id), link TEXT)"
        )
        for member_id, reference in ((54, None), (51, None), (None, "discuss.channel,42"), (None, "discuss.channel.member,53")):
            with self.subTest(member_id=member_id, reference=reference):
                self.connection.execute("DELETE FROM custom_owner")
                self.connection.execute("INSERT INTO custom_owner VALUES (1,?,?)", (member_id, reference))
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)

    def test_custom_polymorphic_field_and_numeric_model_alias_refuse(self) -> None:
        self.connection.executescript(
            "CREATE TABLE custom_owner (target INTEGER, kind TEXT);"
            "INSERT INTO ir_model_fields(name,ttype,store,company_dependent,relation) VALUES ('target','many2one_reference',1,0,NULL);"
            "INSERT INTO custom_owner VALUES (42,'discuss.channel');"
            "CREATE TABLE mail_alias (alias_model_id INTEGER REFERENCES ir_model(id), alias_force_thread_id INTEGER);"
        )
        for change in ("SELECT 1", "DELETE FROM custom_owner", "INSERT INTO mail_alias VALUES (701,42)"):
            self.connection.execute(change)
            if change == "DELETE FROM custom_owner":
                continue
            before = self.all_rows()
            with self.assertRaises(RuntimeError):
                self.apply()
            self.assertEqual(self.all_rows(), before)

    def test_distinct_audiences_and_no_mail_are_unchanged(self) -> None:
        self.connection.execute("UPDATE discuss_channel_member SET partner_id=202 WHERE id=53")
        self.connection.execute("DELETE FROM discuss_channel_member WHERE id=54")
        chats = self.all_rows()["discuss_channel"]
        members = self.all_rows()["discuss_channel_member"]
        self.apply()
        self.assertEqual(self.all_rows()["discuss_channel"], chats)
        self.assertEqual(self.all_rows()["discuss_channel_member"], members)
        self.connection.execute("DELETE FROM ir_module_module WHERE name='mail'")
        self.apply()
        self.assertEqual(self.all_rows()["discuss_channel"], chats)

    def test_all_groups_preflight_before_any_effect(self) -> None:
        self.connection.executescript(
            "INSERT INTO res_partner(id,name) VALUES (303,'Carol');"
            "INSERT INTO discuss_channel SELECT 43,'Alice, Carol',channel_type,active,'third',description,last_interest_dt,"
            "create_date,write_date,custom_value FROM discuss_channel WHERE id=41;"
            "INSERT INTO discuss_channel SELECT 44,'Alice, Carol',channel_type,active,'fourth',description,last_interest_dt,"
            "create_date,write_date,custom_value FROM discuss_channel WHERE id=41;"
            "INSERT INTO discuss_channel_member(id,channel_id,partner_id,fold_state,new_message_separator,last_interest_dt,create_date) "
            "SELECT id+4,channel_id+2,CASE WHEN partner_id=202 THEN 303 ELSE partner_id END,fold_state,new_message_separator,"
            "last_interest_dt,create_date FROM discuss_channel_member;"
            "INSERT INTO mail_message VALUES (1,'discuss.channel',44,'history');"
        )
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)

    def test_internal_column_collision_refuses_without_effects(self) -> None:
        self.connection.execute("ALTER TABLE discuss_channel ADD COLUMN members TEXT")
        self.connection.execute("UPDATE discuss_channel SET members='custom payload' WHERE id=42")
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)

    def test_properties_and_json_reference_refuse_without_effects(self) -> None:
        self.connection.execute("CREATE TABLE property_owner (definition JSON, value JSON)")
        for definition, value in (
            ([{"name": "chat", "type": "many2one", "comodel": "discuss.channel"}], {"chat": 42}),
            ([], {"reference": "discuss.channel.member,53"}),
        ):
            with self.subTest(value=value):
                self.connection.execute("DELETE FROM property_owner")
                self.connection.execute("INSERT INTO property_owner VALUES (?,?)", (json.dumps(definition), json.dumps(value)))
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)

    def test_default_pin_timestamps_use_visible_state_despite_clock_skew(self) -> None:
        self.connection.execute("UPDATE discuss_channel_member SET unpin_dt='2025-01-01T10:00:02' WHERE partner_id=202")
        self.apply()
        self.assertEqual(len(self.all_rows()["discuss_channel"]), 1)

    def test_virtual_report_metadata_does_not_claim_physical_storage(self) -> None:
        self.connection.executescript(
            "CREATE VIEW im_livechat_report_channel AS SELECT id AS channel_id FROM discuss_channel;"
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model) "
            "VALUES ('channel_id','many2one',1,'discuss.channel','im_livechat.report.channel'),"
            "('channel_id','many2one',1,'discuss.channel','inline.query.report');"
        )
        self.apply()
        self.assertEqual(len(self.all_rows()["discuss_channel"]), 1)

    def test_physical_relations_without_fk_and_materialized_storage_refuse(self) -> None:
        self.connection.executescript(
            "CREATE TABLE unusual_owner (chat_id INTEGER);"
            "INSERT INTO unusual_owner VALUES (42);"
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model) "
            "VALUES ('chat_id','many2one',1,'discuss.channel','different.table.name');"
        )
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)
        self.connection.execute("DELETE FROM unusual_owner")
        self.connection.execute("INSERT INTO materialized_references VALUES ('chat_id')")
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)

    def test_owned_many2many_without_fk_and_unsupported_column_type_refuse(self) -> None:
        self.connection.executescript(
            "CREATE TABLE owned_groups (chat_id INTEGER, group_id INTEGER);"
            "INSERT INTO owned_groups VALUES (42,999);"
            "INSERT INTO ir_model_fields(name,ttype,store,model,relation,relation_table,column1,column2) "
            "VALUES ('group_ids','many2many',1,'discuss.channel','res.groups','owned_groups','chat_id','group_id');"
        )
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)
        self.connection.execute("DELETE FROM owned_groups")
        self.connection.execute("CREATE TABLE unusual_owner (chat_id TEXT)")
        self.connection.execute("INSERT INTO unusual_owner VALUES ('42')")
        self.connection.execute(
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model) "
            "VALUES ('chat_id','many2one',1,'discuss.channel','different.table.name')"
        )
        before = self.all_rows()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.all_rows(), before)

    def test_same_name_text_storage_checks_values_without_claiming_ownership(self) -> None:
        self.connection.executescript(
            "CREATE TABLE notification_history (channel INTEGER REFERENCES discuss_channel(id));"
            "CREATE TABLE unrelated_owner (channel TEXT);"
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model) "
            "VALUES ('channel','many2one',1,'discuss.channel','notification.history');"
            "INSERT INTO ir_model VALUES (700,'res.users');"
        )
        for value in (
            '["synthetic","res.users",42]',
            '["synthetic","res.users",53,"update"]',
            '["synthetic","discuss.channel",999]',
            '["synthetic","discuss.channel.member",999,"update"]',
            '["synthetic","named-topic"]',
        ):
            with self.subTest(value=value):
                self.connection.execute("SAVEPOINT unrelated")
                self.connection.execute("INSERT INTO unrelated_owner VALUES (?)", (value,))
                try:
                    self.test_retains_survivor_and_audience_unchanged_and_replays()
                finally:
                    self.connection.execute("ROLLBACK TO unrelated")
                    self.connection.execute("RELEASE unrelated")
        self.connection.execute('INSERT INTO unrelated_owner VALUES (\'["synthetic","res.users",42]\')')
        for table, value in (
            ("notification_history", 42),
            ("unrelated_owner", "42"),
            ("unrelated_owner", "discuss.channel,42"),
            ("unrelated_owner", '["synthetic","discuss.channel",42]'),
            ("unrelated_owner", '["synthetic","discuss.channel.member",53,"update"]'),
            ("unrelated_owner", '{"nested":{"reference":"discuss.channel,42"}}'),
            ("unrelated_owner", "unrelated text"),
            ("unrelated_owner", "41,42"),
            ("unrelated_owner", "042"),
            ("unrelated_owner", "discuss.channel, 42"),
            ("unrelated_owner", "[42]"),
            ("unrelated_owner", '{"channel_id":42}'),
            ("unrelated_owner", '["synthetic","unknown.model",999]'),
            ("unrelated_owner", '["synthetic","discuss.channel",42'),
            ("unrelated_owner", '["synthetic","discuss.channel,42"]'),
            ("unrelated_owner", '["synthetic","discuss.channel"]'),
            ("unrelated_owner", '["synthetic","res.users",999,"discuss.channel,42"]'),
            ("unrelated_owner", '["discuss.channel","42"]'),
            ("unrelated_owner", '["discuss.channel","res.users",5]'),
            ("unrelated_owner", '["synthetic","42"]'),
            ("unrelated_owner", '["synthetic","res.users",999,"42"]'),
            ("unrelated_owner", '["discuss.channel,42","res.users",999]'),
            ("unrelated_owner", '"\\u0000"'),
        ):
            with self.subTest(table=table, value=value):
                self.connection.execute("SAVEPOINT referenced")
                self.connection.execute("INSERT INTO " + table + " VALUES (?)", (value,))
                try:
                    before = self.all_rows()
                    with self.assertRaises(RuntimeError):
                        self.apply()
                    self.assertEqual(self.all_rows(), before)
                finally:
                    self.connection.execute("ROLLBACK TO referenced")
                    self.connection.execute("RELEASE referenced")


class LunchTests(EmptyChatFixture):
    def setUp(self) -> None:
        super().setUp()
        self.connection.executescript(
            "CREATE TABLE resource_calendar (id INTEGER PRIMARY KEY, two_weeks_calendar BOOLEAN, hours_per_week REAL);"
            "CREATE TABLE resource_calendar_attendance (id INTEGER PRIMARY KEY, calendar_id INTEGER REFERENCES resource_calendar(id), "
            "name TEXT, dayofweek TEXT, hour_from REAL, hour_to REAL, day_period TEXT, duration_hours REAL, duration_days REAL, "
            "display_type TEXT, week_type TEXT, date_from TEXT, date_to TEXT, resource_id INTEGER, custom_value TEXT);"
            "INSERT INTO ir_module_module VALUES ('resource','installed','19.0');"
            "INSERT INTO ir_model VALUES (703,'resource.calendar.attendance');"
            "INSERT INTO resource_calendar VALUES (61,NULL,40);"
            "INSERT INTO resource_calendar_attendance(id,calendar_id,name,dayofweek,hour_from,hour_to,day_period,duration_hours,duration_days) "
            "VALUES (81,61,'Morning','0',8,12,'morning',4,0.5),"
            "(82,61,'Break','0',12,13,'lunch',0,0),(83,61,'Afternoon','0',13,17,'afternoon',4,0.5);"
        )

    def assert_gap_preserved(self) -> dict[str, list[tuple]]:
        before = self.all_rows()
        self.apply()
        after = self.all_rows()
        self.assertEqual(
            after["resource_calendar_attendance"], [row for row in before["resource_calendar_attendance"] if row[0] != 82]
        )
        self.assertEqual(after["resource_calendar"], before["resource_calendar"])
        return after

    def test_gap_work_hours_and_other_bridge_transitions_survive_replay(self) -> None:
        chats = self.all_rows()["discuss_channel"]
        after = self.assert_gap_preserved()
        self.assertEqual(after["discuss_channel"], chats[:1])
        self.apply()
        self.assertEqual(self.all_rows(), after)

    def test_full_day_work_and_unrelated_calendar_are_preserved(self) -> None:
        self.connection.executescript(
            "UPDATE resource_calendar_attendance SET day_period='full_day',duration_days=1 WHERE id=81;"
            "INSERT INTO resource_calendar VALUES (62,1,0);"
            "INSERT INTO resource_calendar_attendance(id,calendar_id,name,display_type,week_type) "
            "VALUES (84,62,'Other week','line_section','1');"
        )
        self.assert_gap_preserved()

    def test_explicit_single_week_preserves_gap(self) -> None:
        self.connection.execute("UPDATE resource_calendar SET two_weeks_calendar=0")
        self.assert_gap_preserved()

    def test_unsupported_calendar_and_lunch_state_refuse_before_all_effects(self) -> None:
        self.connection.execute("DELETE FROM ir_module_module WHERE name='mail'")
        for change in (
            "UPDATE resource_calendar_attendance SET hour_to=14 WHERE id=81",
            "UPDATE resource_calendar SET two_weeks_calendar=1",
            "UPDATE resource_calendar_attendance SET week_type='1' WHERE id=81",
            "UPDATE resource_calendar_attendance SET display_type='line_section' WHERE id=81",
            "UPDATE resource_calendar_attendance SET date_from='2026-01-01' WHERE id=83",
            "UPDATE resource_calendar_attendance SET resource_id=9 WHERE id=82",
            "UPDATE resource_calendar_attendance SET custom_value='retained' WHERE id=82",
            "UPDATE resource_calendar_attendance SET duration_hours=1 WHERE id=82",
            "UPDATE resource_calendar_attendance SET duration_hours=NULL WHERE id=82",
            "UPDATE resource_calendar_attendance SET hour_to=hour_from WHERE id=82",
            "UPDATE resource_calendar_attendance SET day_period='custom' WHERE id=81",
            "INSERT INTO mail_message VALUES (1,'resource.calendar.attendance',82,'history')",
            "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','break','resource.calendar.attendance',82)",
            "INSERT INTO ir_model_fields(name,ttype,store,company_dependent,relation) "
            "VALUES ('custom','many2one',1,1,'resource.calendar.attendance')",
            "INSERT INTO deletion_triggers VALUES ('custom_delete')",
        ):
            with self.subTest(change=change):
                self.connection.execute("SAVEPOINT lunch")
                self.connection.execute(change)
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)
                self.connection.execute("ROLLBACK TO lunch")
                self.connection.execute("RELEASE lunch")

    def test_attendance_reference_storage_refuses(self) -> None:
        self.connection.executescript(
            "CREATE TABLE custom_owner (attendance_id INTEGER REFERENCES resource_calendar_attendance(id), link TEXT, payload JSON);"
            "CREATE TABLE custom_relation (owner INTEGER, attendance INTEGER);"
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model,relation_table,column1,column2) "
            "VALUES ('attendance_ids','many2many',1,'resource.calendar.attendance','custom.owner','custom_relation','owner','attendance');"
        )
        for change in (
            "INSERT INTO custom_owner(attendance_id) VALUES (82)",
            "INSERT INTO custom_owner(link) VALUES ('resource.calendar.attendance,82')",
            "INSERT INTO custom_owner(payload) VALUES ('" + json.dumps({"reference": "resource.calendar.attendance,82"}) + "')",
            "INSERT INTO custom_relation VALUES (1,82)",
            "INSERT INTO materialized_references VALUES ('attendance')",
        ):
            with self.subTest(change=change):
                self.connection.execute("SAVEPOINT reference_case")
                self.connection.execute(change)
                before = self.all_rows()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.all_rows(), before)
                self.connection.execute("ROLLBACK TO reference_case")
                self.connection.execute("RELEASE reference_case")


if __name__ == "__main__":
    unittest.main()
