from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from odoo_devkit.cli import build_parser
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.workspace import clean_workspace, resolve_workspace_path, sync_workspace, workspace_status


class WorkspaceSyncTestCase(unittest.TestCase):
    def test_sync_creates_workspace_lock_and_pycharm_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.devkit]
name = "devkit-repo"
path = "../devkit-repo"

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

[repos.shared_addons]
name = "shared-addons-repo"
path = "./addons/shared"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons", "sources/shared-addons"]
web_base_url = "https://opw-local.example.com"

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom", "platform", "tools"]
attached_paths = ["sources/shared-addons", "sources/devkit"]

[codex]
workspace_agents = true
workspace_docs_index = true

[[ide.run_configurations]]
name = "Workspace Sync"
working_directory = "$PROJECT_DIR$"
command = ["uv", "--directory", "$PROJECT_DIR$/../odoo-devkit", "run", "platform", "workspace", "sync", "--manifest", "$PROJECT_DIR$/workspace.toml"]

[[ide.run_configurations]]
name = "OPW Platform Update Local"
working_directory = "$PROJECT_DIR$"
command = ["uv", "--directory", "$PROJECT_DIR$/../odoo-devkit", "run", "platform", "runtime", "workflow", "--manifest", "$PROJECT_DIR$/workspace.toml", "--workflow", "update"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            self.assertTrue(result.workspace_path.exists())
            self.assertTrue(result.lock_file_path.exists())
            self.assertTrue(result.generated_odoo_conf_path.exists())
            self.assertTrue(result.runtime_env_path.exists())
            self.assertIsNotNone(result.workspace_agents_path)
            self.assertTrue(result.workspace_agents_path.exists())
            self.assertIsNotNone(result.workspace_docs_index_path)
            self.assertTrue(result.workspace_docs_index_path.exists())
            self.assertEqual((result.workspace_path / "sources" / "tenant").resolve(), tenant_repo_path.resolve())
            self.assertEqual((result.workspace_path / "sources" / "devkit").resolve(), devkit_repo_path.resolve())
            self.assertEqual(
                runtime_repo_path.resolve(), manifest.runtime_repo.resolve_path(manifest_directory=manifest.manifest_directory)
            )
            self.assertEqual(
                (result.workspace_path / "sources" / "shared-addons").resolve(), (tenant_repo_path / "addons" / "shared").resolve()
            )
            self.assertEqual(
                result.attached_paths,
                (
                    (result.workspace_path / "sources" / "shared-addons").resolve(),
                    (result.workspace_path / "sources" / "devkit").resolve(),
                ),
            )

            metadata_payload = json.loads(result.pycharm_metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata_payload["tenant"], "opw")
            self.assertEqual(metadata_payload["focus_paths"], ["addons/opw_custom", "platform", "tools"])
            self.assertEqual(
                metadata_payload["attached_paths"],
                [
                    str((result.workspace_path / "sources" / "shared-addons").resolve()),
                    str((result.workspace_path / "sources" / "devkit").resolve()),
                ],
            )

            workspace_agents_contents = result.workspace_agents_path.read_text(encoding="utf-8")
            self.assertIn("Workspace Operating Guide", workspace_agents_contents)
            self.assertIn(str(tenant_repo_path), workspace_agents_contents)
            self.assertIn(str(devkit_repo_path), workspace_agents_contents)

            workspace_docs_index_contents = result.workspace_docs_index_path.read_text(encoding="utf-8")
            self.assertIn("Workspace Docs", workspace_docs_index_contents)
            self.assertIn("Workspace operating guide", workspace_docs_index_contents)
            self.assertIn("Shared workspace CLI guide", workspace_docs_index_contents)
            self.assertIn("Shared workspace architecture", workspace_docs_index_contents)
            self.assertIn("Shared workspace command patterns", workspace_docs_index_contents)
            self.assertIn("Tenant overlay guide", workspace_docs_index_contents)

            self.assertEqual(len(result.run_configuration_paths), 2)
            first_run_configuration = result.run_configuration_paths[0].read_text(encoding="utf-8")
            self.assertIn("Workspace Sync", first_run_configuration)
            self.assertIn("$PROJECT_DIR$/workspace.toml", first_run_configuration)

            lock_contents = result.lock_file_path.read_text(encoding="utf-8")
            self.assertIn('tenant = "opw"', lock_contents)
            self.assertIn("[repos.tenant]", lock_contents)
            self.assertIn("[repos.devkit]", lock_contents)
            self.assertIn("[repos.runtime]", lock_contents)

    def test_status_reports_existing_workspace_after_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            status_payload = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)

            self.assertTrue(status_payload["workspace_exists"])
            self.assertTrue(status_payload["lock_file_exists"])
            self.assertEqual(status_payload["runtime_context"], "opw")
            self.assertEqual(status_payload["runtime_instance"], "local")
            self.assertTrue(status_payload["workspace_agents_exists"])
            self.assertTrue(status_payload["workspace_docs_index_exists"])
            self.assertEqual(
                status_payload["attached_paths"], [str((resolve_workspace_path(manifest) / "sources" / "devkit").resolve())]
            )

    def test_clean_removes_workspace_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            workspace_path = resolve_workspace_path(manifest)
            self.assertTrue(workspace_path.exists())

            clean_workspace(manifest=manifest)
            self.assertFalse(workspace_path.exists())

    def test_sync_repoints_existing_managed_symlink_when_tenant_repo_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            first_tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo-one")
            second_tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo-two")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")

            first_manifest_path = first_tenant_repo_path / "workspace.toml"
            first_manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo-one"
