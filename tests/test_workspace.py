from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from odoo_devkit import local_runtime, workspace
from odoo_devkit.cli import build_parser
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.runtime_environment import RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR
from odoo_devkit.workspace import clean_workspace, resolve_workspace_path, sync_workspace, workspace_status
from odoo_devkit.workspace_surface import LOCAL_NOTES_PATH, RESERVED_OVERRIDE_PATH


class WorkspaceSyncTestCase(unittest.TestCase):
    def test_manifest_accepts_an_explicit_empty_run_configuration_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = self._write_minimal_manifest(Path(directory))
            with manifest_path.open("a", encoding="utf-8") as manifest_file:
                manifest_file.write("\nrun_configurations = []\n")

            self.assertEqual(load_workspace_manifest(manifest_path).ide.run_configurations, ())

    def test_manifest_rejects_run_configurations_without_an_array_of_tables(self) -> None:
        for raw_value in ('""', '"command"', "42", "false", "{}", '["command"]'):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as directory:
                manifest_path = self._write_minimal_manifest(Path(directory))
                with manifest_path.open("a", encoding="utf-8") as manifest_file:
                    manifest_file.write(f"\nrun_configurations = {raw_value}\n")

                with self.assertRaisesRegex(ValueError, r"ide\.run_configurations"):
                    load_workspace_manifest(manifest_path)

    def test_internal_git_commands_exclude_runtime_payload(self) -> None:
        completed_process = mock.Mock(returncode=0, stdout="main\n", stderr="")
        with mock.patch.dict(os.environ, {RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: "test-payload"}):
            with mock.patch("odoo_devkit.workspace.subprocess.run", return_value=completed_process) as run_mock:
                result = workspace._git_output(Path("."), "rev-parse", "--abbrev-ref", "HEAD")

        self.assertEqual(result, "main")
        self.assertNotIn(RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, run_mock.call_args.kwargs["env"])

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
            self.assertIsNotNone(result.workspace_session_prompt_path)
            self.assertTrue(result.workspace_session_prompt_path.exists())
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
            self.assertIn(str(tenant_repo_path), workspace_agents_contents)
            self.assertIn(str(devkit_repo_path), workspace_agents_contents)
            self.assertIn(str((tenant_repo_path / "addons" / "shared").resolve()), workspace_agents_contents)

            workspace_docs_index_contents = result.workspace_docs_index_path.read_text(encoding="utf-8")
            self.assertIn(str((tenant_repo_path / "addons" / "shared").resolve()), workspace_docs_index_contents)

            workspace_session_prompt_contents = result.workspace_session_prompt_path.read_text(encoding="utf-8")
            self.assertIn(str(result.workspace_path), workspace_session_prompt_contents)
            self.assertIn(str(tenant_repo_path), workspace_session_prompt_contents)
            self.assertIn(str(devkit_repo_path), workspace_session_prompt_contents)

            self.assertEqual(len(result.run_configuration_paths), len(manifest.ide.run_configurations))
            for path, definition in zip(result.run_configuration_paths, manifest.ide.run_configurations, strict=True):
                configuration = ElementTree.parse(path).find("configuration")
                self.assertIsNotNone(configuration)
                assert configuration is not None
                options = {option.attrib["name"]: option.attrib["value"] for option in configuration.findall("option")}
                self.assertEqual(configuration.attrib["name"], definition.name)
                self.assertEqual(shlex.split(options["SCRIPT_TEXT"]), list(definition.command))
                self.assertEqual(options["SCRIPT_WORKING_DIRECTORY"], definition.working_directory)

            lock = tomllib.loads(result.lock_file_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["tenant"], manifest.tenant)
            self.assertEqual(set(lock["repos"]), {"tenant", "devkit", "runtime", "shared_addons"})
            self.assertEqual(lock["repos"]["tenant"]["resolved_path"], str(tenant_repo_path.resolve()))

    def test_sync_materializes_shared_addons_from_repo_url_and_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            shared_addons_repo_path = self._create_git_repo(temp_root / "shared-addons-repo")
            (shared_addons_repo_path / "addons" / "shared_widget").mkdir(parents=True, exist_ok=True)
            (shared_addons_repo_path / "addons" / "shared_widget" / "README.md").write_text(
                "# shared widget\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=shared_addons_repo_path, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "add shared addon"],
                cwd=shared_addons_repo_path,
                check=True,
                capture_output=True,
            )
            shared_addons_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=shared_addons_repo_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.shared_addons]
