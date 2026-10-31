from __future__ import annotations

import copy
import hashlib
import json
import os
import runpy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/mail_view_20.py"))


class MailStageTests(unittest.TestCase):
    def test_native_stage_updates_only_admitted_architecture_and_preserves_history(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            "CREATE TABLE ir_ui_view(id INTEGER PRIMARY KEY, arch_db TEXT, arch_prev TEXT, write_uid INTEGER);"
            "CREATE TABLE ir_model_data(id INTEGER PRIMARY KEY, res_id INTEGER, name TEXT);"
            "INSERT INTO ir_ui_view VALUES (17, 'old architecture', 'retained previous edit', 29);"
            "INSERT INTO ir_model_data VALUES (51, 17, 'retained alias');"
        )
        current: dict[str, Any] = {
            "database_uuid": "candidate",
            "target": {"arch_db": {"en_US": "old architecture"}},
            "binding": {"record_id": 17, "noupdate": False},
            "customizations": [],
        }
        reference = copy.deepcopy(current)
        reference["database_uuid"] = "independent"
        reference["modules"] = {"mail": "19.source"}
        function = MODULE["migrate_mail"]
        cursor = connection.cursor()

        class NativeCursor:
            @staticmethod
            def execute(query: str, parameters: tuple) -> None:
                cursor.execute(query.replace("%s", "?").replace("::jsonb", ""), parameters)

            @staticmethod
            def fetchone() -> tuple | None:
                return cursor.fetchone()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "native.xml"
            target.write_text(
                f'<odoo><record id="{MODULE["VIEW_XMLID"].split(".")[1]}" model="ir.ui.view">'
                '<field name="name">Native form</field><field name="model">ir.actions.server</field>'
                f'<field name="inherit_id" ref="{MODULE["ROOT_XMLID"]}"/>'
                '<field name="arch"><xpath expr="//form" position="inside"><field name="native_field"/></xpath></field>'
                "</record></odoo>"
            )
            capture = Mock(return_value=current)
            with patch.dict(function.__globals__, load_reference=Mock(return_value={"reference": reference}), capture=capture):
                for version in (None, "20.partial"):
                    with self.assertRaises(RuntimeError):
                        function(NativeCursor(), version, target)
                capture.assert_not_called()
                changed = copy.deepcopy(current)
                changed["target"]["arch_db"]["fr_FR"] = "retained translation"
                capture.return_value = changed
                with self.assertRaises(RuntimeError):
                    function(NativeCursor(), reference["modules"]["mail"], target)
                self.assertEqual(connection.execute("SELECT arch_db FROM ir_ui_view").fetchone(), ("old architecture",))
                capture.return_value = current
                function(NativeCursor(), reference["modules"]["mail"], target)
            self.assertEqual(
                connection.execute("SELECT * FROM ir_ui_view").fetchall(),
                [(17, json.dumps({"en_US": MODULE["canonical_architecture"](target)}), "retained previous edit", 29)],
            )
        self.assertEqual(connection.execute("SELECT * FROM ir_model_data").fetchall(), [(51, 17, "retained alias")])
        self.assertTrue(connection.in_transaction)


class MailFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.target: dict[str, Any] = {
            "active": True,
            "type": "form",
            "arch_db": {"en_US": "<data><field name='template_id'/></data>"},
            "arch_updated": False,
            "parent_xmlids": [{"xmlid": MODULE["ROOT_XMLID"]}],
            "xmlids": [{"xmlid": MODULE["VIEW_XMLID"], "noupdate": False}],
        }
        self.reference: dict[str, Any] = {
            "database_uuid": "independently-generated-reference",
            "target": self.target,
            "views": [self.target],
            "modules": {"mail": "source-version"},
            "binding": {"record_id": 101, "xmlid_id": 102, "noupdate": False},
            "customizations": [],
        }
        self.current = copy.deepcopy(self.reference)
        self.current["database_uuid"] = "candidate-copy"
        self.current["binding"].update(record_id=201, xmlid_id=202)

    def assert_refused(self) -> None:
        before = copy.deepcopy(self.current)
        with self.assertRaises(RuntimeError):
            MODULE["admit"](self.current, self.reference, whole_family=True)
        self.assertEqual(self.current, before)


