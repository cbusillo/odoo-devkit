from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from test_core_action_20 import ActionCursor
from test_core_references_20 import CoreReferenceFixture, bridge, patch_attr


class WindowActionCursor(ActionCursor):
    def fetchall(self) -> list[tuple]:
        return [
            tuple(
                json.loads(value) if column[0] == "help" and isinstance(value, str) else value
                for column, value in zip(self.cursor.description, row, strict=True)
            )
            for row in super().fetchall()
        ]


class HrOrgChartActionFixture(CoreReferenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.cursor = WindowActionCursor(self.connection)
        self.view_plan_patch = patch_attr(bridge, "_hr_org_chart_view_plan", Mock(return_value=None))
        self.view_plan_patch.start()
        self.name = "action_hr_employee_public_org_chart"
        self.employee_model = "hr.employee.public"
        self.native = {
            "name": "Organization",
            "res_model": self.employee_model,
            "path": "org-chart-public",
            "view_mode": "hierarchy,kanban,list,form,graph,pivot",
            "mobile_view_mode": "hierarchy",
            "domain": "[]",
            "context": "{'chat_icon': True}",
        }
        root = ElementTree.Element("odoo")
        data = ElementTree.SubElement(root, "data")
        record = ElementTree.SubElement(data, "record", id=self.name, model="ir.actions.act_window")
        for name, value in self.native.items():
            ElementTree.SubElement(record, "field", name=name).text = value
        ElementTree.SubElement(record, "field", name="view_id", eval="False")
        help_field = ElementTree.SubElement(record, "field", name="help", type="html")
        self.help = '<p class="o_view_nocontent_smiling_face">Create an employee.</p><p>Employee information.</p>'
        help_field.extend(ElementTree.fromstring("<div>" + self.help + "</div>"))
        link = ElementTree.SubElement(data, "record", id="act_hr_employee_public_hierarchy_view", model="ir.actions.act_window.view")
        ElementTree.SubElement(link, "field", name="sequence", eval="16")
        ElementTree.SubElement(link, "field", name="view_mode").text = "hierarchy"
        ElementTree.SubElement(link, "field", name="act_window_id", ref="hr.hr_employee_public_action")
        self.target = Path(self.directory.name) / "hr.xml"
        ElementTree.ElementTree(root).write(self.target)
        patch_attr(bridge, "HR_ORG_CHART_DATA_FILE", self.target).start()
        self.connection.executescript(
            "INSERT INTO ir_module_module VALUES ('hr_org_chart','installed','19.0.1.0');"
            "INSERT INTO ir_module_module VALUES ('hr','installed','19.0.1.0');"
            "CREATE TABLE ir_act_window (id INTEGER PRIMARY KEY, name TEXT, type TEXT, res_model TEXT, path TEXT UNIQUE, "
            "view_mode TEXT, mobile_view_mode TEXT, domain TEXT, context TEXT, view_id INTEGER, search_view_id INTEGER, "
            "help TEXT, target TEXT, binding_model_id INTEGER, binding_type TEXT, binding_view_types TEXT, "
            'res_id INTEGER, usage TEXT, "limit" INTEGER, filter BOOLEAN, cache BOOLEAN, create_uid INTEGER, write_uid INTEGER);'
            "CREATE TABLE other_actions (storage TEXT, id INTEGER, path TEXT);"
            "CREATE VIEW ir_actions AS SELECT 'ir_act_window' AS storage,id,path FROM ir_act_window UNION ALL SELECT * FROM other_actions;"
            "CREATE TABLE ir_act_window_group_rel (act_id INTEGER, gid INTEGER);"
            "CREATE TABLE ir_act_window_view (id INTEGER PRIMARY KEY, act_window_id INTEGER, view_id INTEGER, "
            "sequence INTEGER, view_mode TEXT, multi BOOLEAN, UNIQUE(act_window_id,view_mode));"
            "CREATE TABLE action_links (action INTEGER REFERENCES ir_act_window(id), xmlid TEXT, url TEXT);"
        )
        self.action = self.connection.execute(
            "INSERT INTO ir_act_window(name,type,res_model,path,view_mode,mobile_view_mode,domain,context,help,target,"
            'binding_type,binding_view_types,res_id,"limit",filter,cache,create_uid,write_uid) '
            "VALUES (?,'ir.actions.act_window',?,?,?,'kanban',?,?,?,'current','action','list,form',0,80,0,1,43,59)",
            (
                json.dumps({"en_US": self.native["name"]}),
                self.native["res_model"],
                self.native["path"],
                self.native["view_mode"],
                self.native["domain"],
                self.native["context"],
                json.dumps({"en_US": self.help}),
            ),
        ).lastrowid
        assert self.action is not None
        self.xmlid("hr_org_chart", self.name, "ir.actions.act_window", self.action, False)
        self.connection.execute("INSERT INTO ir_act_window(id,path) VALUES (200,'employees')")
        self.xmlid("hr", "hr_employee_public_action", "ir.actions.act_window", 200, False)
        self.connection.execute("INSERT INTO ir_act_window_view VALUES (300,200,NULL,16,'hierarchy',0)")
        self.xmlid("hr_org_chart", "act_hr_employee_public_hierarchy_view", "ir.actions.act_window.view", 300, False)
        self.connection.execute(
            "INSERT INTO action_links VALUES (?,?,?)", (self.action, f"hr_org_chart.{self.name}", "/odoo/" + self.native["path"])
        )
        self.add_private_and_department()

    def add_private_and_department(self) -> None:
        self.private_name = "action_hr_employee_org_chart"
        self.private_action = 201
        self.search_view = 900
        self.connection.execute("CREATE TABLE ir_ui_view(id INTEGER PRIMARY KEY, model TEXT, type TEXT)")
        self.connection.execute("INSERT INTO ir_ui_view VALUES (?,'hr.employee','search')", (self.search_view,))
        self.xmlid("hr", "view_employee_filter", "ir.ui.view", self.search_view, False)
        self.connection.execute(
            "UPDATE ir_act_window SET type='ir.actions.act_window',res_model=? WHERE id=200", (self.employee_model,)
        )
        document = ElementTree.parse(self.target)
        public = document.find(".//record[@model='ir.actions.act_window']")
        assert public is not None
        private = copy.deepcopy(public)
        private.set("id", self.private_name)
        self.private_native = self.native | {
            "res_model": "hr.employee",
            "path": "org-chart",
            "view_mode": "hierarchy,kanban,list,form,activity,graph,pivot",
        }
        for name, value in self.private_native.items():
            field = private.find(f"field[@name='{name}']")
            assert field is not None
            field.text = value
        ElementTree.SubElement(private, "field", name="search_view_id", ref="hr.view_employee_filter")
        root = ElementTree.Element("odoo")
        root.append(private)
        self.private_target = Path(self.directory.name) / "hr_employee_views.xml"
        self.department_target = Path(self.directory.name) / "hr_department_views.xml"
        self.cohort_links = [
            ("act_hr_employee_public_hierarchy_view", 300, 200, 16, self.target),
            ("act_open_view_employee_list_my_hierarchy_view", 310, 210, 16, self.private_target),
            ("act_hr_department_hierarchy_view_tree_action", 320, 220, 25, self.department_target),
            ("act_hr_department_hierarchy_view_kanban_action", 330, 230, 25, self.department_target),
        ]
        parents = {
            210: ("open_view_employee_list_my", "hr.employee"),
            220: ("hr_department_tree_action", "hr.department"),
            230: ("hr_department_kanban_action", "hr.department"),
        }
        department = ElementTree.Element("odoo")
        for name, identity, parent, sequence, _path in self.cohort_links[1:]:
            parent_name, model = parents[parent]
            self.connection.execute(
                "INSERT INTO ir_act_window(id,type,res_model) VALUES (?,'ir.actions.act_window',?)", (parent, model)
            )
            self.xmlid("hr", parent_name, "ir.actions.act_window", parent, False)
            self.connection.execute(
                "INSERT INTO ir_act_window_view VALUES (?,?,NULL,?,'hierarchy',NULL)", (identity, parent, sequence)
            )
            self.xmlid("hr_org_chart", name, "ir.actions.act_window.view", identity, False)
            link = ElementTree.SubElement(
                root if parent == 210 else department, "record", id=name, model="ir.actions.act_window.view"
            )
            ElementTree.SubElement(link, "field", name="sequence", eval=str(sequence))
            ElementTree.SubElement(link, "field", name="view_mode").text = "hierarchy"
            ElementTree.SubElement(link, "field", name="act_window_id", ref="hr." + parent_name)
        ElementTree.ElementTree(root).write(self.private_target)
        ElementTree.ElementTree(department).write(self.department_target)
        patch_attr(bridge, "HR_EMPLOYEE_DATA_FILE", self.private_target).start()
        patch_attr(bridge, "HR_DEPARTMENT_DATA_FILE", self.department_target).start()
        columns = [column[1] for column in self.connection.execute("PRAGMA table_info(ir_act_window)")]
        values = dict(
            zip(columns, self.connection.execute("SELECT * FROM ir_act_window WHERE id=?", (self.action,)).fetchone(), strict=True)
        )
        values.update(
            id=self.private_action,
            res_model=self.private_native["res_model"],
            path=self.private_native["path"],
            view_mode=self.private_native["view_mode"],
            mobile_view_mode=self.private_native["mobile_view_mode"],
            search_view_id=self.search_view,
        )
        self.connection.execute(
            "INSERT INTO ir_act_window ("
            + ",".join('"' + column + '"' for column in columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(values[column] for column in columns),
        )
        self.xmlid("hr_org_chart", self.private_name, "ir.actions.act_window", self.private_action, False)
        self.connection.execute(
            "INSERT INTO action_links VALUES (?,?,?)", (self.private_action, "hr_org_chart." + self.private_name, "/odoo/org-chart")
        )
        for parent in parents:
            modes = ("list", "form", "kanban", "activity", "graph", "pivot") if parent == 210 else ("list", "form", "kanban")
            for index, mode in enumerate(modes):
                self.connection.execute(
                    "INSERT INTO ir_act_window_view(act_window_id,view_id,sequence,view_mode) VALUES (?,NULL,?,?)",
                    (parent, 10 + index * 5, mode),
                )

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in (
                "ir_act_window",
                "other_actions",
                "action_links",
                "ir_act_window_group_rel",
                "ir_act_window_view",
                "ir_ui_view",
            )
        }

    def native_load(self) -> None:
        target = bridge._reference(self.cursor, "hr", self.name)
        if target is None:
            self.connection.execute("INSERT INTO ir_act_window(path) VALUES (?)", (self.native["path"],))
            return
        self.assertFalse(target.noupdate)
        self.connection.execute(
            "UPDATE ir_act_window SET mobile_view_mode=? WHERE id=?", (self.native["mobile_view_mode"], target.record_id)
        )
        link = bridge._reference(self.cursor, "hr", "act_hr_employee_public_hierarchy_view")
        if link is None:
            self.connection.execute("INSERT INTO ir_act_window_view(act_window_id,sequence,view_mode) VALUES (200,16,'hierarchy')")
        else:
            self.assertEqual(
                self.connection.execute(
                    "SELECT act_window_id,view_mode FROM ir_act_window_view WHERE id=?", (link.record_id,)
                ).fetchone(),
                (200, "hierarchy"),
            )

    def assert_refuses_without_effects(self) -> None:
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.snapshot(), before)