name = "shared-addons-repo"
url = "{shared_addons_repo_path}"
ref = "main"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons", "sources/shared-addons/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/shared-addons", "sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            materialized_shared_addons_path = result.workspace_path / "sources" / "shared-addons"
            self.assertTrue(materialized_shared_addons_path.exists())
            self.assertFalse(materialized_shared_addons_path.is_symlink())
            self.assertTrue((materialized_shared_addons_path / "addons" / "shared_widget" / "README.md").exists())

            materialized_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=materialized_shared_addons_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(materialized_head, shared_addons_head)

            lock = tomllib.loads(result.lock_file_path.read_text(encoding="utf-8"))
            locked_source = lock["repos"]["shared_addons"]
            self.assertIn("declared_url_sha256", locked_source)
            self.assertNotIn(str(shared_addons_repo_path), json.dumps(lock))
            self.assertEqual(locked_source["declared_ref"], manifest.shared_addons_repo.ref)
            status_payload = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            sources = status_payload["sources"]
            assert isinstance(sources, list)
            shared_source_status = next(source_status for source_status in sources if source_status["role"] == "shared_addons")
            self.assertEqual(shared_source_status["materialization"], "managed_checkout")
            self.assertFalse(shared_source_status["editable"])
            self.assertTrue(shared_source_status["materialization_current"])

            (materialized_shared_addons_path / "local-edit.txt").write_text("do not edit managed checkouts\n", encoding="utf-8")
            managed_drift_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(managed_drift_status["current"])
            self.assertTrue(managed_drift_status["surface_current"])
            self.assertFalse(managed_drift_status["managed_source_baseline_current"])
            managed_stale_reasons = managed_drift_status["stale_reasons"]
            assert isinstance(managed_stale_reasons, list)
            self.assertIn("managed_source_baseline_drift:shared_addons", managed_stale_reasons)
            parser = build_parser()
            arguments = parser.parse_args(["workspace", "status", "--manifest", str(manifest_path), "--check"])
            with mock.patch("odoo_devkit.cli._discover_repo_root", return_value=devkit_repo_path):
                with mock.patch("builtins.print"):
                    with self.assertRaisesRegex(SystemExit, "1"):
                        arguments.handler(arguments)
            (materialized_shared_addons_path / "local-edit.txt").unlink()

            unsafe_lock_contents = result.lock_file_path.read_text(encoding="utf-8").replace(
                "[repos.shared_addons]\n",
                '[repos.shared_addons]\ndeclared_url = "https://operator:secret@example.invalid/repo.git"\n',
            )
            result.lock_file_path.write_text(unsafe_lock_contents, encoding="utf-8")
            unsafe_lock_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(unsafe_lock_status["current"])
            lock_stale_reasons = unsafe_lock_status["stale_reasons"]
            assert isinstance(lock_stale_reasons, list)
            self.assertIn("lock_source_contract_mismatch:shared_addons", lock_stale_reasons)
            self.assertNotIn("operator:secret", json.dumps(unsafe_lock_status))

    def test_sync_materializes_runtime_repo_from_url_and_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            runtime_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=runtime_repo_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
