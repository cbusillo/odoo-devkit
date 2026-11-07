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
        self.stock: dict[str, dict[str, ElementTree.Element]] = {
            record.attrib["id"]: {field.attrib["name"]: field for field in record.findall("field")}
            for record in ElementTree.parse(source).findall("record[@model='ir.ui.view']")
        }
        self.connection.executescript(
            "ALTER TABLE ir_ui_view ADD COLUMN inherit_id INTEGER; ALTER TABLE ir_ui_view ADD COLUMN name TEXT;"
            "ALTER TABLE ir_ui_view ADD COLUMN mode TEXT; ALTER TABLE ir_ui_view ADD COLUMN active BOOLEAN;"
            "ALTER TABLE ir_ui_view ADD COLUMN priority INTEGER; ALTER TABLE ir_ui_view ADD COLUMN arch_updated BOOLEAN;"
            "ALTER TABLE ir_ui_view ADD COLUMN arch_fs TEXT; ALTER TABLE ir_ui_view ADD COLUMN is_seo_optimized BOOLEAN;"
            "ALTER TABLE ir_ui_view ADD COLUMN customize_show BOOLEAN; ALTER TABLE ir_ui_view ADD COLUMN arch_db JSON;"
            "ALTER TABLE ir_ui_view ADD COLUMN arch_prev TEXT; ALTER TABLE ir_ui_view ADD COLUMN write_uid INTEGER;"
            "ALTER TABLE ir_ui_view ADD COLUMN custom_value TEXT;"
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
    def options_architecture(self) -> str:
        source = Path(bridge.__file__).parent / "legacy/hr_org_chart/hr_employee_public_hierarchy_options_19.xml"
        root = ElementTree.parse(source, parser=ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True)))
        return ElementTree.tostring(root.getroot(), encoding="unicode")

    def test_native19_image_options_preserve_current_history_links_and_replay(self) -> None:
        identity = self.views["hr_employee_public_hierarchy_view"]
        original = self.connection.execute("SELECT arch_db FROM ir_ui_view WHERE id=?", (identity,)).fetchone()[0]
        source = Path(bridge.__file__).parent / "legacy/hr_org_chart/hr_employee_public_views_18.xml"
        previous = ElementTree.parse(source).find("record[@id='hr_employee_public_hierarchy_view']/field[@name='arch']")
        assert previous is not None
        history18 = ElementTree.tostring(previous[0], encoding="unicode")
        options = self.options_architecture()
        for case, current, history in (
            ("current_options", json.dumps({"en_US": options}), history18),
            ("previous_options", original, options),
        ):
            with self.subTest(case=case):
                self.connection.execute("SAVEPOINT options_form")
                try:
                    self.connection.execute("UPDATE ir_ui_view SET arch_db=?,arch_prev=? WHERE id=?", (current, history, identity))
                    before = self.connection.execute("SELECT * FROM ir_ui_view WHERE id=?", (identity,)).fetchone()
                    links = self.snapshot()["view_links"]
                    self.apply()
                    self.assertEqual(self.connection.execute("SELECT * FROM ir_ui_view WHERE id=?", (identity,)).fetchone(), before)
                    self.assertEqual(self.snapshot()["view_links"], links)
                    after = self.snapshot()
                    self.apply()
                    self.assertEqual(self.snapshot(), after)
                finally:
                    self.connection.execute("ROLLBACK TO options_form")
                    self.connection.execute("RELEASE options_form")

    def test_changed_native19_image_options_refuse_before_effects(self) -> None:
        identity = self.views["hr_employee_public_hierarchy_view"]
        stock_image = self.stock["hr_employee_public_hierarchy_view"]["arch"].find(".//field[@preview_image]")
        assert stock_image is not None
        preview = stock_image.attrib["preview_image"]
        for extra_option in (False, True):
            root = ElementTree.fromstring(self.options_architecture())
            image = root.find(".//field[@widget='background_image']")
            assert image is not None
            options = image.attrib["options"]
            image.set(
                "options",
                options[:-1] + ", 'custom': true}" if extra_option else options.replace(repr(preview), repr(preview + "_custom")),
            )
            changed = ElementTree.tostring(root, encoding="unicode")
            for column, value in (("arch_db", json.dumps({"en_US": changed})), ("arch_prev", changed)):
                with self.subTest(extra_option=extra_option, column=column):
                    self.connection.execute("SAVEPOINT changed_options")
                    try:
                        self.connection.execute("UPDATE ir_ui_view SET " + column + "=? WHERE id=?", (value, identity))
                        self.assert_refuses_without_effects()
                    finally:
                        self.connection.execute("ROLLBACK TO changed_options")
                        self.connection.execute("RELEASE changed_options")

    def test_untracked_public_website_defaults_are_preserved_and_changed_values_refuse(self) -> None:
        self.connection.executescript(
            "ALTER TABLE ir_ui_view ADD COLUMN track BOOLEAN; ALTER TABLE ir_ui_view ADD COLUMN visibility TEXT;"
        )
        self.connection.execute("UPDATE ir_ui_view SET track=0,visibility='' WHERE id IN (?,?)", tuple(self.views.values()))
        before = self.connection.execute("SELECT id,track,visibility FROM ir_ui_view ORDER BY id").fetchall()
        self.apply()
        self.assertEqual(self.connection.execute("SELECT id,track,visibility FROM ir_ui_view ORDER BY id").fetchall(), before)
        for statement, value in (
            ("UPDATE ir_ui_view SET track=? WHERE id=?", True),
            ("UPDATE ir_ui_view SET visibility=? WHERE id=?", "connected"),
        ):
            self.connection.execute("SAVEPOINT website_default")
            try:
                self.connection.execute(statement, (value, next(iter(self.views.values()))))
                self.assert_refuses_without_effects()
            finally:
                self.connection.execute("ROLLBACK TO website_default")
                self.connection.execute("RELEASE website_default")

    def test_stock_previous_hierarchy_from_18_and_absent_extension_history_are_preserved(self) -> None:
        source = Path(bridge.__file__).parent / "legacy/hr_org_chart/hr_employee_public_views_18.xml"
        hierarchy = ElementTree.parse(source).find("record[@id='hr_employee_public_hierarchy_view']/field[@name='arch']")
        assert hierarchy is not None
        previous = ElementTree.tostring(hierarchy[0], encoding="unicode")
        self.connection.execute(
            "UPDATE ir_ui_view SET arch_prev=? WHERE id=?", (previous, self.views["hr_employee_public_hierarchy_view"])
        )
        extension = self.views["hr_employee_public_view_form_inherit_org_chart"]
        self.connection.execute("UPDATE ir_ui_view SET arch_prev=NULL WHERE id=?", (extension,))
        before = self.snapshot()
        self.apply()
        self.assertEqual(
            self.connection.execute(
                "SELECT arch_prev FROM ir_ui_view WHERE id=?", (self.views["hr_employee_public_hierarchy_view"],)
            ).fetchone(),
            (previous,),
        )
        self.assertEqual(self.connection.execute("SELECT arch_prev FROM ir_ui_view WHERE id=?", (extension,)).fetchone(), (None,))
        self.assertEqual(self.snapshot()["view_links"], before["view_links"])
        after = self.snapshot()
        self.apply()
        self.assertEqual(self.snapshot(), after)

    def test_stock_18_extension_history_preserves_rows_and_replays_with_stock_19_or_empty_current(self) -> None:
        source = Path(bridge.__file__).parent / "legacy/hr_org_chart/hr_employee_public_form_18.xml"
        extension = ElementTree.parse(source).find("record/field[@name='arch']")
        assert extension is not None
        previous = ElementTree.tostring(extension[0], encoding="unicode")
        identity = self.views["hr_employee_public_view_form_inherit_org_chart"]
        for empty_current in (False, True):
            with self.subTest(empty_current=empty_current):
                self.connection.execute("SAVEPOINT stock_history")
                try:
                    self.connection.execute("UPDATE ir_ui_view SET arch_prev=? WHERE id=?", (previous, identity))
                    if empty_current:
                        self.connection.execute(
                            "UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": "<data/>"}), identity)
                        )
                    before = self.connection.execute("SELECT * FROM ir_ui_view WHERE id=?", (identity,)).fetchone()
                    links = self.snapshot()["view_links"]
                    self.apply()
                    after = self.connection.execute("SELECT * FROM ir_ui_view WHERE id=?", (identity,)).fetchone()
                    self.assertEqual(before[:12] + before[13:], after[:12] + after[13:])
                    self.assertFalse(list(ElementTree.fromstring(json.loads(after[12])["en_US"])))
                    self.assertEqual(self.snapshot()["view_links"], links)
                    migrated = self.snapshot()
                    self.apply()
                    self.assertEqual(self.snapshot(), migrated)
                finally:
                    self.connection.execute("ROLLBACK TO stock_history")
                    self.connection.execute("RELEASE stock_history")
        changed = ElementTree.fromstring(previous)
        changed[0].set("class", changed[0].attrib["class"] + " custom")
        for current, history in ((previous, previous), (None, ElementTree.tostring(changed, encoding="unicode"))):
            with self.subTest(current=current, history=history):
                self.connection.execute("SAVEPOINT unsupported_history")
                try:
                    self.connection.execute("UPDATE ir_ui_view SET arch_prev=? WHERE id=?", (history, identity))
                    if current is not None:
                        self.connection.execute(
                            "UPDATE ir_ui_view SET arch_db=? WHERE id=?", (json.dumps({"en_US": current}), identity)
                        )
                    self.assert_refuses_without_effects()
                finally:
                    self.connection.execute("ROLLBACK TO unsupported_history")
                    self.connection.execute("RELEASE unsupported_history")

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
