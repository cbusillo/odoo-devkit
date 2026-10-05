from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from odoo_devkit.artifact_inputs import load_artifact_inputs_definition
from odoo_devkit.manifest import load_workspace_manifest
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

            for written_path in result.written_paths:
                self.assertNotIn("replace-me", written_path.read_text(encoding="utf-8"), written_path)
            manifest = load_workspace_manifest(output_directory / "workspace.toml")
            self.assertEqual(manifest.tenant, "opw")
            self.assertIsNotNone(load_artifact_inputs_definition(manifest=manifest))


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
repo_name = "control-project"

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
            template = load_workspace_cockpit_manifest(template_root / "workspace-cockpit.toml")
            manifest = load_workspace_cockpit_manifest(output_directory / "workspace-cockpit.toml")
            self.assertEqual(manifest.repos, template.repos)
            self.assertTrue(workspace_cockpit_status(manifest=manifest, output_directory=output_directory).is_current)
            for relative_path in ("AGENTS.md", "docs/README.md", "docs/session-prompt.md"):
                contents = (output_directory / relative_path).read_text(encoding="utf-8")
                for repo in template.repos:
                    if relative_path.endswith("session-prompt.md") and repo.group != "primary":
                        continue
                    self.assertIn(repo.path, contents)
                    if relative_path != "docs/README.md":
                        self.assertIn(repo.repo_name, contents)

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
repo_name = "control-project"
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

    def test_invalid_cockpit_manifest_fails_before_generating_guides(self) -> None:
        valid = (
            "schema_version = 1\n"
            '[[repos]]\ngroup = "primary"\nrole = "devkit"\nlabel = "Devkit"\npath = "sources/devkit"\nrepo_name = "devkit"\n'
            '[[repos]]\ngroup = "primary"\nrole = "control_plane"\nlabel = "Control"\npath = "sources/control"\nrepo_name = "control"\n'
        )
        invalid_inputs = (
            valid.replace("schema_version = 1", "schema_version = 99"),
            valid.replace('path = "sources/control"', 'path = "sources/devkit"'),
            valid.replace('path = "sources/control"', 'path = "/absolute/control"'),
            valid.replace('group = "primary"', 'group = "unsupported"', 1),
            valid.replace('role = "control_plane"', 'role = "tenant"'),
            valid.replace('role = "devkit"', 'role = "tenant"'),
            valid + '[[repos]]\ngroup = "primary"\nrole = "devkit"\nlabel = "Other"\npath = "sources/other"\nrepo_name = "other"\n',
            valid
            + '[[repos]]\ngroup = "primary"\nrole = "control_plane"\nlabel = "Other"\npath = "sources/other"\nrepo_name = "other"\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "workspace-cockpit.toml"
            manifest_path.write_text(valid, encoding="utf-8")
            self.assertEqual(len(load_workspace_cockpit_manifest(manifest_path).repos), 2)
        for contents in invalid_inputs:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                template = root / "templates" / "workspace-cockpit"
                template.mkdir(parents=True)
                (template / "workspace-cockpit.toml").write_text(contents, encoding="utf-8")
                output = root / "output"
                with self.assertRaises(ValueError):
                    scaffold_workspace_cockpit(repo_root=root, output_directory=output, force=False)
                self.assertFalse((output / "AGENTS.md").exists())
                self.assertFalse((output / "docs").exists())

    def test_real_workspace_cockpit_template_scaffolds_a_current_root(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "workspace-root"

            scaffold_workspace_cockpit(
                repo_root=repo_root,
                output_directory=output_directory,
                force=False,
            )

            manifest = load_workspace_cockpit_manifest(output_directory / "workspace-cockpit.toml")
            self.assertTrue(workspace_cockpit_status(manifest=manifest, output_directory=output_directory).is_current)


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
repo_name = "control-project"

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
            manifest = load_workspace_cockpit_manifest(manifest_path)
            self.assertTrue(workspace_cockpit_status(manifest=manifest, output_directory=output_directory).is_current)
            for relative_path in ("AGENTS.md", "docs/README.md", "docs/session-prompt.md"):
                contents = (output_directory / relative_path).read_text(encoding="utf-8")
                for repo in manifest.repos:
                    if relative_path.endswith("session-prompt.md") and repo.group != "primary":
                        continue
                    self.assertIn(repo.path, contents)
                    if relative_path != "docs/README.md":
                        self.assertIn(repo.repo_name, contents)

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
repo_name = "control-project"
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

            (output_directory / "AGENTS.override.md").write_text("replacement\n", encoding="utf-8")
            override_result = workspace_cockpit_status(manifest=manifest, output_directory=output_directory)
            self.assertFalse(override_result.is_current)
            self.assertTrue(override_result.reserved_override_exists)
            (output_directory / "AGENTS.override.md").unlink()

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
repo_name = "control-project"
""".lstrip(),
                encoding="utf-8",
            )

            sync_workspace_cockpit(
                manifest=load_workspace_cockpit_manifest(manifest_path),
                output_directory=output_directory,
                overwrite_existing=True,
            )

            manifest = load_workspace_cockpit_manifest(manifest_path)
            guidance = {
                "AGENTS.md": (*manifest.agents_first_read_lines, *manifest.agents_ownership_lines, *manifest.agents_notes_lines),
                "docs/README.md": (
                    *manifest.docs_external_reference_lines,
                    *manifest.docs_working_split_lines,
                    *manifest.docs_operational_note_lines,
                ),
                "docs/session-prompt.md": manifest.session_prompt_rule_lines,
            }
            for relative_path, lines in guidance.items():
                contents = (output_directory / relative_path).read_text(encoding="utf-8")
                for line in lines:
                    self.assertIn(line, contents)

            changed_manifest = replace(manifest, session_prompt_rule_lines=("Use the changed tenant guide.",))
            self.assertFalse(workspace_cockpit_status(manifest=changed_manifest, output_directory=output_directory).is_current)
            sync_workspace_cockpit(manifest=changed_manifest, output_directory=output_directory, overwrite_existing=True)
            contents = (output_directory / "docs/session-prompt.md").read_text(encoding="utf-8")
            self.assertIn(changed_manifest.session_prompt_rule_lines[0], contents)
            for old_line in manifest.session_prompt_rule_lines:
                self.assertNotIn(old_line, contents)


if __name__ == "__main__":
    unittest.main()