path = "."

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            first_manifest = load_workspace_manifest(first_manifest_path)
            first_result = sync_workspace(manifest=first_manifest, devkit_repo_path=devkit_repo_path)
            self.assertEqual((first_result.workspace_path / "sources" / "tenant").resolve(), first_tenant_repo_path.resolve())

            second_manifest_path = second_tenant_repo_path / "workspace.toml"
            second_manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "../tenant-repo-one/assembled"

[repos.tenant]
name = "tenant-repo-two"
path = "."

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            second_manifest = load_workspace_manifest(second_manifest_path)
            second_result = sync_workspace(manifest=second_manifest, devkit_repo_path=devkit_repo_path)

            self.assertEqual(second_result.workspace_path, first_result.workspace_path)
            self.assertEqual((second_result.workspace_path / "sources" / "tenant").resolve(), second_tenant_repo_path.resolve())

    def test_workspace_surface_prefers_tenant_root_scripts_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            scripts_directory = tenant_repo_path / "scripts"
            scripts_directory.mkdir(parents=True, exist_ok=True)
            (scripts_directory / "workspace-sync").write_text("#!/bin/sh\n", encoding="utf-8")
            (scripts_directory / "workspace-status").write_text("#!/bin/sh\n", encoding="utf-8")

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            workspace_agents_contents = result.workspace_agents_path.read_text(encoding="utf-8")
            workspace_docs_contents = result.workspace_docs_index_path.read_text(encoding="utf-8")

            self.assertIn("sources/tenant/scripts/workspace-sync", workspace_agents_contents)
            self.assertIn("sources/tenant/scripts/workspace-status", workspace_agents_contents)
            self.assertIn("sources/tenant/scripts/workspace-sync", workspace_docs_contents)
            self.assertIn("sources/tenant/scripts/workspace-status", workspace_docs_contents)

    def test_cli_parser_accepts_workspace_run_remainder(self) -> None:
        parser = build_parser()
        parsed_arguments = parser.parse_args(["workspace", "run", "--manifest", "workspace.toml", "--", "pwd"])
        self.assertEqual(parsed_arguments.command, ["--", "pwd"])

    def _create_git_repo(self, repo_path: Path) -> Path:
        repo_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Code"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "code@example.com"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "README.md").write_text(f"# {repo_path.name}\n", encoding="utf-8")
        (repo_path / "AGENTS.md").write_text(f"# {repo_path.name} guide\n", encoding="utf-8")
        (repo_path / "docs").mkdir(exist_ok=True)
        (repo_path / "docs" / "README.md").write_text(f"# {repo_path.name} docs\n", encoding="utf-8")
        (repo_path / "docs" / "ARCHITECTURE.md").write_text(f"# {repo_path.name} architecture\n", encoding="utf-8")
        (repo_path / "docs" / "roles.md").write_text(f"# {repo_path.name} roles\n", encoding="utf-8")
        (repo_path / "docs" / "tooling").mkdir(parents=True, exist_ok=True)
        (repo_path / "docs" / "tooling" / "workspace-cli.md").write_text(
            f"# {repo_path.name} workspace cli\n",
            encoding="utf-8",
        )
        (repo_path / "docs" / "tooling" / "command-patterns.md").write_text(
            f"# {repo_path.name} command patterns\n",
            encoding="utf-8",
        )
        (repo_path / "docs" / "tooling" / "tenant-overlay.md").write_text(
            f"# {repo_path.name} tenant overlay\n",
            encoding="utf-8",
        )
        (repo_path / "addons").mkdir(exist_ok=True)
        (repo_path / "addons" / "shared").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, check=True, capture_output=True)
        return repo_path


if __name__ == "__main__":
    unittest.main()
