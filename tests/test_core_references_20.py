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


class Cursor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.cursor = connection.cursor()

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


class CoreReferenceTests(unittest.TestCase):
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
            "CREATE TABLE ir_model (id INTEGER PRIMARY KEY, model TEXT);"
            "CREATE TABLE ir_rule (id INTEGER PRIMARY KEY, model_id INTEGER, name TEXT, domain_force TEXT, active BOOLEAN, "
            "perm_read BOOLEAN, perm_write BOOLEAN, perm_create BOOLEAN, perm_unlink BOOLEAN);"
            "CREATE TABLE ir_model_access (id INTEGER PRIMARY KEY, model_id INTEGER, name TEXT, group_id INTEGER, active BOOLEAN, "
            "perm_read BOOLEAN, perm_write BOOLEAN, perm_create BOOLEAN, perm_unlink BOOLEAN);"
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

    def test_same_major_is_noop_and_other_crossings_refuse(self) -> None:
        before = self.snapshot()
        bridge.migrate(self.cursor, "20.0.1.3")
        self.assertEqual(self.snapshot(), before)
        for version in (None, "", "18.0.1.3", "21.0.1.3"):
            with self.subTest(version=version), self.assertRaises(RuntimeError):
                bridge.migrate(self.cursor, version)
            self.assertEqual(self.snapshot(), before)

    def test_data_only_grouped_rule_refuses_with_siblings_before_any_bridge_write(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_rule VALUES (23,31,'Retained contact limit',?,1,1,0,0,0)", ("[('id','=',7)]",))
        self.connection.execute("INSERT INTO ir_rule VALUES (29,31,'Source portal limit',?,1,1,0,0,0)", ("[('id','=',9)]",))
        self.connection.executemany("INSERT INTO rule_group_rel VALUES (?,?)", ((23, 67), (29, 10)))
        self.xmlid("fixture", "contact_limit", "ir.rule", 23, True)
        self.xmlid("base", "portal_limit", "ir.rule", 29, False)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','installed','19.0.1.0')")
        before = self.snapshot()
        policies = self.connection.execute("SELECT * FROM ir_rule").fetchall()
        with self.assertRaises(RuntimeError) as refusal:
            self.apply()
        self.assertIn("fixture.contact_limit", str(refusal.exception))
        self.assertIn("base.portal_limit", str(refusal.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.connection.execute("SELECT * FROM ir_rule").fetchall(), policies)

    def test_data_only_access_plan_retains_inactive_acl_and_global_rule(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained write',67,0,0,1,0,0)")
        domain = "[('company_id','in',company_ids)]"
        self.connection.execute("INSERT INTO ir_rule VALUES (23,31,'Retained company',?,0,1,1,0,0)", (domain,))
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
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'No operations',67,1,0,0,0,0)")
        self.connection.execute("INSERT INTO ir_model_access VALUES (16,31,'Unrestricted role',NULL,1,1,0,0,0)")
        before = self.connection.execute("SELECT * FROM ir_model_access").fetchall()
        with self.assertRaises(RuntimeError) as refusal:
            bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertIn("ir.model.access:14", str(refusal.exception))
        self.assertIn("ir.model.access:16", str(refusal.exception))
        self.assertEqual(self.connection.execute("SELECT * FROM ir_model_access").fetchall(), before)

    def test_unowned_acl_is_planned_and_module_owned_policy_requires_its_source_mapping(self) -> None:
        self.connection.execute("INSERT INTO ir_model VALUES (31,'res.partner')")
        self.connection.executemany("INSERT INTO ir_model_access VALUES (?,31,'Retained read',67,1,1,0,0,0)", ((14,), (16,)))
        self.xmlid("base", "source_owned", "ir.model.access", 16, False)
        self.xmlid("fixture", "protected_alias", "ir.model.access", 16, True)
        self.connection.execute("INSERT INTO ir_module_module VALUES ('base','to upgrade','19.0.1.0')")
        plan = bridge.plan_access_transition(bridge.read_access_policies(self.cursor))
        self.assertEqual([record.source.identifier for record in plan.converted], [14])
        self.assertFalse(plan.converted[0].source.aliases)
        self.assertEqual([record.identifier for record in plan.source_owned], [16])
        self.assertEqual(plan.source_owned[0].protected_aliases, ("fixture.protected_alias",))

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
        self.connection.execute("INSERT INTO ir_model_access VALUES (14,31,'Retained security policy',67,1,1,0,0,0)")
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
