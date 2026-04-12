from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_real_template_renders_current_shared_addons_contract(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "tenant-repo"

            scaffold_tenant_overlay(
                repo_root=repo_root,
                output_directory=output_directory,
                tenant="opw",
                force=False,
            )

            manifest_text = (output_directory / "workspace.toml").read_text(encoding="utf-8")
            agents_text = (output_directory / "AGENTS.md").read_text(encoding="utf-8")
            docs_index_text = (output_directory / "docs" / "README.md").read_text(encoding="utf-8")
            workspace_sync_text = (output_directory / "scripts" / "workspace-sync").read_text(encoding="utf-8")
            workspace_status_text = (output_directory / "scripts" / "workspace-status").read_text(encoding="utf-8")

            self.assertIn('name = "odoo-devkit"', manifest_text)
            self.assertIn('[repos.runtime]', manifest_text)
            self.assertIn('path = "../odoo-devkit"', manifest_text)
            self.assertIn('name = "odoo-shared-addons"', manifest_text)
            self.assertIn('path = "../odoo-shared-addons"', manifest_text)
            self.assertIn('addons_paths = ["sources/tenant/addons", "sources/shared-addons"]', manifest_text)
            self.assertIn('platform", "runtime", "workflow"', manifest_text)
            self.assertIn('name = "opw Platform Update Local"', manifest_text)
            self.assertIn("sibling\n  `odoo-devkit` repo", agents_text)
            self.assertIn("current runtime commands in the sibling `odoo-devkit` repo", docs_index_text)
            self.assertIn('platform workspace sync --manifest "$repo_root/workspace.toml"', workspace_sync_text)
            self.assertIn('platform workspace status --manifest "$repo_root/workspace.toml"', workspace_status_text)


if __name__ == "__main__":
    unittest.main()