class MailAdmissionTests(MailFixture):
    def test_bound_source_closure_preserves_independent_family_and_component_versions(self) -> None:
        self.current["modules"]["tenant_extension"] = "source.extension"
        profile = {"reference": self.reference, "history": {"source_contract": {"modules": self.current["modules"].copy()}}}
        before = copy.deepcopy(profile)
        MODULE["admit_profile"](self.current, profile)
        self.assertEqual(profile, before)
        for change in ("component", "extra", "family"):
            changed = copy.deepcopy(self.current)
            if change == "component":
                changed["modules"]["mail"] += ".different"
                changed_profile = copy.deepcopy(profile)
                changed_profile["history"]["source_contract"]["modules"] = changed["modules"].copy()
            else:
                changed_profile = profile
                if change == "extra":
                    changed["modules"]["unexpected"] = "source.unexpected"
                else:
                    changed["views"][0]["active"] = False
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                MODULE["admit_profile"](changed, changed_profile)

    def test_capture_can_select_an_independent_qweb_inheritance_family(self) -> None:
        root_xmlid, target_xmlid = "fixture.root", "fixture.child"
        root_id, target_id = 301, 302
        root = {"id": root_id, "inherit_id": None, "name": "Fixture root", "type": "qweb", "arch_db": {"en_US": "<t/>"}}
        target = dict(root, id=target_id, inherit_id=root_id, name="Fixture child")
        cursor = Mock()
        cursor.fetchall.side_effect = [
            [(401, "ir.ui.view", root_id, False, root_id)],
            [(402, "ir.ui.view", target_id, False, target_id)],
            [(root,), (target,)],
            [(root_id, *root_xmlid.split("."), "ir.ui.view", False), (target_id, *target_xmlid.split("."), "ir.ui.view", False)],
            [],
            [("fixture", "source")],
        ]
        cursor.fetchone.side_effect = [None, ("independent",)]

        captured = MODULE["capture"](cursor, root_xmlid=root_xmlid, view_xmlid=target_xmlid)

        self.assertEqual(captured["binding"]["record_id"], target_id)
        self.assertEqual([row["xmlid"] for row in captured["target"]["xmlids"]], [target_xmlid])
        self.assertEqual([row["xmlid"] for row in captured["target"]["parent_xmlids"]], [root_xmlid])
        cursor.fetchone.side_effect = [(1,)]
        cursor.fetchall.side_effect = [
            [(401, "ir.ui.view", root_id, False, root_id)],
            [(402, "ir.ui.view", target_id, False, target_id)],
            [(root,), (target,)],
            [(root_id, *root_xmlid.split("."), "ir.ui.view", False), (target_id, *target_xmlid.split("."), "ir.ui.view", False)],
            [],
        ]
        with self.assertRaises(RuntimeError):
            MODULE["capture"](cursor, root_xmlid=root_xmlid, view_xmlid=target_xmlid)

    def test_cross_database_surrogate_ids_do_not_reject_stock(self) -> None:
        MODULE["admit"](self.current, self.reference, whole_family=True)

    def test_candidate_cannot_supply_its_own_reference(self) -> None:
        self.current["database_uuid"] = self.reference["database_uuid"]
        self.assert_refused()

    def test_architecture_edit_with_unmodified_flag_is_deferred(self) -> None:
        self.current["target"]["arch_db"]["en_US"] = "<data><p>Keep my edit</p></data>"
        self.assert_refused()

    def test_custom_translation_is_deferred(self) -> None:
        self.current["target"]["arch_db"]["fr_FR"] = "<data><p>Mon contenu</p></data>"
        self.assert_refused()

    def test_linked_user_customization_is_deferred(self) -> None:
        self.current["customizations"] = [{"ref_id": 201, "arch": "<form><p>My dashboard</p></form>"}]
        self.assert_refused()

    def test_archived_or_reparented_stock_is_deferred(self) -> None:
        for field, value in (("active", False), ("parent_xmlids", [])):
            with self.subTest(field=field):
                current = copy.deepcopy(self.current)
                current["target"][field] = value
                with self.assertRaises(RuntimeError):
                    MODULE["admit"](current, self.reference, whole_family=True)

    def test_ownership_noupdate_and_inherited_edits_are_deferred(self) -> None:
        for field, value in (("xmlids", []), ("type", "qweb")):
            with self.subTest(field=field):
                current = copy.deepcopy(self.current)
                current["target"][field] = value
                with self.assertRaises(RuntimeError):
                    MODULE["admit"](current, self.reference, whole_family=True)
        self.current["binding"]["noupdate"] = True
        self.assert_refused()

    def test_inherited_edit_and_module_closure_change_are_deferred(self) -> None:
        self.current["views"].append({"arch_db": {"en_US": "<data><p>Inherited edit</p></data>"}})
        self.assert_refused()
        self.current["views"] = copy.deepcopy(self.reference["views"])
        self.current["modules"]["custom_module"] = "custom-version"
        self.assert_refused()

    def test_admission_does_not_mutate_deferred_payload(self) -> None:
        self.current["customizations"] = [{"arch": "Keep me", "active": False}]
        before = copy.deepcopy(self.current)
        self.assert_refused()
        self.assertEqual(self.current, before)

    def test_later_mail_stage_allows_added_native_schema_metadata(self) -> None:
        self.current["target"]["native20_schema_metadata"] = "new stock column"
        MODULE["admit"](self.current, self.reference, whole_family=False)

    def test_later_mail_stage_still_refuses_live_architecture_edit(self) -> None:
        self.current["target"]["arch_db"]["en_US"] = "<data><p>Edited</p></data>"
        with self.assertRaises(RuntimeError):
            MODULE["admit"](self.current, self.reference, whole_family=False)


