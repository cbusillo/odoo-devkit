from __future__ import annotations

import importlib.util
import json
import re
import runpy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "prepare_core_references_20.py"
spec = importlib.util.spec_from_file_location("core_references_20", SCRIPT)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class Cursor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.cursor = connection.cursor()

        def to_regclass(name: str) -> str | None:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            return name if exists else None

        connection.create_function("to_regclass", 1, to_regclass)

    def execute(self, query: str, parameters: tuple = ()) -> Any:
        if "res_id = ANY(%s)" in query:
            return self.cursor.execute(
                query.replace("%s", "?").replace("res_id = ANY(?)", "res_id IN (SELECT value FROM json_each(?))"),
                (parameters[0], json.dumps(parameters[1])),
            )
        if "= ANY(%s)" in query:
            return self.cursor.execute(
                query.replace("= ANY(%s)", "IN (SELECT value FROM json_each(?))"),
                (json.dumps(parameters[0]),),
            )
        if "FROM pg_constraint" in query:
            references = []
            tables = self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table,) in tables:
                fields = self.cursor.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall()
                identity = '"id"' if any(row[1] == "id" for row in fields) else "rowid"
                for foreign_key in self.cursor.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,)).fetchall():
                    if foreign_key[2] != parameters[0]:
                        continue
                    name = table + "_" + foreign_key[3] + "_fk"
                    selection = (
                        "SELECT count(*), json_group_array("
                        + identity
                        + ") FROM "
                        + '"'
                        + table.replace('"', '""')
                        + '" WHERE "'
                        + foreign_key[3].replace('"', '""')
                        + '" IN (SELECT value FROM json_each(%s))'
                    )
                    references.append((table, name, 1, 1, foreign_key[4], selection))
            if not references:
                return self.cursor.execute("SELECT NULL,NULL,NULL,NULL,NULL,NULL WHERE 0")
            return self.cursor.execute(" UNION ALL ".join("SELECT ?,?,?,?,?,?" for _ in references), sum(references, ()))
        if "json_each(" in query:
            translated = re.sub(r"%%|%s", lambda match: "%" if match[0] == "%%" else "?", query)
            return self.cursor.execute(translated, (json.dumps(parameters[0]),))
        if "FROM pg_attribute" in query:
            columns = [
                (table, row[1])
                for table in parameters[0]
                for row in self.cursor.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall()
            ]
            if not columns:
                return self.cursor.execute("SELECT NULL, NULL WHERE 0")
            return self.cursor.execute(" UNION ALL ".join("SELECT ?, ?" for _ in columns), sum(columns, ()))
        return self.cursor.execute(query.replace("%s", "?").replace(" FOR UPDATE", "").replace("::jsonb", ""), parameters)

    def fetchone(self) -> Any:
        row = self.cursor.fetchone()
        if row and len(row) == 2 and self.cursor.description[1][0].startswith("json_group_array"):
            return row[0], json.loads(row[1])
        return row

    def fetchall(self) -> list[tuple]:
        return [
            tuple(
                json.loads(value) if column[0] == "arch_db" else value
                for column, value in zip(self.cursor.description, row, strict=True)
            )
            for row in self.cursor.fetchall()
        ]


class CoreReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE ir_model_data (id INTEGER PRIMARY KEY, module TEXT, name TEXT, model TEXT, res_id INTEGER, "
            "noupdate BOOLEAN, create_uid INTEGER, write_uid INTEGER, create_date TEXT, write_date TEXT, UNIQUE(module,name));"
            "CREATE TABLE res_country (id INTEGER PRIMARY KEY, code TEXT);"
            "CREATE TABLE res_country_state (id INTEGER PRIMARY KEY, country_id INTEGER, code TEXT, name TEXT, write_uid INTEGER);"
            "CREATE TABLE website (id INTEGER PRIMARY KEY, name TEXT, domain TEXT, company_id INTEGER);"
            "CREATE TABLE ir_module_module (name TEXT PRIMARY KEY, state TEXT, latest_version TEXT);"
            "CREATE TABLE ir_model (id INTEGER PRIMARY KEY, model TEXT);"
            "CREATE TABLE ir_model_inherit (model_id INTEGER, parent_id INTEGER, parent_field_id INTEGER);"
            "CREATE TABLE ir_rule (id INTEGER PRIMARY KEY, model_id INTEGER, name TEXT, domain_force TEXT, active BOOLEAN, "
            "perm_read BOOLEAN, perm_write BOOLEAN, perm_create BOOLEAN, perm_unlink BOOLEAN, "
            "create_uid INTEGER, create_date TEXT, write_uid INTEGER, write_date TEXT, global BOOLEAN);"
            "CREATE TABLE ir_model_access (id INTEGER PRIMARY KEY, model_id INTEGER, name TEXT, group_id INTEGER, active BOOLEAN, "
            "perm_read BOOLEAN, perm_write BOOLEAN, perm_create BOOLEAN, perm_unlink BOOLEAN, "
            "create_uid INTEGER, create_date TEXT, write_uid INTEGER, write_date TEXT);"
            "CREATE TABLE rule_group_rel (rule_group_id INTEGER, group_id INTEGER);"
            "CREATE TABLE res_groups (id INTEGER PRIMARY KEY);"
            "INSERT INTO res_groups VALUES (10),(67);"
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
        patch.object(bridge, "STATE_DATA_FILE", target).start()

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
            "ir_model",
            "ir_model_inherit",
            "ir_model_access",
            "ir_rule",
            "rule_group_rel",
            "res_groups",
        )
        return {table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall() for table in tables}

    def apply(self) -> None:
        bridge.migrate(self.cursor, "19.0.1.3")

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

    def test_same_major_refuses_partial_access_conversion_before_any_bridge_write(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute(
            "INSERT INTO ir_model_access(id,model_id,name,group_id,active,perm_read) VALUES (14,31,'Retained read',67,1,1)"
        )
        self.connection.execute(
            "INSERT INTO ir_rule(id,model_id,name,domain_force,active,perm_read) VALUES (14,31,'Unowned inactive limit','[]',0,1)"
        )
        self.xmlid("fixture", "retained_access", "ir.model.access", 14, True)
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as refusal:
            bridge.migrate(self.cursor, "20.0.1.3")
        self.assertIn("fixture.retained_access", str(refusal.exception))
        self.assertIn("ir.rule:14", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_native_access_schema_fences_residue_despite_a_legacy_or_saas_base_version(self) -> None:
        self.connection.execute("CREATE TABLE ir_access (id INTEGER PRIMARY KEY)")
        self.xmlid("fixture", "retained_rule", "ir.rule", 14, True)
        for version in ("19.0.1.0", "saas~20.0.1.0", "20.0.1.0"):
            with self.subTest(version=version):
                self.connection.execute("INSERT OR REPLACE INTO ir_module_module VALUES ('base','installed',?)", (version,))
                before = list(self.connection.iterdump())
                with self.assertRaises(RuntimeError) as refusal:
                    bridge.ensure_native_access_replay(self.cursor)
                self.assertIn("fixture.retained_rule", str(refusal.exception))
                self.assertEqual(list(self.connection.iterdump()), before)

    def test_cleanup_guard_keeps_aliases_even_without_legacy_tables(self) -> None:
        self.xmlid("fixture", "retained_rule", "ir.rule", 14, True)
        self.connection.execute("DROP TABLE ir_model_access")
        self.connection.execute("DROP TABLE ir_rule")
        before = list(self.connection.iterdump())
        with self.assertRaises(RuntimeError) as refusal:
            bridge.ensure_no_legacy_access_residue(self.cursor)
        self.assertIn("fixture.retained_rule", str(refusal.exception))
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_unknown_stored_policy_columns_refuse_inventory_and_cleanup_without_effects(self) -> None:
        for table in ("ir_model_data", "ir_model_access", "ir_rule", "rule_group_rel"):
            with self.subTest(table=table):
                self.connection.execute("SAVEPOINT extended_schema")
                self.connection.execute("ALTER TABLE " + table + " ADD COLUMN x_retained TEXT")
                before = list(self.connection.iterdump())
                for operation in (bridge.read_access_policies, bridge.ensure_no_legacy_access_residue):
                    with self.subTest(operation=operation.__name__):
                        with self.assertRaises(RuntimeError) as refusal:
                            operation(self.cursor)
                        self.assertIn(table + ".x_retained", str(refusal.exception))
                        self.assertEqual(list(self.connection.iterdump()), before)
                self.connection.execute("ROLLBACK TO extended_schema")
                self.connection.execute("RELEASE extended_schema")

    def test_native_cleanup_consumers_refuse_without_changing_records(self) -> None:
        fixtures = (
            ("ir_model_fields", "relation TEXT, model TEXT, state TEXT", "?, 'res.partner', 'manual'", False),
            ("ir_attachment", "res_model TEXT", "?", False),
            ("mail_activity_type", "res_model TEXT", "?", False),
            ("mail_followers", "res_model TEXT", "?", False),
            ("mail_message", "model TEXT", "?", False),
            ("ir_act_window", "res_model TEXT", "?", False),
            ("ir_act_report_xml", "model TEXT", "?", False),
            ("ir_filters", "model_id TEXT", "?", False),
            ("ir_act_server", "model_id INTEGER REFERENCES ir_model(id)", "?", True),
            ("mail_activity", "res_model_id INTEGER REFERENCES ir_model(id)", "?", True),
        )
        self.connection.execute("INSERT INTO ir_model VALUES (31,'ir.rule'), (32,'res.partner')")
        for table, columns, values, model_id in fixtures:
            with self.subTest(table=table):
                self.connection.execute("SAVEPOINT consumer")
                self.connection.execute("CREATE TABLE " + table + " (id INTEGER PRIMARY KEY, " + columns + ")")
                self.connection.execute("INSERT INTO " + table + " VALUES (14," + values + ")", (32 if model_id else "res.partner",))
                bridge.ensure_no_legacy_access_residue(self.cursor)
                self.connection.execute("INSERT INTO " + table + " VALUES (23," + values + ")", (31 if model_id else "ir.rule",))
                before = list(self.connection.iterdump())
                with self.assertRaises(RuntimeError) as refusal:
                    bridge.ensure_no_legacy_access_residue(self.cursor)
                self.assertIn(table + ":23", str(refusal.exception))
                self.assertEqual(list(self.connection.iterdump()), before)
                self.connection.execute("ROLLBACK TO consumer")
                self.connection.execute("RELEASE consumer")

    def test_scheduled_actions_and_custom_model_aliases_are_not_cleanup_casualties(self) -> None:
        self.connection.executescript(
            "INSERT INTO ir_model VALUES (31,'ir.model.access');"
            "CREATE TABLE ir_act_server (id INTEGER PRIMARY KEY, model_id INTEGER REFERENCES ir_model(id));"
            "CREATE TABLE ir_cron (id INTEGER PRIMARY KEY, ir_actions_server_id INTEGER);"
            "INSERT INTO ir_act_server VALUES (14,31); INSERT INTO ir_cron VALUES (23,14);"
            "CREATE TABLE ir_actions (id INTEGER PRIMARY KEY); INSERT INTO ir_actions VALUES (14);"
            "CREATE TABLE ir_act_window (id INTEGER PRIMARY KEY, res_model TEXT);"
            "CREATE TABLE ir_act_report_xml (id INTEGER PRIMARY KEY, model TEXT);"
            "CREATE TABLE ir_filters (id INTEGER PRIMARY KEY, action_id INTEGER, model_id TEXT);"
            "INSERT INTO ir_filters VALUES (43,14,'res.partner');"
            "CREATE TABLE ir_ui_menu (id INTEGER PRIMARY KEY, action TEXT);"
            "INSERT INTO ir_ui_menu VALUES (53,'ir.actions.server,14');"
            "CREATE TABLE ir_actions_todo (id INTEGER PRIMARY KEY, action_id INTEGER REFERENCES ir_actions(id));"
            "INSERT INTO ir_actions_todo VALUES (63,14);"
        )
        self.xmlid("__custom__", "retained_model", "ir.model", 31, True)
        before = list(self.connection.iterdump())
        with self.assertRaises(RuntimeError) as refusal:
            bridge.ensure_no_legacy_access_residue(self.cursor)
        for identity in ("ir_cron:23", "__custom__.retained_model", "ir_filters:43", "ir_ui_menu:53", "ir_actions_todo:63"):
            self.assertIn(identity, str(refusal.exception))
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_unnamed_foreign_key_consumer_refuses_for_every_delete_action_without_effects(self) -> None:
        self.connection.executescript(
            "INSERT INTO ir_model VALUES (31,'ir.rule');"
            "CREATE TABLE ir_model_fields (id INTEGER PRIMARY KEY, model TEXT, relation TEXT, state TEXT);"
            "INSERT INTO ir_model_fields VALUES (41,'ir.rule',NULL,'base');"
            "CREATE TABLE ir_ui_view (id INTEGER PRIMARY KEY, model TEXT);"
            "INSERT INTO ir_ui_view VALUES (51,'ir.rule');"
        )
        self.connection.executescript(
            "CREATE TABLE ir_act_window (id INTEGER PRIMARY KEY, res_model TEXT);"
            "INSERT INTO ir_act_window VALUES (61,'ir.rule');"
            "CREATE TABLE ir_act_report_xml (id INTEGER PRIMARY KEY, model TEXT);"
            "INSERT INTO ir_act_report_xml VALUES (71,'ir.rule');"
        )
        for table, identifier in (
            ("ir_model", 31),
            ("ir_model_fields", 41),
            ("ir_ui_view", 51),
            ("ir_act_window", 61),
            ("ir_act_report_xml", 71),
        ):
            for delete_action in ("CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"):
                with self.subTest(table=table, delete_action=delete_action):
                    self.connection.execute("SAVEPOINT consumer")
                    self.connection.execute(
                        'CREATE TABLE "third_party%source_reference" (id INTEGER PRIMARY KEY, target INTEGER '
                        "REFERENCES " + table + "(id) ON DELETE " + delete_action + ")"
                    )
                    self.connection.execute('INSERT INTO "third_party%source_reference" VALUES (23,?)', (identifier,))
                    before = list(self.connection.iterdump())
                    with self.assertRaises(RuntimeError) as refusal:
                        bridge.ensure_no_legacy_access_residue(self.cursor)
                    self.assertIn("third_party%source_reference:23", str(refusal.exception))
                    self.assertEqual(list(self.connection.iterdump()), before)
                    self.connection.execute("ROLLBACK TO consumer")
                    self.connection.execute("RELEASE consumer")

    def test_base_owned_reflection_is_not_exempted_from_reference_mapping(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'ir.rule')")
        self.xmlid("base", "model_ir_rule", "ir.model", 31, True)
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as refusal:
            bridge.ensure_no_legacy_access_residue(self.cursor)
        self.assertIn("base.model_ir_rule", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_actual_runtime_startup_preserves_normal_legacy_and_fresh_paths_but_refuses_partial_twenty(self) -> None:
        cases = (
            (18, True, True),
            (19, True, True),
            (20, False, False),
            (20, True, False),
            (20, True, True),
            ("unknown", True, False),
        )
        for major, database_exists, legacy in cases:
            with self.subTest(major=major, database_exists=database_exists, legacy=legacy):
                self.connection.execute("SAVEPOINT startup")
                if legacy:
                    self.connection.execute("INSERT INTO ir_model VALUES (31,'ir.rule')")
                    self.xmlid("fixture", "retained_rule", "ir.rule", 14, True)
                before = self.snapshot()
                server, connection = MagicMock(), MagicMock()
                server.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,) if database_exists else None
                connection.cursor.return_value.__enter__.return_value = self.cursor
                postgres = SimpleNamespace(connect=MagicMock(side_effect=(server, connection)))
                odoo = SimpleNamespace(release=SimpleNamespace(version_info=("untrusted_namespace",)))
                release = Path(self.directory.name) / "native-release.py"
                release.write_text(f"version_info = ({major!r},)\n")
                with (
                    patch.dict(sys.modules, {"psycopg2": postgres, "odoo": odoo}),
                    patch.object(bridge, "NATIVE_RELEASE_FILE", release),
                ):
                    arguments = dict(database="fixture", host="db", port=5432, user="fixture", password="synthetic")
                    if type(major) is not int or (major >= 20 and database_exists and legacy):
                        with self.assertRaises(RuntimeError):
                            bridge.prepare_native_access_startup(**arguments)
                    else:
                        bridge.prepare_native_access_startup(**arguments)
                if type(major) is int and major < 20:
                    postgres.connect.assert_not_called()
                elif type(major) is int:
                    for opened in (server, connection) if database_exists else (server,):
                        self.assertLess(
                            opened.mock_calls.index(call.set_session(readonly=True)),
                            opened.mock_calls.index(call.cursor()),
                        )
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO startup")
                self.connection.execute("RELEASE startup")

    def test_native_end_hook_refuses_before_legacy_alias_cleanup(self) -> None:
        self.xmlid("__custom__", "retained_rule", "ir.rule", 14, True)
        before = self.snapshot()
        hook = SCRIPT.with_name("native_upgrade") / "base" / "0.0.0" / "end-00-access.py"
        with self.assertRaises(RuntimeError) as refusal:
            runpy.run_path(str(hook))["migrate"](self.cursor, "19.0.1.3")
        self.assertIn("__custom__.retained_rule", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_native_twenty_without_legacy_tables_replays_without_effects(self) -> None:
        self.connection.execute("DROP TABLE ir_model_access")
        self.connection.execute("DROP TABLE ir_rule")
        before = list(self.connection.iterdump())
        bridge.migrate(self.cursor, "20.0.1.3")
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_clean_same_major_replay_is_noop_and_other_crossings_refuse(self) -> None:
        before = self.snapshot()
        bridge.migrate(self.cursor, "20.0.1.3")
        self.assertEqual(self.snapshot(), before)
        for version in (None, "", "18.0.1.3", "21.0.1.3"):
            with self.subTest(version=version), self.assertRaises(RuntimeError):
                bridge.migrate(self.cursor, version)
            self.assertEqual(self.snapshot(), before)

    def test_data_only_grouped_rule_records_frozen_siblings_without_applying_any_bridge_write(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Custom read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.connection.execute(
            "INSERT INTO ir_rule VALUES (23,31,'Retained contact limit',?,1,1,0,0,0,NULL,NULL,NULL,NULL,NULL)",
            ("[('id','=',7)]",),
        )
        self.connection.execute(
            "INSERT INTO ir_rule VALUES (29,31,'Source portal limit',?,1,1,0,0,0,NULL,NULL,NULL,NULL,NULL)",
            ("[('id','=',9)]",),
        )
        self.connection.executemany("INSERT INTO rule_group_rel VALUES (?,?)", ((23, 67), (29, 10)))
        self.xmlid("fixture", "contact_limit", "ir.rule", 23, True)
        self.xmlid("base", "portal_limit", "ir.rule", 29, False)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        before = self.snapshot()
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        restriction = next(policy for policy in plan.converted if policy.source.source_model == "ir.rule")
        self.assertIsNone(restriction.group_id)
        self.assertEqual(restriction.source.aliases, ("fixture.contact_limit",))
        self.assertEqual(
            {(policy.source_model, policy.identifier) for policy in restriction.dependencies},
            {("ir.rule", 23), ("ir.rule", 29), ("ir.model.access", 14)},
        )
        self.assertEqual([policy.identifier for policy in plan.source_owned], [29])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(RuntimeError) as refusal:
            self.apply()
        self.assertIn("fixture.contact_limit", str(refusal.exception))
        self.assertIn("base.portal_limit", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_grouped_rule_requires_plain_data_only_permission_for_each_group_and_operation(self) -> None:
        source = bridge.AccessPolicy(
            "ir.rule", 23, "Custom limit", "res.partner", (67,), "r", "[('id','=',7)]", True, ("fixture.limit",), (), False
        )
        sibling = source._replace(identifier=29, groups=(10,), aliases=("base.limit",), source_owned=True)
        access = source._replace(
            source_model="ir.model.access", identifier=14, domain="", aliases=("base.permission",), source_owned=True
        )
        for changed_access in (
            access,
            access._replace(source_owned=False, active=False),
            access._replace(source_owned=False, groups=(10,)),
            access._replace(source_owned=False, operation="u"),
            access._replace(source_owned=False, operation=""),
        ):
            with self.subTest(access=changed_access):
                with self.assertRaises(RuntimeError) as refusal:
                    bridge.plan_access_transition((source, sibling, changed_access))
                self.assertIn("fixture.limit", str(refusal.exception))
                self.assertIn("base.permission", str(refusal.exception))
                self.assertIn("base.limit", str(refusal.exception))
        plan = bridge.plan_access_transition((source, sibling, access._replace(source_owned=False)))
        self.assertEqual({policy.source.identifier for policy in plan.converted}, {14, 23})

    def test_grouped_rule_unsupported_interactions_list_source_and_dependencies(self) -> None:
        source = bridge.AccessPolicy(
            "ir.rule", 23, "Custom limit", "res.partner", (67,), "ru", "[('id','=',7)]", True, ("fixture.limit",), (), False
        )
        sibling = source._replace(identifier=29, groups=(10,), aliases=("base.limit",), source_owned=True)
        access = source._replace(source_model="ir.model.access", identifier=14, domain="", aliases=("fixture.permission",))
        for changed_source, changed_sibling in (
            (source, sibling._replace(active=False)),
            (source._replace(active=False), sibling),
            (source, sibling._replace(operation="r")),
            (source, sibling._replace(domain="False")),
            (source, sibling._replace(domain="[] if user.id else [('id','=',7)]")),
            (source, sibling._replace(domain="[*user.domains]")),
            (source, sibling._replace(domain="['|', ('id','=',7)]")),
        ):
            with self.subTest(source=changed_source, sibling=changed_sibling), self.assertRaises(RuntimeError) as refusal:
                bridge.plan_access_transition((changed_source, changed_sibling, access))
            self.assertIn("fixture.limit", str(refusal.exception))
            self.assertIn("base.limit", str(refusal.exception))

    def test_acl_admission_requires_an_active_compiled_rule_covering_every_intersecting_operation(self) -> None:
        access = bridge.AccessPolicy("ir.model.access", 14, "Custom read", "res.partner", (67,), "r", "", True, (), (), False)
        source = access._replace(source_model="ir.rule", identifier=23, domain="[('id','=',7)]")
        sibling = source._replace(identifier=29, groups=(10,), source_owned=True)
        plan = bridge.plan_access_transition((access, source, sibling))
        self.assertEqual([policy.source.identifier for policy in plan.converted], [14, 23])
        self.assertEqual(plan.converted[0].domain, "")
        self.assertEqual([policy.identifier for policy in plan.converted[0].dependencies], [23])
        for changed_access, changed_source, changed_sibling in (
            (access._replace(groups=(11,)), source, sibling),
            (access._replace(operation="ru"), source, sibling._replace(operation="ru")),
            (access, source._replace(active=False), sibling),
        ):
            with self.subTest(access=changed_access, source=changed_source), self.assertRaises(RuntimeError):
                bridge.plan_access_transition((changed_access, changed_source, changed_sibling))

    def test_policy_inventory_keeps_overlapping_identities_and_record_reference_audit(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute(
            "INSERT INTO ir_model_access(id,model_id,name,group_id,active,perm_read) VALUES (14,31,'Retained read',67,1,1)"
        )
        self.connection.execute(
            "INSERT INTO ir_rule(id,model_id,name,domain_force,active,perm_read) VALUES (14,31,'Retained limit','[]',1,1)"
        )
        self.xmlid("fixture", "read_access", "ir.model.access", 14, True)
        self.xmlid("__custom__", "additional_access", "ir.model.access", 14, False)
        self.xmlid("fixture", "read_limit", "ir.rule", 14, True)
        self.connection.execute("UPDATE ir_model_data SET noupdate=NULL WHERE module='__custom__' AND name='additional_access'")
        for table, audit in (
            ("ir_model_access", (37, "2025-01-02 03:04:05", 41, "2025-02-03 04:05:06")),
            ("ir_rule", (None, None, 53, "2025-03-04 05:06:07")),
        ):
            self.connection.execute(
                "UPDATE " + table + " SET create_uid=?,create_date=?,write_uid=?,write_date=? WHERE id=14", audit
            )
        self.connection.execute(
            "UPDATE ir_model_data SET create_date=?,write_date=? WHERE model IN ('ir.model.access','ir.rule')",
            ("2025-04-05 06:07:08", "2025-05-06 07:08:09"),
        )
        self.connection.execute("UPDATE ir_rule SET global=0 WHERE id=14")
        before = self.snapshot()
        policies = bridge.read_access_policies(self.cursor)
        self.assertEqual(
            {(policy.source_model, policy.identifier) for policy in policies}, {("ir.model.access", 14), ("ir.rule", 14)}
        )
        for policy, table in zip(policies, ("ir_model_access", "ir_rule"), strict=True):
            audit = self.connection.execute(
                "SELECT create_uid,create_date,write_uid,write_date FROM " + table + " WHERE id=?", (policy.identifier,)
            ).fetchone()
            self.assertEqual(policy.audit, audit)
            aliases = self.connection.execute(
                "SELECT id,module,name,noupdate,create_uid,create_date,write_uid,write_date FROM ir_model_data "
                "WHERE model=? AND res_id=? ORDER BY module,name",
                (policy.source_model, policy.identifier),
            ).fetchall()
            self.assertEqual([(*alias[:4], *alias.audit) for alias in policy.alias_records], aliases)
            if policy.source_model == "ir.rule":
                self.assertIsNotNone(policy.global_rule)
                self.assertFalse(policy.global_rule)
        self.assertEqual(self.snapshot(), before)

    def test_data_only_access_plan_retains_inactive_acl_and_global_rule(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained write',67,0,0,1,0,0,NULL,NULL,NULL,NULL)")
        domain = "[('company_id','in',company_ids)]"
        self.connection.execute(
            "INSERT INTO ir_rule VALUES (23,31,'Retained company',?,0,1,1,0,0,NULL,NULL,NULL,NULL,NULL)",
            (domain,),
        )
        self.xmlid("fixture", "write_access", "ir.model.access", 14, True)
        self.xmlid("fixture", "company_limit", "ir.rule", 23, True)
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertEqual(len(plan.converted), 2)
        self.assertFalse(plan.source_owned)
        access, rule = plan.converted
        self.assertEqual(
            (access.source.identifier, access.source.model, access.source.aliases), (14, "res.partner", ("fixture.write_access",))
        )
        self.assertFalse(access.source.active)
        self.assertEqual((access.group_id, access.operation, access.domain), (67, "u", ""))
        self.assertFalse(rule.source.active)
        self.assertIsNone(rule.group_id)
        self.assertEqual(rule.domain, domain)
        self.assertEqual(rule.operation, "ru")

    def test_access_refusals_are_aggregated_and_unowned_policies_are_not_dropped(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,NULL,67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.connection.execute("INSERT INTO ir_model_access VALUES (16,31,'Unrestricted role',NULL,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        before = self.connection.execute("SELECT * FROM ir_model_access").fetchall()
        policies = bridge.read_access_policies(self.cursor)
        with self.assertRaises(RuntimeError) as refusal:
            bridge.plan_access_transition(policies)
        for policy in policies:
            self.assertIn(f"{policy.source_model}:{policy.identifier}", str(refusal.exception))
        self.assertEqual(self.connection.execute("SELECT * FROM ir_model_access").fetchall(), before)

    def test_unowned_acl_is_planned_and_module_owned_policy_requires_its_source_mapping(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.executemany(
            "INSERT INTO ir_model_access VALUES (?,31,'Retained read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)",
            ((14,), (16,)),
        )
        self.xmlid("base", "source_owned", "ir.model.access", 16, False)
        self.xmlid("fixture", "protected_alias", "ir.model.access", 16, True)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','to upgrade','19.0.1.0')")
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertEqual([record.source.identifier for record in plan.converted], [14])
        self.assertFalse(plan.converted[0].source.aliases)
        self.assertEqual([record.identifier for record in plan.source_owned], [16])
        self.assertEqual(plan.source_owned[0].protected_aliases, ("fixture.protected_alias",))

    def test_data_only_zero_acls_keep_references_and_remain_unapplied(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute(
            "INSERT INTO ir_model_access VALUES (14,31,'Retained zero operations',67,1,0,0,0,0,NULL,NULL,NULL,NULL)"
        )
        self.xmlid("__custom__", "retained_zero", "ir.model.access", 14, True)
        for group in (None, 67):
            for active in (False, True):
                with self.subTest(group=group, active=active):
                    self.connection.execute("UPDATE ir_model_access SET group_id=?,active=? WHERE id=14", (group, active))
                    before = self.snapshot()
                    policies = bridge.read_access_policies(self.cursor)
                    plan = bridge.plan_access_transition(policies)
                    self.assertEqual(len(plan.converted), len(policies))
                    self.assertFalse(plan.source_owned)
                    self.assertEqual(plan.converted[0].source, policies[0])
                    self.assertEqual(plan.converted[0].group_id, group)
                    with self.assertRaises(RuntimeError) as refusal:
                        self.apply()
                    self.assertIn(policies[0].aliases[0], str(refusal.exception))
                    self.assertEqual(self.snapshot(), before)

    def test_source_owned_zero_operation_policies_refuse_before_bridge_writes(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        self.connection.execute(
            "INSERT INTO ir_model_access VALUES (14,31,'Source zero operations',67,1,0,0,0,0,NULL,NULL,NULL,NULL)"
        )
        self.connection.execute("INSERT INTO ir_model_access VALUES (16,31,'Source read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.xmlid("base", "no_operations", "ir.model.access", 14, False)
        self.xmlid("base", "source_read", "ir.model.access", 16, False)
        for retained_alias in (False, True):
            if retained_alias:
                self.xmlid("fixture", "retained_empty", "ir.model.access", 14, True)
            for active in (True, False):
                for protected in (True, False):
                    self.connection.execute("SAVEPOINT zero_operations")
                    with self.subTest(retained_alias=retained_alias, active=active, protected=protected):
                        self.connection.execute("UPDATE ir_model_access SET active=? WHERE id=14", (active,))
                        self.connection.execute(
                            "UPDATE ir_model_data SET noupdate=? WHERE module='base' AND name='no_operations'", (protected,)
                        )
                        before = self.snapshot()
                        with self.assertRaises(RuntimeError) as refusal:
                            bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
                        self.assertIn("base.no_operations", str(refusal.exception))
                        if retained_alias:
                            self.assertIn("fixture.retained_empty", str(refusal.exception))
                        with self.assertRaises(RuntimeError):
                            self.apply()
                        self.assertEqual(self.snapshot(), before)
                    self.connection.execute("ROLLBACK TO zero_operations")
                    self.connection.execute("RELEASE zero_operations")

        self.connection.execute("UPDATE ir_model_access SET perm_read=1 WHERE id=14")
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertEqual({policy.identifier for policy in plan.source_owned}, {14, 16})

    def test_zero_rule_keeps_its_unused_domain_and_refuses_mapping(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute(
            "INSERT INTO ir_rule VALUES (23,31,'Retained unused domain',?,1,0,0,0,0,NULL,NULL,NULL,NULL,NULL)",
            ("[('name','=','retained')]",),
        )
        self.xmlid("__custom__", "retained_zero_rule", "ir.rule", 23, True)
        policies = bridge.read_access_policies(self.cursor)
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as refusal:
            bridge.plan_access_transition(policies)
        self.assertIn(policies[0].aliases[0], str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_source_owned_shapes_require_the_same_explicit_native_mapping(self) -> None:
        self.connection.executemany("INSERT INTO ir_model VALUES (?,?)", ((31, "fixture.child"), (32, "res.partner")))
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Source read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.xmlid("base", "source_read", "ir.model.access", 14, False)
        for statement, reason in (
            ("UPDATE ir_model SET model='ir.rule' WHERE id=31", "merged security-model"),
            ("INSERT INTO ir_model_inherit VALUES (31,32,71)", "delegated parents"),
            ("UPDATE ir_model_access SET group_id=NULL", "groupless permission"),
        ):
            self.connection.execute("SAVEPOINT source_shape")
            with self.subTest(reason=reason):
                self.connection.execute(statement)
                before = self.snapshot()
                with self.assertRaises(RuntimeError) as refusal:
                    bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
                self.assertIn("base.source_read", str(refusal.exception))
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
            self.connection.execute("ROLLBACK TO source_shape")
            self.connection.execute("RELEASE source_shape")

    def test_unapplied_access_plans_refuse_before_any_bridge_write(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        self.connection.executemany(
            "INSERT INTO ir_model_access VALUES (?,31,'Retained read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)",
            ((14,), (16,)),
        )
        self.xmlid("base", "source_read", "ir.model.access", 16, False)
        self.xmlid("fixture", "custom_read", "ir.model.access", 14, True)
        before = self.snapshot()
        bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        with self.assertRaises(RuntimeError) as refusal:
            self.apply()
        self.assertIn("base.source_read", str(refusal.exception))
        self.assertIn("fixture.custom_read", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_dangling_policy_alias_or_group_relation_refuses_before_any_bridge_write(self) -> None:
        for statement in (
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('fixture','missing','ir.rule',93,1)",
            "INSERT INTO rule_group_rel VALUES (93,67)",
        ):
            with self.subTest(statement=statement):
                self.connection.execute("SAVEPOINT dangling")
                self.connection.execute(statement)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO dangling")
                self.connection.execute("RELEASE dangling")

    def test_missing_group_and_merged_security_model_refuse(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'ir.rule')")
        self.connection.execute(
            "INSERT INTO ir_model_access VALUES (14,31,'Retained security policy',67,1,1,0,0,0,NULL,NULL,NULL,NULL)"
        )
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        self.connection.execute("UPDATE ir_model SET model='res.partner'")
        self.connection.execute("DELETE FROM res_groups WHERE id=67")
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_data_only_acl_refuses_active_or_inactive_module_grouped_siblings(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.xmlid("fixture", "contact_access", "ir.model.access", 14, True)
        self.connection.execute(
            "INSERT INTO ir_rule VALUES (29,31,'Source portal limit',?,1,1,0,0,0,NULL,NULL,NULL,NULL,NULL)",
            ("[('id','=',9)]",),
        )
        self.connection.execute("INSERT INTO rule_group_rel VALUES (29,10)")
        self.xmlid("base", "portal_limit", "ir.rule", 29, False)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        for active in (True, False):
            self.connection.execute("UPDATE ir_rule SET active=?", (active,))
            before = self.snapshot()
            with self.subTest(active=active):
                with self.assertRaises(RuntimeError) as refusal:
                    self.apply()
                self.assertIn("fixture.contact_access", str(refusal.exception))
                self.assertIn("base.portal_limit", str(refusal.exception))
                self.assertEqual(self.snapshot(), before)
        self.connection.execute("UPDATE ir_model_access SET perm_read=0,perm_write=1")
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertEqual(len(plan.converted), 1)
        self.assertEqual(plan.converted[0].source.identifier, 14)

    def test_nameless_rule_refuses_regardless_of_ownership_before_bridge_writes(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_rule VALUES (23,31,NULL,'[]',1,1,0,0,0,NULL,NULL,NULL,NULL,NULL)")
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        for module in ("fixture", "base"):
            self.connection.execute("SAVEPOINT nameless")
            with self.subTest(module=module):
                self.xmlid(module, "nameless_rule", "ir.rule", 23, False)
                before = self.snapshot()
                with self.assertRaises(RuntimeError) as refusal:
                    bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
                self.assertIn(module + ".nameless_rule", str(refusal.exception))
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
            self.connection.execute("ROLLBACK TO nameless")
            self.connection.execute("RELEASE nameless")

    def test_delegated_parent_requires_native_permission_and_rule_mapping(self) -> None:
        self.connection.executemany("INSERT INTO ir_model VALUES (?,?)", ((31, "fixture.child"), (32, "res.partner")))
        self.connection.execute("INSERT INTO ir_model_inherit VALUES (31,32,71)")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained child read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.xmlid("fixture", "child_read", "ir.model.access", 14, True)
        before = self.snapshot()
        with self.assertRaises(RuntimeError) as refusal:
            self.apply()
        self.assertIn("fixture.child_read", str(refusal.exception))
        self.assertIn("res.partner", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)

    def test_installed_customization_modules_do_not_hide_unsafe_data_only_acl(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained read',67,1,1,0,0,0,NULL,NULL,NULL,NULL)")
        self.connection.execute("INSERT INTO ir_rule VALUES (29,31,'Source portal limit','[]',1,1,0,0,0,NULL,NULL,NULL,NULL,NULL)")
        self.connection.execute("INSERT INTO rule_group_rel VALUES (29,10)")
        self.xmlid("base", "portal_limit", "ir.rule", 29, False)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        for module in ("studio_customization", "__export__", "__custom__"):
            with self.subTest(module=module):
                self.connection.execute("SAVEPOINT customized")
                self.xmlid(module, "retained_read", "ir.model.access", 14, True)
                self.connection.execute("INSERT INTO ir_module_module VALUES (?,'installed','19.0.1.0')", (module,))
                before = self.snapshot()
                with self.assertRaises(RuntimeError) as refusal:
                    self.apply()
                self.assertIn(module + ".retained_read", str(refusal.exception))
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("ROLLBACK TO customized")
                self.connection.execute("RELEASE customized")

    def bank_form(self, state: str = "installed") -> int:
        self.connection.execute(
            "CREATE TABLE ir_ui_view (id INTEGER PRIMARY KEY, model TEXT, inherit_id INTEGER, mode TEXT, arch_db TEXT, "
            "arch_prev TEXT, arch_updated BOOLEAN, active BOOLEAN, priority INTEGER, write_uid INTEGER)"
        )
        parent = self.connection.execute("INSERT INTO ir_ui_view(model) VALUES ('res.partner.bank')").lastrowid
        self.xmlid("base", "view_partner_bank_form", "ir.ui.view", parent, False)
        child = self.connection.execute(
            "INSERT INTO ir_ui_view VALUES (NULL,'res.partner.bank',?,'primary',?,'Retained history',1,1,14,71)",
            (
                parent,
                json.dumps(
                    {
                        "en_US": f"<data><xpath {bridge.BANK_SELECTOR_19}/><separator string='Custom'/></data>",
                        "fr_FR": f"<data><xpath {bridge.BANK_SELECTOR_19}/><separator string='Personnalise'/></data>",
                    }
                ),
            ),
        ).lastrowid
        self.xmlid("account", "view_partner_bank_form_inherit_account", "ir.ui.view", child, False)
        self.xmlid("custom", "retained_bank", "ir.ui.view", child, True)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('account',?,'19.0.1.0')", (state,))
        return child

    def bank_snapshot(self) -> list[tuple]:
        return self.connection.execute("SELECT * FROM ir_ui_view ORDER BY id").fetchall()

    def test_bank_selector_transition_preserves_custom_languages_metadata_and_replay(self) -> None:
        child = self.bank_form()
        before = self.bank_snapshot()
        aliases = self.connection.execute("SELECT * FROM ir_model_data WHERE model='ir.ui.view'").fetchall()
        self.apply()
        after = self.bank_snapshot()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1][:4] + after[1][5:], before[1][:4] + before[1][5:])
        for language, arch in json.loads(before[1][4]).items():
            self.assertEqual(json.loads(after[1][4])[language], arch.replace(bridge.BANK_SELECTOR_19, bridge.BANK_SELECTOR_20))
        self.assertEqual(self.connection.execute("SELECT * FROM ir_model_data WHERE model='ir.ui.view'").fetchall(), aliases)
        self.assertEqual(self.connection.execute("SELECT id FROM ir_ui_view WHERE inherit_id IS NOT NULL").fetchone()[0], child)
        first = self.snapshot()
        self.apply()
        self.assertEqual(self.bank_snapshot(), after)
        self.assertEqual(self.snapshot(), first)

    def test_unsupported_bank_sources_refuse_before_any_effect(self) -> None:
        child = self.bank_form()
        changes = (
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='account'", ()),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='base' AND name='view_partner_bank_form'", ()),
            ("DELETE FROM ir_model_data WHERE module='base' AND name='view_partner_bank_form'", ()),
            ("DELETE FROM ir_model_data WHERE module='account'", ()),
            ("UPDATE ir_model_data SET model='wrong' WHERE module='account'", ()),
            ("UPDATE ir_model_data SET res_id=-1 WHERE module='account'", ()),
            ("UPDATE ir_ui_view SET inherit_id=NULL WHERE id=?", (child,)),
            ("UPDATE ir_ui_view SET mode='extension' WHERE id=?", (child,)),
            ("UPDATE ir_ui_view SET model='wrong' WHERE id=?", (child,)),
        )
        for statement, parameters in changes:
            with self.subTest(statement=statement):
                self.connection.execute("SAVEPOINT bank")
                self.connection.execute(statement, parameters)
                before, rows = self.snapshot(), self.bank_snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.bank_snapshot(), rows)
                self.connection.execute("ROLLBACK TO bank")
                self.connection.execute("RELEASE bank")

    def test_uninstalled_account_leaves_its_bank_view_unchanged(self) -> None:
        self.bank_form(state="uninstalled")
        before = self.bank_snapshot()
        self.apply()
        self.assertEqual(self.bank_snapshot(), before)

    def test_missing_duplicate_and_mixed_language_selectors_refuse_without_effects(self) -> None:
        child = self.bank_form()
        old = f"<data><xpath {bridge.BANK_SELECTOR_19}/></data>"
        new = old.replace(bridge.BANK_SELECTOR_19, bridge.BANK_SELECTOR_20)
        for architectures in ({}, {"en_US": "<data/>"}, {"en_US": old + old}, {"en_US": old + new}, {"en_US": old, "fr_FR": new}):
            with self.subTest(architectures=architectures):
                self.connection.execute("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps(architectures), child))
                before, rows = self.snapshot(), self.bank_snapshot()
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.bank_snapshot(), rows)


if __name__ == "__main__":
    unittest.main()
