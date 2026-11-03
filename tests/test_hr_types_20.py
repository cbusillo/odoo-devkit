from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock, patch

from test_core_references_20 import CoreReferenceFixture, Cursor, bridge, patch_attr
from test_upgrade_paths import HELPERS

HR_TYPE_PLAN = bridge._hr_type_plan


class TypeCursor(Cursor):
    def execute(self, query: str, parameters: tuple = ()) -> Any:
        query = query.replace("=ANY(%s)", " IN (SELECT value FROM json_each(%s))")
        parameters = tuple(json.dumps(value) if isinstance(value, list) else value for value in parameters)
        return super().execute(query, parameters)


class NativeUtility(ModuleType):
    def __init__(self, connection: sqlite3.Connection, source: Path) -> None:
        super().__init__("odoo.upgrade.util")
        self.connection = connection
        self.__file__ = str(source / "util/__init__.py")

    def table_exists(self, _cr: Any, table: str) -> bool:
        return self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def column_type(self, _cr: Any, table: str, column: str) -> str | None:
        types = {row[1]: row[2] for row in self.connection.execute('PRAGMA table_info("' + table + '")')}
        return {"INTEGER": "int4", "TEXT": "varchar", "JSON": "jsonb"}.get(types.get(column, ""))

    def column_exists(self, cr: Any, table: str, column: str) -> bool:
        return self.column_type(cr, table, column) is not None

    @staticmethod
    def format_query(_cr: Any, query: str, table: str) -> str:
        return query.replace("{}", '"' + table + '"')

    @staticmethod
    def get_m2m_on(_cr: Any, _table: str) -> list[tuple]:
        return []

    def rename_table(self, _cr: Any, old: str, new: str, *, remove_constraints: bool = True) -> None:
        if remove_constraints:
            raise RuntimeError("Native default would remove custom constraints")
        self.connection.execute('ALTER TABLE "' + old + '" RENAME TO "' + new + '"')

    def rename_model(self, _cr: Any, old: str, new: str, *, rename_table: bool = True) -> None:
        if rename_table:
            raise RuntimeError("Native default would remove custom constraints")
        for table, column in (
            ("ir_model", "model"),
            ("ir_model_fields", "model"),
            ("ir_model_fields", "relation"),
            ("ir_model_data", "model"),
            ("mail_message", "model"),
        ):
            self.connection.execute('UPDATE "' + table + '" SET "' + column + '"=? WHERE "' + column + '"=?', (new, old))
        self.connection.execute(
            "UPDATE ir_model_data SET name=replace(name,?,?) WHERE model IN ('ir.model','ir.model.fields')",
            (old.replace(".", "_"), new.replace(".", "_")),
        )

    def rename_field(self, cr: Any, model: str, old: str, new: str, *, skip_inherit: str) -> None:
        if skip_inherit != "*":
            raise RuntimeError("Fixture requires the explicit admitted owner set")
        self.connection.execute("UPDATE ir_model_fields SET name=? WHERE model=? AND name=?", (new, model, old))
        self.connection.execute("UPDATE ir_model_fields SET related=replace(related,?,?)", (old, new))
        self.connection.execute(
            "UPDATE ir_model_data SET name=? WHERE model='ir.model.fields' AND module!='__export__' "
            "AND res_id IN (SELECT id FROM ir_model_fields WHERE model=? AND name=?)",
            ("field_" + model.replace(".", "_") + "__" + new, model, new),
        )
        table = model.replace(".", "_")
        if self.column_exists(cr, table, old):
            self.connection.execute('ALTER TABLE "' + table + '" RENAME COLUMN "' + old + '" TO "' + new + '"')


