from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from odoo_devkit.artifact_inputs import load_artifact_inputs_definition
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.scaffold import scaffold_tenant_overlay


class TenantOverlayScaffoldTests(unittest.TestCase):
    def test_scaffold_copies_overlay_templates_and_renders_tenant_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "devkit-repo"
            template_root = repo_root / "templates" / "tenant-overlay"
            (template_root / "docs").mkdir(parents=True, exist_ok=True)
            (template_root / "AGENTS.md").write_text("tenant replace-me\n", encoding="utf-8")
            (template_root / "docs" / "README.md").write_text("docs for replace-me\n", encoding="utf-8")
            (template_root / "workspace.toml").write_text('tenant = "replace-me"\n', encoding="utf-8")

            output_directory = temp_root / "tenant-repo"
            result = scaffold_tenant_overlay(
                repo_root=repo_root,
                output_directory=output_directory,
                tenant="opw",
                force=False,
            )

            self.assertEqual(result.output_directory, output_directory)
            self.assertEqual((output_directory / "AGENTS.md").read_text(encoding="utf-8"), "tenant opw\n")
            self.assertEqual((output_directory / "docs" / "README.md").read_text(encoding="utf-8"), "docs for opw\n")
            self.assertEqual((output_directory / "workspace.toml").read_text(encoding="utf-8"), 'tenant = "opw"\n')

    def test_scaffold_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "devkit-repo"
            template_root = repo_root / "templates" / "tenant-overlay"
            template_root.mkdir(parents=True, exist_ok=True)
            (template_root / "AGENTS.md").write_text("tenant replace-me\n", encoding="utf-8")

            output_directory = temp_root / "tenant-repo"
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / "AGENTS.md").write_text("existing\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "overwrite existing file"):
                scaffold_tenant_overlay(
                    repo_root=repo_root,
                    output_directory=output_directory,
                    tenant="opw",
                    force=False,
                )

    def test_forced_scaffold_updates_managed_file_and_preserves_owner_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "templates" / "tenant-overlay"
            template.mkdir(parents=True)
            (template / "AGENTS.md").write_text("tenant replace-me\n", encoding="utf-8")
            output = root / "tenant"
            output.mkdir()
            (output / "AGENTS.md").write_text("stale\n", encoding="utf-8")
            owner_file = output / "owner-notes.md"
            owner_contents = "Keep my notes.\n"
            owner_file.write_text(owner_contents, encoding="utf-8")

            scaffold_tenant_overlay(repo_root=root, output_directory=output, tenant="custom", force=True)

            self.assertEqual((output / "AGENTS.md").read_text(encoding="utf-8"), "tenant custom\n")
            self.assertEqual(owner_file.read_text(encoding="utf-8"), owner_contents)

    def test_real_template_renders_a_loadable_tenant_overlay(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "tenant-repo"

            result = scaffold_tenant_overlay(
                repo_root=repo_root,
                output_directory=output_directory,
                tenant="opw",
                force=False,
            )

            for script in (output_directory / "scripts").iterdir():
                template = repo_root / "templates" / "tenant-overlay" / script.relative_to(output_directory)
                self.assertEqual(os.access(script, os.X_OK), os.access(template, os.X_OK), script)

            for written_path in result.written_paths:
                self.assertNotIn("replace-me", written_path.read_text(encoding="utf-8"), written_path)
            manifest = load_workspace_manifest(output_directory / "workspace.toml")
            self.assertEqual(manifest.tenant, "opw")
            self.assertIsNotNone(load_artifact_inputs_definition(manifest=manifest))


if __name__ == "__main__":
    unittest.main()
