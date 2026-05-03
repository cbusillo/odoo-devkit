from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from odoo_devkit.scaffold import scaffold_tenant_overlay, scaffold_workspace_cockpit
from odoo_devkit.workspace_cockpit import load_workspace_cockpit_manifest, sync_workspace_cockpit, workspace_cockpit_status


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
            artifact_inputs_text = (output_directory / "artifact-inputs.toml").read_text(encoding="utf-8")
            agents_text = (output_directory / "AGENTS.md").read_text(encoding="utf-8")
            docs_index_text = (output_directory / "docs" / "README.md").read_text(encoding="utf-8")
            workspace_sync_text = (output_directory / "scripts" / "workspace-sync").read_text(encoding="utf-8")
            workspace_status_text = (output_directory / "scripts" / "workspace-status").read_text(encoding="utf-8")

            self.assertIn('name = "odoo-devkit"', manifest_text)
            self.assertIn("[repos.runtime]", manifest_text)
            self.assertIn('path = "../odoo-devkit"', manifest_text)
            self.assertIn('name = "odoo-shared-addons"', manifest_text)
            self.assertIn('path = "../odoo-shared-addons"', manifest_text)
            self.assertIn('addons_paths = ["sources/tenant/addons", "sources/shared-addons"]', manifest_text)
            self.assertIn('focus_paths = ["addons", "docs", "workspace.toml", "artifact-inputs.toml"]', manifest_text)
            self.assertIn('repository = "cbusillo/disable_odoo_online"', artifact_inputs_text)
            self.assertIn('selector = "main"', artifact_inputs_text)
            self.assertIn('platform", "runtime", "workflow"', manifest_text)
            self.assertIn('name = "opw Platform Update Local"', manifest_text)
            self.assertIn("sibling\n  `odoo-devkit` repo", agents_text)
            self.assertIn("current runtime commands in the sibling `odoo-devkit` repo", docs_index_text)
            self.assertIn('platform workspace sync --manifest "$repo_root/workspace.toml"', workspace_sync_text)
            self.assertIn('platform workspace status --manifest "$repo_root/workspace.toml"', workspace_status_text)


