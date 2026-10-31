from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "prepare_core_references_20.py"
spec = importlib.util.spec_from_file_location("core_references_20", SCRIPT)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def patch_attr(target: Any, name: str, value: Any, *, create: bool = False) -> Any:
    return patch.object(target, name, value, create=create)


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
                json.loads(value) if column[0] == "arch_db" else value
                for column, value in zip(self.cursor.description, row, strict=True)
            )
            for row in self.cursor.fetchall()
        ]


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


if __name__ == "__main__":
    unittest.main()


class DiscussCursor(Cursor):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.connection = connection

    def execute(self, query: str, parameters: tuple = ()) -> Any:
        if "FROM pg_constraint" in query:
            rows = []
            for (table,) in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for foreign_key in self.connection.execute('PRAGMA foreign_key_list("' + table + '")'):
                    if foreign_key[2] in {"discuss_channel", "discuss_channel_member", "ir_model"}:
                        rows.append(("public", table, foreign_key[3], foreign_key[2], foreign_key[4], 1))
            return self._catalog(rows, 6)
        if "FROM information_schema.columns" in query:
            rows = []
            for (table,) in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for column in self.connection.execute('PRAGMA table_info("' + table + '")'):
                    rows.append((table, column[1], "text" if column[2] == "TEXT" else "integer"))
            return self._catalog(rows, 3)
        if "FROM pg_trigger" in query:
            query = "SELECT 1 FROM deletion_triggers"
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


class EmptyChatTests(CoreReferenceFixture):
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
            "CREATE TABLE ir_model_fields (name TEXT, ttype TEXT, store BOOLEAN, company_dependent BOOLEAN, relation TEXT, model TEXT, relation_table TEXT);"
            "CREATE TABLE mail_message (id INTEGER PRIMARY KEY, model TEXT, res_id INTEGER, body TEXT);"
            "CREATE TABLE deletion_triggers (name TEXT);"
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
        self.cursor = DiscussCursor(self.connection)
        patch_attr(bridge, "_identified", self.cursor.identified, create=True).start()

    def all_rows(self) -> dict[str, list[tuple]]:
        tables = [row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: self.connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall() for table in tables}

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
        changes = (
            "INSERT INTO mail_message VALUES (1,'discuss.channel',42,'history')",
            "INSERT INTO mail_message VALUES (1,'discuss.channel',41,'survivor history')",
            "INSERT INTO mail_message VALUES (1,'discuss.channel.member',53,'member history')",
            "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','chat','discuss.channel',42)",
            "INSERT INTO ir_model_data(module,name,model,res_id) VALUES ('custom','member','discuss.channel.member',51)",
            "UPDATE discuss_channel SET description='custom'",
            "UPDATE discuss_channel SET custom_value='custom' WHERE id=42",
            "UPDATE discuss_channel SET name='custom'",
            "UPDATE discuss_channel SET last_interest_dt='2026-01-01' WHERE id=42",
            "UPDATE discuss_channel SET create_date=NULL WHERE id=42",
            "UPDATE discuss_channel SET active=0 WHERE id=42",
            "UPDATE discuss_channel_member SET last_interest_dt='2026-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET custom_channel_name='custom'",
            "UPDATE discuss_channel_member SET custom_notifications='mentions' WHERE id=53",
            "UPDATE discuss_channel_member SET fold_state='open' WHERE id=54",
            "UPDATE discuss_channel_member SET mute_until_dt='2027-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET seen_message_id=1 WHERE id=51",
            "UPDATE discuss_channel_member SET fetched_message_id=1 WHERE id=53",
            "UPDATE discuss_channel_member SET last_seen_dt='2025-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET unpin_dt='2026-01-01' WHERE id=53",
            "UPDATE discuss_channel_member SET guest_id=303 WHERE id=53",
            "INSERT INTO ir_model_fields(name,ttype,store,company_dependent,relation) VALUES ('custom','many2one',1,1,'discuss.channel')",
            "INSERT INTO ir_model_fields(name,ttype,store,relation,model) VALUES ('chat_id','many2one',1,'discuss.channel','custom.owner')",
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