class SourceFamilyTests(MailFixture):
    def setUp(self) -> None:
        super().setUp()
        self.reference["modules"] = {"base": "19.base", "mail": "19.mail"}
        self.reference["source_image"] = "fixture@sha256:source"
        self.source_modules = dict(self.reference["modules"], base_automation="19.automation", fixture_addon="19.addon")
        self.current["modules"] = dict(self.source_modules)
        self.profile = {"reference": self.reference, "source_image": self.reference.pop("source_image")}
        self.source = (
            '<odoo><data><record id="view_server_action_form" model="ir.ui.view">'
            '<field name="name">Automation form</field><field name="model">ir.actions.server</field>'
            f'<field name="inherit_id" ref="{MODULE["ROOT_XMLID"]}"/>'
            '<field name="arch" type="xml"><xpath expr="//form" position="inside"><button name="automation"/></xpath></field>'
            "</record></data></odoo>"
        )
        self.extension = copy.deepcopy(self.target)
        self.extension.update(
            name="Automation form",
            model="ir.actions.server",
            arch_fs="base_automation/views/ir_actions_server_views.xml",
            arch_db={"en_US": '<xpath position="inside" expr="//form"><button name="automation" /></xpath>'},
            xmlids=[{"xmlid": "base_automation.view_server_action_form", "model": "ir.ui.view", "noupdate": False}],
        )
        self.current["views"].append(self.extension)
        self.source_image = self.profile["source_image"]
        self.source_defaults = {
            key: value
            for key, value in self.reference["target"].items()
            if key not in {"name", "model", "arch_fs", "arch_db", "xmlids"}
        }

    def qualified(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ir_actions_server_views.xml"
            source.write_text(self.source)
            manifest = Path(directory) / "source-closure.json"
            manifest.write_text(
                json.dumps(
                    {
                        "source_image": self.source_image,
                        "modules": self.source_modules,
                        "base_automation_xml_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "base_automation_defaults": self.source_defaults,
                    }
                )
            )
            return MODULE["qualify_source_family"](self.profile, manifest, hashlib.sha256(manifest.read_bytes()).hexdigest(), source)

    def test_static_extension_and_bound_closure_admit_without_replacing_independent_reference(self) -> None:
        original = copy.deepcopy(self.profile)
        qualified = self.qualified()
        MODULE["admit_profile"](self.current, qualified)
        self.assertEqual(self.profile, original)
        self.assertEqual(qualified["reference"], original["reference"])

    def test_unqualified_family_and_closure_still_refuse(self) -> None:
        with self.assertRaises(RuntimeError):
            MODULE["admit_profile"](self.current, self.profile)

    def test_extension_edits_aliases_translations_and_unknown_fields_refuse_intact(self) -> None:
        qualified = self.qualified()
        for field, value in (
            ("active", False),
            ("arch_updated", True),
            ("priority", 7),
            ("unknown_column", "retained"),
            ("arch_db", {"en_US": "<xpath><p>User edit</p></xpath>"}),
            ("arch_db", {"en_US": "<data>Retain user text" + self.extension["arch_db"]["en_US"] + "</data>"}),
            ("arch_db", {**self.extension["arch_db"], "fr_FR": "<xpath><p>Traduction</p></xpath>"}),
            ("xmlids", [{"xmlid": "custom.owner", "model": "ir.ui.view", "noupdate": False}]),
            ("xmlids", [dict(self.extension["xmlids"][0], noupdate=True)]),
            ("parent_xmlids", []),
        ):
            current = copy.deepcopy(self.current)
            current["views"][-1][field] = value
            before = copy.deepcopy(current)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                MODULE["admit_profile"](current, qualified)
            self.assertEqual(current, before)

    def test_later_closure_changes_and_stock_sibling_edits_refuse(self) -> None:
        qualified = self.qualified()
        for change in ("module", "version", "sibling", "new_view", "missing_view"):
            current = copy.deepcopy(self.current)
            if change == "module":
                current["modules"]["unqualified"] = "19.extra"
            elif change == "version":
                current["modules"]["fixture_addon"] = "19.changed"
            elif change == "sibling":
                current["views"][0]["unknown_column"] = "edit"
            elif change == "new_view":
                current["views"].append(dict(self.extension, name="another view"))
            else:
                current["views"].pop()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                MODULE["admit_profile"](current, qualified)

    def test_source_image_or_independent_component_version_disagreement_refuses(self) -> None:
        self.source_image = "fixture@sha256:another"
        with self.assertRaises(RuntimeError):
            self.qualified()
        self.source_image = self.profile["source_image"]
        self.source_modules["mail"] = "19.wrong"
        with self.assertRaises(RuntimeError):
            self.qualified()
        self.source_modules["mail"] = self.reference["modules"]["mail"]
        self.source_defaults["active"] = False
        with self.assertRaises(RuntimeError):
            self.qualified()

    def test_source_artifact_tamper_refuses_before_admission(self) -> None:
        for key in ("manifest", "base_automation_xml"):
            qualified = self.qualified()
            qualified["source_family"][key] += " "
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                MODULE["admit_profile"](self.current, qualified)

    def test_source_comments_survive_comparison_and_added_candidate_comments_refuse(self) -> None:
        comment = "<!-- source annotation -->"
        self.source = self.source.replace("<button", comment + "<button")
        self.extension["arch_db"]["en_US"] = self.extension["arch_db"]["en_US"].replace("<button", comment + "<button")
        qualified = self.qualified()
        MODULE["admit_profile"](self.current, qualified)
        self.extension["arch_db"]["en_US"] = self.extension["arch_db"]["en_US"].replace(comment, "<!-- user annotation -->")
        with self.assertRaises(RuntimeError):
            MODULE["admit_profile"](self.current, qualified)


class TargetSourceTests(unittest.TestCase):
    def test_reference_history_must_share_database_and_complete_modules(self) -> None:
        reference = {
            "database_uuid": "independent",
            "modules": {"base": "19.native", "mail": "19.native"},
            "customizations": [],
            "binding": {"noupdate": False},
            "target": {
                "model": "ir.actions.server",
                "type": "form",
                "active": True,
                "arch_updated": False,
                "arch_db": {"en_US": "stock"},
            },
        }
        function = MODULE["write_reference"]
        cursor = Mock()
        cursor.fetchone.return_value = ("on",)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "native.xml"
            target.write_text("<odoo/>")
            with patch.dict(
                function.__globals__, capture=Mock(return_value=reference), canonical_architecture=Mock(return_value="stock")
            ):
                for section in ("history", "footer"):
                    for label, changes in (("uuid", {"database_uuid": "candidate"}), ("modules", {"modules": {"base": "19.other"}})):
                        facts = {"database_uuid": reference["database_uuid"], "modules": reference["modules"], **changes}
                        output = Path(directory) / (section + label + ".json")
                        with self.assertRaises(RuntimeError):
                            function(cursor, output, target, "source@sha256:source", "target@sha256:target", **{section: facts})
                        self.assertFalse(output.exists())
                    facts = {"database_uuid": reference["database_uuid"], "modules": reference["modules"]}
                    output = Path(directory) / (section + "matched.json")
                    digest = function(cursor, output, target, "source@sha256:source", "target@sha256:target", **{section: facts})
                    self.assertEqual(json.loads(output.read_text())[section], facts)
                    self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest())

    def test_reads_architecture_from_selected_native_source_and_binds_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xml = root / "native.xml"
            xml.write_text(
                f'<odoo><record id="{MODULE["VIEW_XMLID"].split(".", 1)[1]}" model="ir.ui.view">'
                '<field name="name">Native form</field><field name="model">ir.actions.server</field>'
                f'<field name="inherit_id" ref="{MODULE["ROOT_XMLID"]}"/>'
                '<field name="arch"><xpath expr="//form" position="inside"><p>Native source</p></xpath></field>'
                "</record></odoo>"
            )
            profile = root / "reference.json"
            profile.write_text(json.dumps({"target_xml_sha256": hashlib.sha256(xml.read_bytes()).hexdigest()}))
            environment = {
                MODULE["REFERENCE_ENV"]: str(profile),
                MODULE["REFERENCE_DIGEST_ENV"]: hashlib.sha256(profile.read_bytes()).hexdigest(),
            }
            with patch.dict(os.environ, environment):
                MODULE["load_reference"](xml)
                self.assertIn("Native source", MODULE["canonical_architecture"](xml))
                xml.write_text(xml.read_text().replace("Native source", "Changed target source"))
                with self.assertRaises(RuntimeError):
                    MODULE["load_reference"](xml)
                xml.write_text(xml.read_text().replace("Changed target source", "Native source"))
                profile.write_text(profile.read_text() + " ")
                with self.assertRaises(RuntimeError):
                    MODULE["load_reference"](xml)

    def test_missing_or_changed_reference_refuses(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            MODULE["load_reference"](Path("unused.xml"))


if __name__ == "__main__":
    unittest.main()