class HrOrgChartActionTests(HrOrgChartActionFixture):
    def test_diagnostic_checks_every_org_chart_piece_but_skips_all_moves_after_one_failure(self) -> None:
        report = Path(self.directory.name) / "diagnostic.jsonl"
        identity = self.cohort_links[0][1]
        self.connection.execute("UPDATE ir_act_window_view SET sequence=99 WHERE id=?", (identity,))
        before = self.snapshot()
        views = Mock(return_value=None)
        with (
            patch.dict(
                os.environ,
                {
                    "ODOO_MIGRATION_DIAGNOSTIC_REPORT": str(report),
                    "ODOO_COPY_PURPOSE": "migration",
                    "PLATFORM_INSTANCE": "rehearsal",
                    "OPENUPGRADE_TARGET_VERSION": "20.0",
                },
            ),
            patch_attr(bridge, "_hr_org_chart_view_plan", views),
        ):
            self.apply()
        after = self.snapshot()
        for table in ("ir_act_window", "ir_act_window_view", "action_links"):
            self.assertEqual(after[table], before[table])
        for snapshot in (before, after):
            snapshot["ir_model_data"] = [row for row in snapshot["ir_model_data"] if row[1] in {"hr", "hr_org_chart"}]
        self.assertEqual(after["ir_model_data"], before["ir_model_data"])
        views.assert_called_once_with(self.cursor)
        events = [json.loads(line) for line in report.read_text().splitlines()]
        self.assertIn("Org Chart changes", {event["check"] for event in events if event["status"] == "blocked"})

    def test_all_moved_actions_and_links_reuse_preserved_rows_before_native_creation(self) -> None:
        components = [
            (self.private_name, self.private_action),
            (self.name, self.action),
        ]
        before = self.snapshot()
        self.apply()
        prepared = self.snapshot()
        self.assertEqual(
            {key: value for key, value in prepared.items() if key != "ir_model_data"},
            {key: value for key, value in before.items() if key != "ir_model_data"},
        )
        self.apply()
        self.assertEqual(self.snapshot(), prepared)
        for name, identity in components:
            with self.subTest(prepared=name):
                target = bridge._reference(self.cursor, "hr", name)
                self.assertIsNotNone(target)
                self.assertEqual(target.record_id, identity)
                self.assertFalse(target.noupdate)
        for name, identity, _parent, _sequence, _source in self.cohort_links:
            with self.subTest(prepared=name):
                target = bridge._reference(self.cursor, "hr", name)
                self.assertIsNotNone(target)
                self.assertEqual(target.record_id, identity)
                self.assertFalse(target.noupdate)
                self.assertTrue(bridge._reference(self.cursor, "hr_org_chart", name).noupdate)

    def test_private_search_and_original_mobile_mode_require_exact_typed_bindings(self) -> None:
        changes = (
            ("UPDATE ir_act_window SET search_view_id=NULL WHERE id=?", (self.private_action,)),
            ("UPDATE ir_act_window SET search_view_id=999 WHERE id=?", (self.private_action,)),
            ("UPDATE ir_act_window SET mobile_view_mode='kanban' WHERE id=?", (self.private_action,)),
            ("UPDATE ir_ui_view SET model='hr.employee.public' WHERE id=?", (self.search_view,)),
            ("UPDATE ir_ui_view SET type='form' WHERE id=?", (self.search_view,)),
            ("DELETE FROM ir_ui_view WHERE id=?", (self.search_view,)),
            ("UPDATE ir_model_data SET model='ir.actions.act_window' WHERE name='view_employee_filter'", ()),
            ("UPDATE ir_model_data SET res_id=999 WHERE name='view_employee_filter'", ()),
            ("DELETE FROM ir_model_data WHERE name='view_employee_filter'", ()),
            ("INSERT INTO ir_act_window_group_rel VALUES (?,7)", (self.private_action,)),
            ("INSERT INTO ir_act_window_view(id,act_window_id,view_id) VALUES (999,?,900)", (self.private_action,)),
            ("INSERT INTO other_actions VALUES ('ir_act_client',999,?)", (self.private_native["path"],)),
            ("UPDATE ir_act_window SET help=? WHERE id=?", (json.dumps({"en_US": "<p>Edited</p>"}), self.private_action)),
            ("UPDATE ir_model_data SET res_id=999 WHERE module='hr_org_chart' AND name=?", (self.private_name,)),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='hr_org_chart' AND name=?", (self.private_name,)),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr',?,'ir.actions.act_window',999,0)",
                (self.private_name,),
            ),
        )
        for query, parameters in changes:
            with self.subTest(query=query):
                self.connection.execute("SAVEPOINT private_action")
                try:
                    self.connection.execute(query, parameters)
                    self.assert_refuses_without_effects()
                finally:
                    self.connection.execute("ROLLBACK TO private_action")
                    self.connection.execute("RELEASE private_action")

    def test_each_hierarchy_link_refuses_changed_owner_parent_order_or_selection_before_effects(self) -> None:
        for name, identity, parent, _sequence, _source in self.cohort_links:
            changes = (
                ("UPDATE ir_act_window_view SET sequence=99 WHERE id=?", (identity,)),
                ("UPDATE ir_act_window_view SET view_id=900 WHERE id=?", (identity,)),
                ("UPDATE ir_act_window_view SET multi=1 WHERE id=?", (identity,)),
                ("UPDATE ir_act_window_view SET act_window_id=999 WHERE id=?", (identity,)),
                ("UPDATE ir_act_window_view SET view_mode='calendar' WHERE id=?", (identity,)),
                ("DELETE FROM ir_act_window_view WHERE id=?", (identity,)),
                ("UPDATE ir_act_window SET res_model='wrong' WHERE id=?", (parent,)),
                ("UPDATE ir_act_window SET type='ir.actions.client' WHERE id=?", (parent,)),
                ("DELETE FROM ir_act_window WHERE id=?", (parent,)),
                ("DELETE FROM ir_model_data WHERE name=?", (name,)),
                ("UPDATE ir_model_data SET noupdate=1 WHERE name=?", (name,)),
                (
                    "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','link','ir.actions.act_window.view',?,0)",
                    (identity,),
                ),
                (
                    "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr',?,'ir.actions.act_window.view',999,0)",
                    (name,),
                ),
            )
            for query, parameters in changes:
                with self.subTest(name=name, query=query):
                    self.connection.execute("SAVEPOINT hierarchy_link")
                    try:
                        self.connection.execute(query, parameters)
                        self.assert_refuses_without_effects()
                    finally:
                        self.connection.execute("ROLLBACK TO hierarchy_link")
                        self.connection.execute("RELEASE hierarchy_link")

    def test_native_private_search_or_hierarchy_definition_drift_refuses_without_effects(self) -> None:
        for path, name, field_name, attribute, value in (
            (self.private_target, self.private_name, "search_view_id", "ref", "hr.changed_filter"),
            *((path, name, "sequence", "eval", "99") for name, _identity, _parent, _sequence, path in self.cohort_links),
        ):
            with self.subTest(name=name, field=field_name):
                original = path.read_bytes()
                document = ElementTree.fromstring(original)
                field = document.find(f".//record[@id='{name}']/field[@name='{field_name}']")
                assert field is not None
                field.set(attribute, value)
                ElementTree.ElementTree(document).write(path)
                try:
                    self.assert_refuses_without_effects()
                finally:
                    path.write_bytes(original)

    def test_prepare_preserves_row_links_and_replays_then_native_loads_mobile_hierarchy(self) -> None:
        for model in ("ir.actions.act_window.view", "ir.actions.todo"):
            self.xmlid("unrelated", model.replace(".", "_"), model, self.action, False)
        before = self.snapshot()
        link_source = bridge._reference(self.cursor, "hr_org_chart", "act_hr_employee_public_hierarchy_view")
        self.apply()
        self.assertEqual(self.snapshot()["ir_act_window"], before["ir_act_window"])
        self.assertEqual(self.snapshot()["ir_act_window_view"], before["ir_act_window_view"])
        self.assertEqual(self.snapshot()["action_links"], before["action_links"])
        prepared = self.snapshot()
        self.apply()
        self.assertEqual(self.snapshot(), prepared)
        for module in ("hr", "hr_org_chart"):
            self.assertEqual(bridge._reference(self.cursor, module, self.name).record_id, self.action)
        self.assertTrue(bridge._reference(self.cursor, "hr_org_chart", self.name).noupdate)
        self.assertEqual(
            bridge._reference(self.cursor, "hr", "act_hr_employee_public_hierarchy_view").record_id, link_source.record_id
        )
        self.assertTrue(bridge._reference(self.cursor, "hr_org_chart", "act_hr_employee_public_hierarchy_view").noupdate)
        self.native_load()
        self.assertEqual(bridge._reference(self.cursor, "hr_org_chart", self.name).record_id, self.action)
        self.assertEqual(self.snapshot()["action_links"], before["action_links"])
        loaded = self.snapshot()
        bridge.migrate(self.cursor, "20.0.1.0")
        self.assertEqual(self.snapshot(), loaded)
        self.assertEqual(
            self.connection.execute("SELECT id,mobile_view_mode FROM ir_act_window WHERE id=?", (self.action,)).fetchone(),
            (self.action, self.native["mobile_view_mode"]),
        )

    def test_custom_or_ambiguous_input_refuses_before_every_bridge_effect(self) -> None:
        changes = (
            ("UPDATE ir_act_window SET name=?", (json.dumps({"en_US": "Edited"}),)),
            ("UPDATE ir_act_window SET name=?", (json.dumps({"en_US": self.native["name"], "fr_FR": "Edited"}),)),
            ("UPDATE ir_act_window SET help=?", (json.dumps({"en_US": "<p>Edited</p>"}),)),
            ("UPDATE ir_act_window SET help=?", (json.dumps({"en_US": self.help, "fr_FR": self.help}),)),
            ("UPDATE ir_act_window SET help=?", (json.dumps({"en_US": self.help.replace("<p>", '<p onclick="bad">')}),)),
            ("UPDATE ir_act_window SET context=\"{'chat_icon': True, 'custom': 7}\"", ()),
            ("UPDATE ir_act_window SET context='untrusted()'", ()),
            ("UPDATE ir_act_window SET context=\"{'chat_icon': 1}\"", ()),
            ("UPDATE ir_act_window SET domain=\"[('id','=',7)]\"", ()),
            ("UPDATE ir_act_window SET mobile_view_mode='hierarchy'", ()),
            ("UPDATE ir_act_window SET view_id=7", ()),
            ("UPDATE ir_act_window SET search_view_id=7", ()),
            ("UPDATE ir_act_window SET binding_model_id=7", ()),
            ("UPDATE ir_act_window SET target='new'", ()),
            ("UPDATE ir_act_window SET usage='custom'", ()),
            ("UPDATE ir_act_window SET res_id=7", ()),
            ('UPDATE ir_act_window SET "limit"=7', ()),
            ("UPDATE ir_act_window SET filter=1", ()),
            ("UPDATE ir_act_window SET cache=0", ()),
            ("INSERT INTO ir_act_window_group_rel VALUES (?,7)", (self.action,)),
            ("INSERT INTO ir_act_window_view(id,act_window_id,view_id) VALUES (1,?,7)", (self.action,)),
            ("UPDATE ir_act_window_view SET sequence=99 WHERE id=300", ()),
            ("UPDATE ir_act_window_view SET view_id=7 WHERE id=300", ()),
            ("UPDATE ir_act_window_view SET multi=1 WHERE id=300", ()),
            ("UPDATE ir_act_window_view SET view_mode='form' WHERE id=300", ()),
            ("UPDATE ir_act_window_view SET act_window_id=999 WHERE id=300", ()),
            ("DELETE FROM ir_act_window_view WHERE id=300", ()),
            ("UPDATE ir_model_data SET model='wrong' WHERE name='act_hr_employee_public_hierarchy_view'", ()),
            ("DELETE FROM ir_model_data WHERE name='hr_employee_public_action'", ()),
            ("UPDATE ir_model_data SET res_id=999 WHERE name='hr_employee_public_action'", ()),
            ("UPDATE ir_model_data SET res_id=999 WHERE name='act_hr_employee_public_hierarchy_view'", ()),
            ("DELETE FROM ir_model_data WHERE name='act_hr_employee_public_hierarchy_view'", ()),
            ("UPDATE ir_model_data SET noupdate=1 WHERE name='act_hr_employee_public_hierarchy_view'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr','act_hr_employee_public_hierarchy_view','ir.actions.act_window.view',999,0)",
                (),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','link','ir.actions.act_window.view',300,0)",
                (),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr','act_hr_employee_public_hierarchy_view','ir.actions.act_window.view',300,1)",
                (),
            ),
            ("INSERT INTO other_actions VALUES ('ir_act_client',999,?)", (self.native["path"],)),
            ("INSERT INTO other_actions VALUES ('ir_act_client',?,'other')", (self.action,)),
            ("UPDATE ir_model_data SET noupdate=1 WHERE module='hr_org_chart'", ()),
            ("UPDATE ir_model_data SET res_id=-1 WHERE module='hr_org_chart'", ()),
            ("UPDATE ir_model_data SET model='wrong' WHERE module='hr_org_chart'", ()),
            ("DELETE FROM ir_model_data WHERE module='hr_org_chart'", ()),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr',?,'ir.actions.act_window',999,0)",
                (self.name,),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('hr',?,'ir.actions.act_window',?,1)",
                (self.name, self.action),
            ),
            (
                "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','alias','ir.actions.actions',?,0)",
                (self.action,),
            ),
        )
        for query, parameters in changes:
            with self.subTest(query=query):
                self.connection.execute("SAVEPOINT custom")
                self.connection.execute(query, parameters)
                self.assert_refuses_without_effects()
                self.connection.execute("ROLLBACK TO custom")
                self.connection.execute("RELEASE custom")

    def test_other_preflight_refusal_keeps_hr_aliases_unchanged(self) -> None:
        before = self.snapshot()
        with patch_attr(bridge, "_admit_mail", Mock(side_effect=RuntimeError("custom Mail"))):
            self.assert_refuses_without_effects()
        self.assertEqual(self.snapshot(), before)

    def test_changed_or_protected_native_definition_refuses_before_effects(self) -> None:
        original = self.target.read_bytes()
        for change in ("protected", "duplicate", "context", "view", "help", "fields"):
            with self.subTest(change=change):
                document = ElementTree.fromstring(original)
                record = document.find("./data/record")
                data = document.find("data")
                assert record is not None and data is not None
                if change == "protected":
                    data.set("noupdate", "1")
                elif change == "duplicate":
                    data.append(ElementTree.fromstring(ElementTree.tostring(record)))
                else:
                    field = record.find(
                        f"field[@name='{dict(context='context', view='view_id', help='help', fields='view_mode')[change]}']"
                    )
                    assert field is not None
                    if change == "fields":
                        record.remove(field)
                    elif change == "help":
                        field[0].text = "Changed native help"
                    elif change == "view":
                        field.set("eval", "7")
                    else:
                        field.text = "untrusted()"
                ElementTree.ElementTree(document).write(self.target)
                self.assert_refuses_without_effects()
        self.target.write_bytes(original)

    def test_absent_source_module_does_not_adopt_by_path(self) -> None:
        self.connection.execute("UPDATE ir_module_module SET state='uninstalled' WHERE name='hr_org_chart'")
        self.apply()
        self.assertIsNone(bridge._reference(self.cursor, "hr", self.name))
