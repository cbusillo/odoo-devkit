from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as element_tree
from pathlib import Path

from odoo_devkit.cli import build_parser
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.pycharm_sources import prepare_odoo_sources


class OdooSourcesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.project = self.root / "tenant"
        self.source = self.root / "odoo 19"
        for path in (self.project, self.source):
            path.mkdir()
            self.git(path, "init", "-q")
            self.git(path, "config", "user.name", "Fixture")
            self.git(path, "config", "user.email", "fixture@example.invalid")
        (self.source / "odoo").mkdir()
        (self.source / "odoo" / "addons" / "base").mkdir(parents=True)
        (self.source / "odoo" / "addons" / "base" / "__manifest__.py").write_text("{}\n")
        (self.source / "odoo" / "release.py").write_text("version_info = (19, 0, 0, FINAL, 0, '')\n")
        (self.source / "addons" / "web").mkdir(parents=True)
        (self.source / "addons" / "web" / "__manifest__.py").write_text("{}\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "Source fixture")
        self.commit = self.git(self.source, "rev-parse", "HEAD")
        (self.project / ".gitignore").write_text(".idea/\n")
        (self.project / "pyproject.toml").write_text('[project]\nname = "tenant-dependencies"\nversion = "0.1.0"\n')
        self.manifest_path = self.project / "workspace.toml"
        self.manifest_path.write_text(
            'schema_version = 1\ntenant = "test"\n'
            '[workspace]\nname = "test"\npython = "3.13"\n'
            '[repos.tenant]\nname = "tenant"\npath = "."\n'
            '[runtime]\ncontext = "test"\ninstance = "local"\ndatabase = "test"\naddons_paths = []\n'
            '[ide]\nmode = "tenant_repo"\nfocus_paths = ["addons"]\nattached_paths = []\n'
        )

    @staticmethod
    def git(path: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    def prepare(self, **overrides: object) -> dict[str, object]:
        arguments = {
            "manifest": load_workspace_manifest(self.manifest_path),
            "source_path": self.source,
            "expected_commit": self.commit,
            "expected_series": "19.0",
        }
        arguments.update(overrides)
        return prepare_odoo_sources(**arguments)

    def test_cli_creates_exact_project_and_repeated_preparation_does_not_rewrite(self) -> None:
        arguments = build_parser().parse_args(
            [
                "workspace",
                "prepare-ide",
                "--manifest",
                str(self.manifest_path),
                "--odoo-source",
                str(self.source),
                "--odoo-commit",
                self.commit,
                "--odoo-series",
                "19.0",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            arguments.handler(arguments)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["project_path"], str(self.project))
        self.assertEqual(summary["odoo_commit"], self.commit)
        self.assertEqual(summary["odoo_series"], "19.0")
        module_path = Path(summary["module_path"])
        module = element_tree.parse(module_path)
        content_urls = [content.get("url") for content in module.findall("./component/content")]
        self.assertEqual(module.getroot().get("external.system.id"), "pyproject.toml")
        self.assertEqual(module_path.name, "tenant-dependencies.iml")
        self.assertEqual(content_urls, [self.project.as_uri(), self.source.as_uri()])
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.project / ".idea").iterdir()}
        self.assertFalse(self.prepare()["changed"])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_existing_module_preserves_sdk_roots_comments_and_other_ide_files(self) -> None:
        self.prepare()
        module_path = self.project / ".idea" / "tenant-dependencies.iml"
        module = element_tree.parse(module_path)
        manager = module.find("./component")
        manager.remove(manager.findall("content")[1])
        element_tree.SubElement(manager, "orderEntry", {"type": "jdk", "jdkName": "Owner SDK"})
        element_tree.SubElement(manager, "content", {"url": (self.root / "shared-addons").as_uri()})
        manager.append(element_tree.Comment(" keep my project settings "))
        module.write(module_path)
        workspace_path = self.project / ".idea" / "workspace.xml"
        workspace_path.write_bytes(b"owner state must not change\n")
        modules_bytes = (self.project / ".idea" / "modules.xml").read_bytes()
        self.assertTrue(self.prepare()["changed"])
        contents = module_path.read_text()
        self.assertIn("Owner SDK", contents)
        self.assertIn("shared-addons", contents)
        self.assertIn("keep my project settings", contents)
        self.assertEqual(workspace_path.read_bytes(), b"owner state must not change\n")
        self.assertEqual((self.project / ".idea" / "modules.xml").read_bytes(), modules_bytes)

    def test_wrong_commit_series_or_mutable_ref_writes_nothing(self) -> None:
        for overrides, message in (
            ({"expected_commit": "a" * 40}, "commit mismatch"),
            ({"expected_commit": "19.0"}, "full 40-character"),
            ({"expected_series": "18.0"}, "series mismatch"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.prepare(**overrides)
            self.assertFalse((self.project / ".idea").exists())

    def test_dirty_dependency_is_rejected_without_modifying_it(self) -> None:
        release_path = self.source / "odoo" / "release.py"
        release_path.write_text("version_info = (18, 0, 0, FINAL, 0, '')\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.prepare()
        self.assertIn("18, 0", release_path.read_text())
        self.assertFalse((self.project / ".idea").exists())

    def test_unignored_or_tracked_metadata_is_not_overwritten(self) -> None:
        (self.project / ".gitignore").write_text("")
        with self.assertRaisesRegex(ValueError, "Git-ignored"):
            self.prepare()
        self.assertFalse((self.project / ".idea").exists())
        (self.project / ".gitignore").write_text(".idea/\n")
        self.prepare()
        module_path = self.project / ".idea" / "tenant-dependencies.iml"
        module = element_tree.parse(module_path)
        manager = module.find("./component")
        manager.remove(manager.findall("content")[1])
        module.write(module_path)
        self.git(self.project, "add", "-f", ".idea/tenant-dependencies.iml")
        before = module_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "tracked"):
            self.prepare()
        self.assertEqual(module_path.read_bytes(), before)

    def test_foreign_module_and_main_checkout_remain_untouched(self) -> None:
        self.prepare()
        foreign = self.root / "main-checkout"
        foreign.mkdir()
        foreign_module = foreign / "main.iml"
        foreign_module.write_text('<module type="PYTHON_MODULE" />')
        modules_path = self.project / ".idea" / "modules.xml"
        modules = element_tree.parse(modules_path)
        element_tree.SubElement(modules.find("./component/modules"), "module", {"filepath": str(foreign_module)})
        modules.write(modules_path)
        before = modules_path.read_bytes()
        self.assertFalse(self.prepare()["changed"])
        self.assertEqual(modules_path.read_bytes(), before)
        self.assertEqual(foreign_module.read_text(), '<module type="PYTHON_MODULE" />')

    def test_multiple_local_modules_fail_before_writing(self) -> None:
        self.prepare()
        other_module = self.project / ".idea" / "other.iml"
        other_module.write_text(
            '<module type="PYTHON_MODULE"><component name="NewModuleRootManager">'
            '<content url="file://$MODULE_DIR$" /></component></module>'
        )
        modules_path = self.project / ".idea" / "modules.xml"
        modules = element_tree.parse(modules_path)
        element_tree.SubElement(modules.find("./component/modules"), "module", {"filepath": str(other_module)})
        modules.write(modules_path)
        before = {path: path.read_bytes() for path in modules_path.parent.iterdir()}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.prepare()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_symlinked_idea_and_manifest_targeting_another_checkout_are_rejected(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        (self.project / ".idea").symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked .idea"):
            self.prepare()
        self.assertEqual(list(foreign.iterdir()), [])
        self.manifest_path.write_text(self.manifest_path.read_text().replace('path = "."', 'path = "../foreign"'))
        with self.assertRaisesRegex(ValueError, "exact tenant"):
            self.prepare()

    def test_existing_different_odoo_root_requires_explicit_reconciliation(self) -> None:
        self.prepare()
        other_source = self.root / "other-odoo"
        (other_source / "odoo").mkdir(parents=True)
        (other_source / "odoo" / "release.py").write_text("version_info = (18, 0)\n")
        module_path = self.project / ".idea" / "tenant-dependencies.iml"
        module = element_tree.parse(module_path)
        module.findall("./component/content")[1].set("url", other_source.as_uri())
        module.write(module_path)
        before = module_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different Odoo source"):
            self.prepare()
        self.assertEqual(module_path.read_bytes(), before)

    def test_symlinked_module_is_not_followed(self) -> None:
        self.prepare()
        module_path = self.project / ".idea" / "tenant-dependencies.iml"
        original = module_path.read_bytes()
        saved_module = self.project / ".idea" / "owner.iml"
        module_path.rename(saved_module)
        module_path.symlink_to(saved_module)
        with self.assertRaisesRegex(ValueError, "symlinked module"):
            self.prepare()
        self.assertEqual(saved_module.read_bytes(), original)

    def test_pycharm_serialized_module_paths_remain_idempotent(self) -> None:
        self.prepare()
        module_path = self.project / ".idea" / "tenant-dependencies.iml"
        module = element_tree.parse(module_path)
        contents = module.findall("./component/content")
        contents[0].set("url", "file://$MODULE_DIR$")
        contents[1].set("url", "file://$MODULE_DIR$/../odoo 19")
        module.write(module_path)
        original = module_path.read_bytes()
        self.assertFalse(self.prepare()["changed"])
        self.assertEqual(module_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
