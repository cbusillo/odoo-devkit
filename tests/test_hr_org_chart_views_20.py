from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

from test_core_references_20 import bridge, patch_attr
from test_hr_org_chart_action_20 import HrOrgChartActionFixture, WindowActionCursor


class ViewCursor(WindowActionCursor):
    def execute(self, query: str, parameters: tuple = ()) -> object:
        if query.startswith("LOCK TABLE"):
            return None
        return super().execute(query, parameters)


class HrOrgChartViewFixture(HrOrgChartActionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.view_plan_patch.stop()
        self.cursor = ViewCursor(self.connection)
        source = Path(bridge.__file__).parent / "legacy/hr_org_chart/hr_employee_public_views_19.xml"
        self.stock = {
            record.attrib["id"]: {field.attrib["name"]: field for field in record.findall("field")}
            for record in ElementTree.parse(source).findall("record[@model='ir.ui.view']")
        }
        self.connection.executescript(
            "CREATE TABLE ir_ui_view(id INTEGER PRIMARY KEY, inherit_id INTEGER, name TEXT, model TEXT, type TEXT, mode TEXT, "
            "active BOOLEAN, priority INTEGER, arch_updated BOOLEAN, arch_fs TEXT, is_seo_optimized BOOLEAN, "
            "customize_show BOOLEAN, arch_db JSON, arch_prev TEXT, write_uid INTEGER, custom_value TEXT);"
            "CREATE TABLE ir_ui_view_custom(ref_id INTEGER);"
            "CREATE TABLE ir_ui_view_group_rel(view_id INTEGER, group_id INTEGER);"
            "CREATE TABLE view_links(view_id INTEGER REFERENCES ir_ui_view(id), value TEXT);"
        )
        self.xmlid("hr", "hr_employee_public_view_form", "ir.ui.view", 400, False)
        self.connection.execute("INSERT INTO ir_ui_view(id,model) VALUES (400,?)", (self.employee_model,))
        self.views = {}
        for index, (name, fields) in enumerate(self.stock.items()):
            identity = 500 + index
            self.views[name] = identity
            self.xmlid("hr_org_chart", name, "ir.ui.view", identity, False)
            inherited = "inherit_id" in fields
            architecture = ElementTree.tostring(fields["arch"][0], encoding="unicode").replace(
                "%(hr_org_chart.action_hr_employee_public_org_chart)d", str(self.action)
            )
            self.connection.execute(
                "INSERT INTO ir_ui_view(id,inherit_id,name,model,type,mode,active,priority,arch_updated,arch_fs,"
                "is_seo_optimized,customize_show,arch_db,arch_prev,write_uid) VALUES (?,?,?,?,?,?,1,16,0,?,0,0,?,?,67)",
                (
                    identity,
                    400 if inherited else None,
                    fields["name"].text,
                    fields["model"].text,
                    "form" if inherited else "hierarchy",
                    "extension" if inherited else "primary",
                    "hr_org_chart/views/hr_employee_public_views.xml",
                    json.dumps({"en_US": architecture}),
                    architecture,
                ),
            )
            self.connection.execute("INSERT INTO view_links VALUES (?, 'retained')", (identity,))
        document = ElementTree.parse(self.target)
        data = document.getroot().find("data")
        assert data is not None
        hierarchy = ElementTree.SubElement(data, "record", id="hr_employee_public_hierarchy_view", model="ir.ui.view")
        for name in ("name", "model", "arch"):
            hierarchy.append(ElementTree.fromstring(ElementTree.tostring(self.stock["hr_employee_public_hierarchy_view"][name])))
        form = ElementTree.SubElement(data, "record", id="hr_employee_public_view_form", model="ir.ui.view")
        ElementTree.SubElement(form, "field", name="name").text = "Public employee form"
        ElementTree.SubElement(form, "field", name="model").text = self.employee_model
        arch = ElementTree.SubElement(form, "field", name="arch", type="xml")
        container = ElementTree.SubElement(ElementTree.SubElement(arch, "form"), "div", id="o_employee_org_chart")
        ElementTree.SubElement(container, "field", name="child_ids", widget="hr_org_chart")
        document.write(self.target)

    def snapshot(self) -> dict[str, list[tuple]]:
        return super().snapshot() | {
            table: self.connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("ir_ui_view", "ir_ui_view_custom", "ir_ui_view_group_rel", "view_links")
        }


class HrOrgChartViewTests(HrOrgChartViewFixture):
    def test_hierarchy_keeps_identity_and_extension_stops_duplicating_native_form_with_history_and_replay(self) -> None:
        before = self.snapshot()
        self.apply()
        after = self.snapshot()
        for name, identity in self.views.items():
            old = next(row for row in before["ir_ui_view"] if row[0] == identity)
            new = next(row for row in after["ir_ui_view"] if row[0] == identity)
            if old[5] == "primary":
                self.assertEqual(old, new)
                self.assertEqual(bridge._reference(self.cursor, "hr", name).record_id, identity)
                self.assertTrue(bridge._reference(self.cursor, "hr_org_chart", name).noupdate)
            else:
                self.assertEqual(old[:12] + old[13:], new[:12] + new[13:])
                self.assertFalse(list(ElementTree.fromstring(json.loads(new[12])["en_US"])))
        self.assertEqual(after["view_links"], before["view_links"])
        self.apply()
        self.assertEqual(self.snapshot(), after)
        bridge.migrate(self.cursor, "20.0.1.0")
        self.assertEqual(self.snapshot(), after)

    def test_custom_translated_restricted_or_dependent_views_refuse_before_any_bridge_effect(self) -> None:
        for identity in self.views.values():
            cases = (
                ("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": "<data custom='1'/>"}), identity)),
                ("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": "<data/>", "fr_FR": "<data/>"}), identity)),
                ("UPDATE ir_ui_view SET arch_prev='<data custom=\"1\"/>' WHERE id=?", (identity,)),
                ("UPDATE ir_ui_view SET custom_value='retain' WHERE id=?", (identity,)),
                ("UPDATE ir_ui_view SET active=0 WHERE id=?", (identity,)),
                ("UPDATE ir_ui_view SET arch_updated=1 WHERE id=?", (identity,)),
                ("UPDATE ir_ui_view SET priority=17 WHERE id=?", (identity,)),
                ("UPDATE ir_ui_view SET inherit_id=999 WHERE id=?", (identity,)),
                ("INSERT INTO ir_ui_view_custom VALUES (?)", (identity,)),
                ("INSERT INTO ir_ui_view_group_rel VALUES (?,1)", (identity,)),
                ("INSERT INTO ir_ui_view(id,inherit_id) VALUES (999,?)", (identity,)),
                (
                    "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES ('custom','alias','ir.ui.view',?,0)",
                    (identity,),
                ),
                ("UPDATE ir_model_data SET noupdate=1 WHERE model='ir.ui.view' AND res_id=?", (identity,)),
                ("UPDATE ir_model_data SET res_id=-1 WHERE model='ir.ui.view' AND res_id=?", (identity,)),
            )
            for statement, parameters in cases:
                with self.subTest(statement=statement, identity=identity):
                    self.connection.execute("SAVEPOINT unsupported")
                    try:
                        self.connection.execute(statement, parameters)
                        self.assert_refuses_without_effects()
                    finally:
                        self.connection.execute("ROLLBACK TO unsupported")
                        self.connection.execute("RELEASE unsupported")

    def test_later_mail_refusal_keeps_all_views_and_aliases_unchanged(self) -> None:
        with patch_attr(bridge, "_admit_mail", Mock(side_effect=RuntimeError("custom Mail"))):
            self.assert_refuses_without_effects()

    def test_competing_primary_conflicting_native_owner_or_missing_parent_refuse_before_effects(self) -> None:
        for statement in (
            "INSERT INTO ir_ui_view(id,model,type,mode) VALUES (999,'hr.employee.public','hierarchy','primary')",
            "INSERT INTO ir_model_data(module,name,model,res_id,noupdate) "
            "VALUES ('hr','hr_employee_public_hierarchy_view','ir.ui.view',999,0)",
            "UPDATE ir_model_data SET noupdate=1 WHERE name='hr_employee_public_view_form'",
            "DELETE FROM ir_ui_view WHERE id=400",
        ):
            with self.subTest(statement=statement):
                self.connection.execute("SAVEPOINT unsupported")
                try:
                    self.connection.execute(statement)
                    self.assert_refuses_without_effects()
                finally:
                    self.connection.execute("ROLLBACK TO unsupported")
                    self.connection.execute("RELEASE unsupported")

    def test_preview_image_attribute_change_is_not_normalized_into_stock(self) -> None:
        identity = self.views["hr_employee_public_hierarchy_view"]
        old = self.connection.execute("SELECT arch_db FROM ir_ui_view WHERE id=?", (identity,)).fetchone()[0]
        architectures = json.loads(old)
        architecture = ElementTree.fromstring(architectures["en_US"])
        image = architecture.find(".//field[@preview_image]")
        assert image is not None
        image.set("preview_image", image.attrib["preview_image"] + "_custom")
        architectures["en_US"] = ElementTree.tostring(architecture, encoding="unicode")
        self.connection.execute("UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps(architectures), identity))
        self.assert_refuses_without_effects()

    def test_native_view_drift_refuses_before_effects(self) -> None:
        original = self.target.read_bytes()
        for change in ("missing", "protected", "inherited", "different_model", "missing_chart", "duplicate_chart"):
            with self.subTest(change=change):
                document = ElementTree.fromstring(original)
                hierarchy = document.find(".//record[@id='hr_employee_public_hierarchy_view']")
                form = document.find(".//record[@id='hr_employee_public_view_form']")
                assert hierarchy is not None and form is not None
                if change == "missing":
                    data = document.find("data")
                    assert data is not None
                    data.remove(hierarchy)
                elif change == "protected":
                    hierarchy.set("noupdate", "1")
                elif change == "inherited":
                    ElementTree.SubElement(form, "field", name="inherit_id", ref="custom.parent")
                elif change == "different_model":
                    model = form.find("field[@name='model']")
                    assert model is not None
                    model.text = "hr.employee"
                else:
                    container = form.find(".//div[@id='o_employee_org_chart']")
                    assert container is not None
                    if change == "missing_chart":
                        container.remove(container[0])
                    else:
                        container.append(ElementTree.fromstring(ElementTree.tostring(container[0])))
                ElementTree.ElementTree(document).write(self.target)
                try:
                    self.assert_refuses_without_effects()
                finally:
                    self.target.write_bytes(original)