url = "{runtime_repo_path}"
ref = "main"

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

            materialized_runtime_repo_path = result.workspace_path / "sources" / "runtime"
            self.assertTrue(materialized_runtime_repo_path.exists())
            self.assertFalse(materialized_runtime_repo_path.is_symlink())

            materialized_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=materialized_runtime_repo_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(materialized_head, runtime_head)

            values = local_runtime.parse_env_file(result.runtime_env_path)
            self.assertEqual(values["ODOO_WORKSPACE_RUNTIME_REPO"], str(materialized_runtime_repo_path.resolve()))

            lock = tomllib.loads(result.lock_file_path.read_text(encoding="utf-8"))
            locked_source = lock["repos"]["runtime"]
            self.assertEqual(locked_source["resolved_path"], str(materialized_runtime_repo_path.resolve()))
            self.assertIn("declared_url_sha256", locked_source)
            self.assertEqual(locked_source["declared_ref"], manifest.runtime_repo.ref)
            status_payload = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            sources = status_payload["sources"]
            assert isinstance(sources, list)
            runtime_source_status = next(source_status for source_status in sources if source_status["role"] == "runtime")
            self.assertEqual(runtime_source_status["materialization"], "managed_checkout")
            self.assertFalse(runtime_source_status["editable"])
            self.assertTrue(runtime_source_status["materialization_current"])

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
            self.assertTrue(status_payload["workspace_session_prompt_exists"])
            self.assertTrue(status_payload["current"])
            self.assertTrue(status_payload["surface_current"])
            self.assertTrue(status_payload["materialization_current"])
            sources = status_payload["sources"]
            edit_roots = status_payload["edit_roots"]
            assert isinstance(sources, list)
            assert isinstance(edit_roots, list)
            self.assertEqual([source["role"] for source in sources], ["tenant", "devkit"])
            self.assertEqual([source["role"] for source in edit_roots], ["tenant", "devkit"])
            self.assertEqual(
                status_payload["attached_paths"], [str((resolve_workspace_path(manifest) / "sources" / "devkit").resolve())]
            )
            self.assertEqual(status_payload["runtime_repo_path"], str(devkit_repo_path.resolve()))

    def test_sync_records_devkit_runtime_repo_for_local_manifest_without_runtime_repo(self) -> None:
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
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            values = local_runtime.parse_env_file(result.runtime_env_path)
            self.assertEqual(values["ODOO_WORKSPACE_RUNTIME_REPO"], str(devkit_repo_path.resolve()))

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

    def test_status_detects_manifest_and_generated_surface_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = self._write_minimal_manifest(tenant_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            result.workspace_agents_path.write_text("tampered\n", encoding="utf-8")
            stale_surface_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(stale_surface_status["current"])
            surface_stale_reasons = stale_surface_status["stale_reasons"]
            surfaces = stale_surface_status["surfaces"]
            assert isinstance(surface_stale_reasons, list)
            assert isinstance(surfaces, list)
            self.assertIn("surface_stale:agents", surface_stale_reasons)
            agents_status = next(surface_status for surface_status in surfaces if surface_status["kind"] == "agents")
            self.assertEqual(agents_status["state"], "stale")

            sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "# manifest drift\n", encoding="utf-8")
            stale_manifest_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(stale_manifest_status["current"])
            manifest_status = stale_manifest_status["manifest"]
            manifest_stale_reasons = stale_manifest_status["stale_reasons"]
            assert isinstance(manifest_status, dict)
            assert isinstance(manifest_stale_reasons, list)
            self.assertFalse(manifest_status["current"])
            self.assertIn("manifest_changed_since_sync", manifest_stale_reasons)
            self.assertTrue(stale_manifest_status["surface_current"])

    def test_status_reports_disabled_surfaces_without_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = self._write_minimal_manifest(tenant_repo_path)
            enabled_manifest = load_workspace_manifest(manifest_path)
            enabled_result = sync_workspace(manifest=enabled_manifest, devkit_repo_path=devkit_repo_path)
            self.assertTrue(enabled_result.workspace_agents_path.exists())

            self._write_minimal_manifest(
                tenant_repo_path,
                codex="""
[codex]
workspace_agents = false
workspace_docs_index = false
""",
            )
            disabled_manifest = load_workspace_manifest(manifest_path)
            disabled_result = sync_workspace(manifest=disabled_manifest, devkit_repo_path=devkit_repo_path)
            status_payload = workspace_status(manifest=disabled_manifest, devkit_repo_path=devkit_repo_path)

            self.assertIsNone(disabled_result.workspace_agents_path)
            self.assertIsNone(disabled_result.workspace_docs_index_path)
            self.assertIsNone(disabled_result.workspace_session_prompt_path)
            self.assertTrue(status_payload["current"])
            self.assertTrue(status_payload["surface_current"])
            surfaces = status_payload["surfaces"]
            assert isinstance(surfaces, list)
            self.assertEqual({surface["state"] for surface in surfaces}, {"disabled"})
            self.assertTrue(all(not surface["exists"] for surface in surfaces))

    def test_status_reports_symlink_drift_and_clean_sync_restores_current_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            alternate_tenant_repo_path = self._create_git_repo(temp_root / "alternate-tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest = load_workspace_manifest(self._write_minimal_manifest(tenant_repo_path))
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            tenant_entry_path = result.workspace_path / "sources" / "tenant"

            tenant_entry_path.unlink()
            tenant_entry_path.symlink_to(alternate_tenant_repo_path)
            drifted_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(drifted_status["current"])
            self.assertFalse(drifted_status["materialization_current"])
            stale_reasons = drifted_status["stale_reasons"]
            assert isinstance(stale_reasons, list)
            self.assertIn("source_materialization_mismatch:tenant", stale_reasons)

            clean_workspace(manifest=manifest)
            sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            restored_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertTrue(restored_status["current"])
            self.assertTrue(restored_status["materialization_current"])

    def test_status_reports_path_sources_and_informational_baseline_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            shared_addons_repo_path = self._create_git_repo(temp_root / "shared-addons-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            manifest_path = self._write_minimal_manifest(
                tenant_repo_path,
                extra_repos=f"""
[repos.shared_addons]
name = "shared-addons-repo"
path = "{shared_addons_repo_path}"

[repos.runtime]
name = "runtime-repo"
path = "{runtime_repo_path}"
""",
            )
            subprocess.run(["git", "add", "workspace.toml"], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "add workspace manifest"],
                cwd=tenant_repo_path,
                check=True,
                capture_output=True,
            )
            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            current_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)

            self.assertTrue(current_status["current"])
            current_sources = current_status["sources"]
            assert isinstance(current_sources, list)
            self.assertEqual(
                [source["role"] for source in current_sources],
                ["tenant", "devkit", "shared_addons", "runtime"],
            )
            self.assertTrue(all(source["editable"] for source in current_sources))
            self.assertTrue(all(source["materialization"] == "linked_path" for source in current_sources))
            self.assertEqual((result.workspace_path / "sources" / "runtime").resolve(), runtime_repo_path.resolve())

            (tenant_repo_path / "README.md").write_text("changed\n", encoding="utf-8")
            drifted_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            drifted_sources = drifted_status["sources"]
            assert isinstance(drifted_sources, list)
            tenant_status = next(source for source in drifted_sources if source["role"] == "tenant")
            self.assertTrue(drifted_status["current"])
            self.assertTrue(drifted_status["surface_current"])
            self.assertFalse(drifted_status["source_baseline_current"])
            self.assertIn("dirty", tenant_status["baseline_drift"])

    def test_status_reports_non_git_path_edit_root_without_false_baseline_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            shared_addons_path = temp_root / "shared-addons-directory"
            (shared_addons_path / "addons").mkdir(parents=True)
            (shared_addons_path / "README.md").write_text("# non-git shared addons\n", encoding="utf-8")
            manifest = load_workspace_manifest(
                self._write_minimal_manifest(
                    tenant_repo_path,
                    extra_repos=f"""
[repos.shared_addons]
name = "shared-addons-directory"
path = "{shared_addons_path}"
""",
                )
            )

            sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            status_payload = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            sources = status_payload["sources"]
            assert isinstance(sources, list)
            shared_source_status = next(source_status for source_status in sources if source_status["role"] == "shared_addons")

            self.assertTrue(status_payload["current"])
            self.assertTrue(status_payload["source_baseline_current"])
            self.assertEqual(shared_source_status["current_repo_state"]["repo_kind"], "directory")
            self.assertEqual(shared_source_status["baseline"]["repo_kind"], "directory")
            self.assertIsNone(shared_source_status["current_repo_state"]["dirty"])
            self.assertEqual(shared_source_status["baseline_drift"], [])

    def test_reserved_override_fails_check_while_local_notes_remain_supplemental(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest = load_workspace_manifest(self._write_minimal_manifest(tenant_repo_path))
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            agents_contents = result.workspace_agents_path.read_text(encoding="utf-8")
            self.assertIn(LOCAL_NOTES_PATH, agents_contents)
            self.assertIn(RESERVED_OVERRIDE_PATH, agents_contents)
            (result.workspace_path / "workspace.local.md").write_text("local non-secret note\n", encoding="utf-8")
            local_notes_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertTrue(local_notes_status["current"])
            local_notes = local_notes_status["local_notes"]
            assert isinstance(local_notes, dict)
            self.assertTrue(local_notes["exists"])
            self.assertTrue(local_notes["valid"])

            (result.workspace_path / "workspace.local.md").unlink()
            (result.workspace_path / "workspace.local.md").symlink_to(tenant_repo_path / "README.md")
            invalid_local_notes_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(invalid_local_notes_status["current"])
            invalid_local_notes = invalid_local_notes_status["local_notes"]
            notes_stale_reasons = invalid_local_notes_status["stale_reasons"]
            assert isinstance(invalid_local_notes, dict)
            assert isinstance(notes_stale_reasons, list)
            self.assertFalse(invalid_local_notes["valid"])
            self.assertIn("local_notes_invalid", notes_stale_reasons)
            (result.workspace_path / "workspace.local.md").unlink()

            (result.workspace_path / "AGENTS.override.md").write_text("replacement\n", encoding="utf-8")
            override_status = workspace_status(manifest=manifest, devkit_repo_path=devkit_repo_path)
            self.assertFalse(override_status["current"])
            reserved_override = override_status["reserved_override"]
            override_stale_reasons = override_status["stale_reasons"]
            assert isinstance(reserved_override, dict)
            assert isinstance(override_stale_reasons, list)
            self.assertTrue(reserved_override["exists"])
            self.assertEqual(reserved_override["semantics"], "full_replacement")
            self.assertIn("reserved_agents_override_present", override_stale_reasons)

    def test_cli_workspace_status_check_exits_nonzero_for_stale_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            manifest_path = self._write_minimal_manifest(tenant_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)
            parser = build_parser()
            arguments = parser.parse_args(["workspace", "status", "--manifest", str(manifest_path), "--check"])

            with mock.patch("odoo_devkit.cli._discover_repo_root", return_value=devkit_repo_path):
                with mock.patch("builtins.print"):
                    arguments.handler(arguments)

            result.workspace_agents_path.write_text("stale\n", encoding="utf-8")
            with mock.patch("odoo_devkit.cli._discover_repo_root", return_value=devkit_repo_path):
                with mock.patch("builtins.print"):
                    with self.assertRaisesRegex(SystemExit, "1"):
                        arguments.handler(arguments)

    def test_cli_parser_accepts_workspace_run_remainder(self) -> None:
        parser = build_parser()
        parsed_arguments = parser.parse_args(["workspace", "run", "--manifest", "workspace.toml", "--", "pwd"])
        self.assertEqual(parsed_arguments.command, ["--", "pwd"])

    @staticmethod
    def _write_minimal_manifest(
        tenant_repo_path: Path,
        *,
        extra_repos: str = "",
        codex: str = "",
    ) -> Path:
        manifest_path = tenant_repo_path / "workspace.toml"
        manifest_path.write_text(
            f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "../workspaces"

[repos.tenant]
name = "tenant-repo"
path = "."

{extra_repos.strip()}

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons"]
attached_paths = ["sources/devkit"]

{codex.strip()}
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return manifest_path

    @staticmethod
    def _create_git_repo(repo_path: Path) -> Path:
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