class HrTypeTests(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        patch_attr(bridge, "_hr_type_plan", HR_TYPE_PLAN).start()
        self.cursor = TypeCursor(self.connection)
        self.connection.create_function(
            "to_regclass",
            1,
            lambda relation_name: (
                relation_name
                if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (relation_name,)).fetchone()
                else None
            ),
        )
        self.connection.executescript(
            "CREATE TABLE ir_model(id INTEGER PRIMARY KEY,model TEXT,state TEXT);"
            "CREATE TABLE ir_model_fields(id INTEGER PRIMARY KEY,model TEXT,model_id INTEGER,name TEXT,ttype TEXT,relation TEXT,"
            "state TEXT,store BOOLEAN,related TEXT);"
            "CREATE TABLE pg_class(relname TEXT);"
            "CREATE TABLE hr_contract_type(id INTEGER PRIMARY KEY,name JSON,code TEXT,sequence INTEGER CHECK(sequence>=0),"
            "country_id INTEGER,write_uid INTEGER,x_notes TEXT);"
            "CREATE TABLE hr_job(id INTEGER PRIMARY KEY,contract_type_id INTEGER REFERENCES hr_contract_type(id));"
            "CREATE TABLE hr_version(id INTEGER PRIMARY KEY,contract_type_id INTEGER REFERENCES hr_contract_type(id));"
            "CREATE TABLE hr_employee(id INTEGER PRIMARY KEY,version_id INTEGER REFERENCES hr_version(id));"
            "CREATE TABLE business_links(id INTEGER PRIMARY KEY,type_id INTEGER REFERENCES hr_contract_type(id),value TEXT);"
            "CREATE TABLE mail_message(id INTEGER PRIMARY KEY,model TEXT,res_id INTEGER,body TEXT);"
            "INSERT INTO ir_module_module VALUES ('hr','installed','19.0.1.0');"
            "INSERT INTO ir_model VALUES (10,'hr.contract.type','base'),(11,'hr.job','base'),"
            "(12,'hr.version','base'),(13,'hr.employee','base');"
        )
        for identity, code in ((31, "employee"), (32, "old_only")):
            self.connection.execute(
                "INSERT INTO hr_contract_type VALUES (?,?,?,?,NULL,79,'retained custom data')",
                (identity, json.dumps({"en_US": code, "fr_FR": "Custom translation"}), "custom-" + code, identity),
            )
            self.xmlid("hr", "contract_type_" + code, "hr.contract.type", identity, True)
        self.connection.executescript(
            "INSERT INTO hr_job VALUES (21,31);INSERT INTO hr_version VALUES (22,32);INSERT INTO hr_employee VALUES (23,22);"
            "INSERT INTO business_links VALUES (24,32,'retained configuration');"
            "INSERT INTO mail_message VALUES (25,'hr.contract.type',32,'retained history');"
        )
        self.xmlid("__export__", "custom_type", "hr.contract.type", 32, True)
        self.xmlid("custom_mod", "protected_type", "hr.contract.type", 32, True)
        self.xmlid("hr", "model_hr_contract_type", "ir.model", 10, False)
        for identity, name, kind, relation in (
            (50, "id", "integer", None),
            (51, "name", "char", None),
            (52, "code", "char", None),
            (53, "sequence", "integer", None),
            (54, "country_id", "many2one", "res.country"),
        ):
            self.connection.execute(
                "INSERT INTO ir_model_fields VALUES (?,'hr.contract.type',10,?,?,?,'base',1,NULL)",
                (identity, name, kind, relation),
            )
            self.xmlid("hr", "field_hr_contract_type__" + name, "ir.model.fields", identity, False)
        for identity, model, owner, store in (
            (41, "hr.job", 11, True),
            (42, "hr.version", 12, True),
            (43, "hr.employee", 13, False),
        ):
            related = None if store else "version_id.contract_type_id"
            self.connection.execute(
                "INSERT INTO ir_model_fields VALUES (?,?,?,?,?,'hr.contract.type','base',?,?)",
                (identity, model, owner, "contract_type_id", "many2one", store, related),
            )
            self.xmlid("hr", "field_" + model.replace(".", "_") + "__contract_type_id", "ir.model.fields", identity, False)
        self.xmlid("__export__", "retained_field", "ir.model.fields", 42, True)
        source = Path(self.directory.name) / "addons/_checkouts/official/src"
        (source / "util").mkdir(parents=True)
        (source / "util/__init__.py").write_text("")
        source.parent.with_suffix(".odoo-source").write_text(HELPERS["UPGRADE_UTIL_SOURCE"])
        self.utility = NativeUtility(self.connection, source)
        sql_module = ModuleType("odoo.tools.sql")
        sql_module.make_index_name = lambda table, field: table + "_" + field + "_index"
        modules = patch.dict(sys.modules, {"odoo.upgrade.util": self.utility, "odoo.tools.sql": sql_module})
        modules.start()
        self.addCleanup(modules.stop)
        target = Path(self.directory.name) / "types.xml"
        target.write_text('<odoo><data noupdate="1"><record id="contract_type_employee" model="hr.employee.type"/></data></odoo>')
        patch_attr(bridge, "HR_TYPE_DATA_FILE", target).start()

    def snapshot(self) -> dict[str, list[tuple]]:
        tables = [row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: self.connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall() for table in tables}

    def load_native_type_alias(self, name: str) -> tuple:
        model, identity = self.connection.execute(
            "SELECT model,res_id FROM ir_model_data WHERE module='hr' AND name=?", (name,)
        ).fetchone()
        if model != "hr.employee.type":
            raise KeyError(model)
        return self.connection.execute("SELECT * FROM hr_employee_type WHERE id=?", (identity,)).fetchone()

    def test_handoff_keeps_custom_translations_ids_links_history_constraints_and_old_only_rows(self) -> None:
        with self.assertRaises(KeyError):
            self.load_native_type_alias("contract_type_employee")
        original = self.snapshot()
        self.apply()
        after = self.snapshot()
        self.assertEqual(after["hr_employee_type"], original["hr_contract_type"])
        self.assertEqual(self.load_native_type_alias("contract_type_employee"), original["hr_contract_type"][0])
        self.assertEqual(self.load_native_type_alias("contract_type_old_only"), original["hr_contract_type"][1])
        self.assertEqual(
            self.connection.execute(
                "SELECT model,res_id,noupdate FROM ir_model_data WHERE module='custom_mod' AND name='protected_type'"
            ).fetchone(),
            ("hr.employee.type", 32, True),
        )
        for table in ("hr_job", "hr_version", "hr_employee", "business_links"):
            self.assertEqual(after[table], original[table])
        self.assertEqual(
            after["mail_message"][0][0:1] + after["mail_message"][0][2:],
            original["mail_message"][0][0:1] + original["mail_message"][0][2:],
        )
        self.assertEqual([row[0] for row in after["ir_model_fields"]], [row[0] for row in original["ir_model_fields"]])
        self.assertEqual(
            self.connection.execute("SELECT name FROM ir_model_fields WHERE model!='hr.employee.type' ORDER BY id").fetchall(),
            [("employee_type_id",)] * 3,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE hr_employee_type SET sequence=-1 WHERE id=31")
        self.apply()
        self.assertEqual(self.snapshot(), after)

    def test_collisions_rebound_metadata_protected_old_only_and_custom_aliases_refuse_before_effects(self) -> None:
        mutations = (
            "CREATE TABLE hr_employee_type(id INTEGER)",
            "INSERT INTO ir_model VALUES (15,'hr.employee.type','base')",
            "UPDATE ir_model_fields SET relation='wrong' WHERE id=41",
            "UPDATE ir_model_fields SET model_id=10 WHERE id=41",
            "UPDATE ir_model_data SET res_id=42 WHERE name='field_hr_job__contract_type_id'",
            "UPDATE ir_model_data SET noupdate=0 WHERE name='contract_type_old_only'",
            "UPDATE ir_model_data SET name='custom_alias' WHERE name='field_hr_job__contract_type_id'",
            "UPDATE ir_model_data SET noupdate=1 WHERE name='field_hr_job__contract_type_id'",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr','model_hr_employee_type','ir.model',10,0)",
            "INSERT INTO pg_class VALUES ('custom_hr_employee_type_index')",
            "ALTER TABLE hr_version ADD COLUMN employee_type_id INTEGER",
            "ALTER TABLE hr_version ADD COLUMN employee_type TEXT DEFAULT 'employee'",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) "
            "VALUES ('custom_mod','model_hr_contract_type','ir.model',10,0)",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) "
            "VALUES ('custom_mod','field_hr_contract_type__name','ir.model.fields',51,0)",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) "
            "VALUES ('custom_mod','field_hr_job__contract_type_id','ir.model.fields',41,0)",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) "
            "VALUES ('custom_mod','retained_type','hr.contract.type',32,0)",
            "INSERT INTO ir_model_fields VALUES (70,'custom.owner',90,'contract_type_id',"
            "'many2one','hr.contract.type','base',1,NULL)",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.connection.execute("SAVEPOINT unsupported")
                try:
                    self.connection.execute(mutation)
                    before = self.snapshot()
                    with self.assertRaises(RuntimeError):
                        self.apply()
                    self.assertEqual(self.snapshot(), before)
                finally:
                    self.connection.execute("ROLLBACK TO unsupported")
                    self.connection.execute("RELEASE unsupported")

    def test_later_shared_admission_failure_prevents_native_hr_effects(self) -> None:
        before = self.snapshot()
        with (
            patch_attr(bridge, "_admit_mail", Mock(side_effect=RuntimeError("unsupported Mail"))),
            self.assertRaises(RuntimeError),
        ):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_unqualified_metadata_and_physical_joins_refuse_before_effects(self) -> None:
        self.connection.execute(
            "INSERT INTO ir_model_fields VALUES (70,'hr.contract.type',10,'x_groups','many2many','res.groups','manual',0,NULL)"
        )
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        self.connection.execute("DELETE FROM ir_model_fields WHERE id=70")
        self.connection.executescript(
            "CREATE TABLE res_groups(id INTEGER PRIMARY KEY);INSERT INTO res_groups VALUES (1);"
            "CREATE TABLE custom_type_group_rel(type_id INTEGER REFERENCES hr_contract_type(id) ON DELETE CASCADE,"
            "group_id INTEGER REFERENCES res_groups(id));INSERT INTO custom_type_group_rel VALUES (32,1);"
        )
        with patch.object(self.utility, "get_m2m_on", return_value=[("custom_type_group_rel", "type_id", "group_id", "res_groups")]):
            before = self.snapshot()
            with self.assertRaises(RuntimeError):
                self.apply()
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.connection.execute("SELECT * FROM custom_type_group_rel").fetchall(), [(32, 1)])