class WorkspaceCockpitScaffoldTests(unittest.TestCase):
    def test_scaffold_writes_workspace_cockpit_manifest_and_generated_docs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "devkit-repo"
            template_root = repo_root / "templates" / "workspace-cockpit"
            template_root.mkdir(parents=True, exist_ok=True)
            (template_root / "workspace-cockpit.toml").write_text(
                """
schema_version = 1

[[repos]]
group = "primary"
role = "devkit"
label = "Devkit"
path = "sources/devkit"
repo_name = "odoo-devkit"

[[repos]]
group = "primary"
role = "control_plane"
label = "Control plane"
path = "sources/harbor"
repo_name = "harbor"

[[repos]]
group = "upstream_image"
label = "Public base image"
path = "sources/odoo-docker"
repo_name = "odoo-docker"
""".lstrip(),
                encoding="utf-8",
            )

            output_directory = temp_root / "workspace-root"
            result = scaffold_workspace_cockpit(
                repo_root=repo_root,
                output_directory=output_directory,
                force=False,
            )

            self.assertEqual(result.output_directory, output_directory)
            manifest_text = (output_directory / "workspace-cockpit.toml").read_text(encoding="utf-8")
            agents_text = (output_directory / "AGENTS.md").read_text(encoding="utf-8")
            docs_index_text = (output_directory / "docs" / "README.md").read_text(encoding="utf-8")
            session_prompt_text = (output_directory / "docs" / "session-prompt.md").read_text(encoding="utf-8")

            self.assertIn("schema_version = 1", manifest_text)
            self.assertIn("workspace-cockpit.toml", agents_text)
            self.assertIn("AGENTS.override.md", agents_text)
            self.assertIn("sources/devkit", agents_text)
            self.assertIn("sync-cockpit-root", docs_index_text)
            self.assertIn("status-cockpit-root", docs_index_text)
            self.assertIn("sources/harbor -> harbor", session_prompt_text)

    def test_workspace_cockpit_scaffold_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "devkit-repo"
            template_root = repo_root / "templates" / "workspace-cockpit"
            template_root.mkdir(parents=True, exist_ok=True)
            (template_root / "workspace-cockpit.toml").write_text(
                """
schema_version = 1

[[repos]]
group = "primary"
role = "devkit"
label = "Devkit"
path = "sources/devkit"
repo_name = "odoo-devkit"

[[repos]]
group = "primary"
role = "control_plane"
label = "Control plane"
path = "sources/harbor"
repo_name = "harbor"
""".lstrip(),
                encoding="utf-8",
            )

            output_directory = temp_root / "workspace-root"
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / "workspace-cockpit.toml").write_text("existing\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "overwrite existing file"):
                scaffold_workspace_cockpit(
                    repo_root=repo_root,
                    output_directory=output_directory,
                    force=False,
                )

    def test_real_workspace_cockpit_template_links_back_to_devkit(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "workspace-root"

            scaffold_workspace_cockpit(
                repo_root=repo_root,
                output_directory=output_directory,
                force=False,
            )

            manifest_text = (output_directory / "workspace-cockpit.toml").read_text(encoding="utf-8")
            agents_text = (output_directory / "AGENTS.md").read_text(encoding="utf-8")
            docs_index_text = (output_directory / "docs" / "README.md").read_text(encoding="utf-8")
            session_prompt_text = (output_directory / "docs" / "session-prompt.md").read_text(encoding="utf-8")

            self.assertIn('path = "sources/devkit"', manifest_text)
            self.assertIn("[guidance.agents]", manifest_text)
            self.assertIn("[guidance.docs]", manifest_text)
            self.assertIn("[guidance.session_prompt]", manifest_text)
            self.assertIn("sources/devkit/AGENTS.md", agents_text)
            self.assertIn("sources/devkit/docs/README.md", agents_text)
            self.assertIn("AGENTS.override.md", agents_text)
            self.assertIn("Shared operating guide", docs_index_text)
            self.assertIn("Shared workspace CLI guide", docs_index_text)
            self.assertIn("workspace-cockpit.toml", agents_text)
            self.assertIn("uv --project sources/devkit", agents_text)
            self.assertIn("status-cockpit-root", agents_text)
            self.assertIn("When cockpit-root files disagree", session_prompt_text)
            self.assertIn("repo-owned code/docs", session_prompt_text)
            self.assertIn("launchplane for remote release actions", session_prompt_text)


class WorkspaceCockpitSyncTests(unittest.TestCase):
    def test_sync_workspace_cockpit_rerenders_existing_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            manifest_path = output_directory / "workspace-cockpit.toml"
            manifest_path.write_text(
                """
schema_version = 1

[[repos]]
group = "primary"
role = "devkit"
label = "Devkit"
path = "sources/devkit"
repo_name = "odoo-devkit"

[[repos]]
group = "primary"
role = "shared_addons"
label = "Shared addons"
path = "sources/shared-addons"
repo_name = "odoo-shared-addons"

[[repos]]
group = "primary"
role = "tenant"
label = "CM tenant"
path = "sources/tenant-cm"
repo_name = "odoo-tenant-cm"

[[repos]]
group = "primary"
role = "control_plane"
label = "Control plane"
path = "sources/harbor"
repo_name = "harbor"

[[repos]]
group = "upstream_image"
label = "Public base image"
path = "sources/odoo-docker"
repo_name = "odoo-docker"
""".lstrip(),
                encoding="utf-8",
            )
            (output_directory / "AGENTS.md").write_text("stale\n", encoding="utf-8")

            result = sync_workspace_cockpit(
                manifest=load_workspace_cockpit_manifest(manifest_path),
                output_directory=output_directory,
                overwrite_existing=True,
            )

            self.assertEqual(result.output_directory, output_directory)
            self.assertIn(output_directory / "AGENTS.md", result.written_paths)
            agents_text = (output_directory / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("sources/shared-addons", agents_text)
            self.assertIn("AGENTS.override.md", agents_text)
            self.assertIn("Public base image", (output_directory / "docs" / "README.md").read_text(encoding="utf-8"))
            session_prompt_text = (output_directory / "docs" / "session-prompt.md").read_text(encoding="utf-8")
            self.assertIn("workspace-cockpit.toml", session_prompt_text)
            self.assertIn("repo-owned code/docs", session_prompt_text)

    def test_workspace_cockpit_status_reports_current_missing_and_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            manifest_path = output_directory / "workspace-cockpit.toml"
            manifest_path.write_text(
                """
schema_version = 1

[[repos]]
group = "primary"
role = "devkit"
label = "Devkit"
path = "sources/devkit"
repo_name = "odoo-devkit"

[[repos]]
group = "primary"
role = "control_plane"
label = "Control plane"
path = "sources/harbor"
repo_name = "harbor"
""".lstrip(),
                encoding="utf-8",
            )

            manifest = load_workspace_cockpit_manifest(manifest_path)
            missing_result = workspace_cockpit_status(manifest=manifest, output_directory=output_directory)

            self.assertFalse(missing_result.is_current)
            self.assertTrue(all(not file_status.exists for file_status in missing_result.file_statuses))

            sync_workspace_cockpit(
                manifest=manifest,
                output_directory=output_directory,
                overwrite_existing=True,
            )
            current_result = workspace_cockpit_status(manifest=manifest, output_directory=output_directory)
            self.assertTrue(current_result.is_current)
            self.assertTrue(all(file_status.matches_expected for file_status in current_result.file_statuses))

            (output_directory / "AGENTS.md").write_text("stale\n", encoding="utf-8")
            stale_result = workspace_cockpit_status(manifest=manifest, output_directory=output_directory)
            self.assertFalse(stale_result.is_current)
            stale_agents_status = next(
                file_status for file_status in stale_result.file_statuses if file_status.path.name == "AGENTS.md"
            )
            self.assertTrue(stale_agents_status.exists)
            self.assertFalse(stale_agents_status.matches_expected)

    def test_workspace_cockpit_sync_renders_guidance_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            manifest_path = output_directory / "workspace-cockpit.toml"
            manifest_path.write_text(
                """
schema_version = 1

[guidance.agents]
first_reads = ["Open the custom cockpit guide first."]
ownership = ["Custom ownership line."]
notes = ["Custom note line."]

[guidance.docs]
external_reference_boundary = ["Custom external boundary."]
working_split = ["Custom working split."]
operational_notes = ["Custom operational note."]

[guidance.session_prompt]
working_rules = ["Custom working rule."]

[[repos]]
group = "primary"
role = "devkit"
label = "Devkit"
path = "sources/devkit"
repo_name = "odoo-devkit"

[[repos]]
group = "primary"
role = "control_plane"
label = "Control plane"
path = "sources/harbor"
repo_name = "harbor"
""".lstrip(),
                encoding="utf-8",
            )

            sync_workspace_cockpit(
                manifest=load_workspace_cockpit_manifest(manifest_path),
                output_directory=output_directory,
                overwrite_existing=True,
            )

            self.assertIn("Open the custom cockpit guide first.", (output_directory / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("Custom ownership line.", (output_directory / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("Custom external boundary.", (output_directory / "docs" / "README.md").read_text(encoding="utf-8"))
            self.assertIn("Custom working split.", (output_directory / "docs" / "README.md").read_text(encoding="utf-8"))
            self.assertIn("Custom working rule.", (output_directory / "docs" / "session-prompt.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
