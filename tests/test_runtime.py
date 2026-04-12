from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import local_runtime
from odoo_devkit.cli import (
    _handle_runtime_logs,
    _handle_runtime_odoo_shell,
    _handle_runtime_psql,
    _handle_runtime_restore,
    _handle_runtime_workflow,
)
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.runtime import (
    build_runtime_platform_command,
    resolve_runtime_repo_path,
    run_native_runtime_inspect,
    run_native_runtime_logs,
    run_native_runtime_odoo_shell,
    run_native_runtime_psql,
    run_native_runtime_restore,
    run_native_runtime_select,
    run_native_runtime_up,
    run_native_runtime_workflow,
    run_runtime_platform_command,
)
from odoo_devkit.workspace import sync_workspace


class RuntimeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.control_plane_root_temporary_directory = tempfile.TemporaryDirectory()
        self.control_plane_root = Path(self.control_plane_root_temporary_directory.name) / "odoo-control-plane"
        self.control_plane_root.mkdir(parents=True, exist_ok=True)
        self.environment_patch = mock.patch.dict(
            os.environ, {local_runtime.CONTROL_PLANE_ROOT_ENV_VAR: str(self.control_plane_root)}
        )
        self.environment_patch.start()
        self.load_environment_patch = mock.patch(
            "odoo_devkit.local_runtime.load_environment_from_control_plane",
            side_effect=self._load_environment_from_control_plane,
        )
        self.load_environment_patch.start()

    def tearDown(self) -> None:
        self.load_environment_patch.stop()
        self.environment_patch.stop()
        self.control_plane_root_temporary_directory.cleanup()
        super().tearDown()

    def test_resolve_runtime_repo_path_prefers_explicit_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

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

            self.assertEqual(resolve_runtime_repo_path(manifest), runtime_repo_path.resolve())

    def test_resolve_runtime_repo_path_requires_workspace_sync_for_repo_addressable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            self._initialize_git_repository(runtime_repo_path)

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

            with self.assertRaisesRegex(ValueError, "must be materialized by `platform workspace sync`"):
                resolve_runtime_repo_path(manifest)

    def test_resolve_runtime_repo_path_uses_materialized_repo_addressable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            self._initialize_git_repository(runtime_repo_path)

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
            self.assertEqual(resolve_runtime_repo_path(manifest), materialized_runtime_repo_path.resolve())
            self.assertEqual(
                build_runtime_platform_command(
                    manifest=manifest,
                    platform_subcommand="restore",
                ),
                (
                    "uv",
                    "--directory",
                    str(materialized_runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "restore",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                ),
            )

    def test_resolve_runtime_repo_path_defaults_to_devkit_repo_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            shared_addons_repo_path = temp_root / "odoo-shared-addons"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.shared_addons]
name = "shared-addons-repo"
path = "../odoo-shared-addons"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons", "sources/shared-addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            self.assertEqual(resolve_runtime_repo_path(manifest), Path(__file__).resolve().parent.parent)

    def test_resolve_runtime_repo_path_requires_explicit_runtime_repo_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[runtime]
context = "opw"
instance = "dev"
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

            with self.assertRaisesRegex(ValueError, r"must declare \[repos.runtime\]"):
                resolve_runtime_repo_path(manifest)

    def test_build_runtime_platform_command_uses_manifest_context_and_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

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

            self.assertEqual(
                build_runtime_platform_command(
                    manifest=manifest,
                    platform_subcommand="run",
                    platform_arguments=("--workflow", "update"),
                ),
                (
                    "uv",
                    "--directory",
                    str(runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "run",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                    "--workflow",
                    "update",
                ),
            )

    def test_run_runtime_platform_command_executes_from_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

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

            completed_process = mock.Mock(returncode=17)
            with mock.patch("odoo_devkit.runtime.subprocess.run", return_value=completed_process) as run_mock:
                exit_code = run_runtime_platform_command(manifest=manifest, platform_subcommand="restore")

            self.assertEqual(exit_code, 17)
            run_mock.assert_called_once_with(
                (
                    "uv",
                    "--directory",
                    str(runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "restore",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                ),
                cwd=tenant_repo_path.resolve(),
                check=False,
            )

    def test_native_runtime_select_writes_runtime_env_and_pycharm_conf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            shared_addons_repo_path = temp_root / "shared-addons-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                shared_addons_repo_path=shared_addons_repo_path,
                addons_paths=("sources/tenant/addons", "sources/shared-addons"),
            )

            manifest = load_workspace_manifest(manifest_path)

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_native_runtime_select(manifest=manifest)

            self.assertEqual(exit_code, 0)
            runtime_env_file = runtime_repo_path / ".platform" / "env" / "opw.local.env"
            pycharm_conf_file = runtime_repo_path / ".platform" / "ide" / "opw.local.odoo.conf"
            self.assertTrue(runtime_env_file.exists())
            self.assertTrue(pycharm_conf_file.exists())
            runtime_env_text = runtime_env_file.read_text(encoding="utf-8")
            self.assertIn("PLATFORM_CONTEXT=opw", runtime_env_text)
            self.assertIn("ODOO_PROJECT_NAME=odoo-opw-local", runtime_env_text)
            self.assertIn("DOCKER_IMAGE=odoo-opw-local", runtime_env_text)
            self.assertIn(f"ODOO_PROJECT_ADDONS_HOST_PATH={(tenant_repo_path / 'addons').resolve()}", runtime_env_text)
            self.assertIn("ODOO_ADDONS_PATH=/odoo/addons,/opt/project/addons,/opt/project/addons/shared", runtime_env_text)
            pycharm_conf_text = pycharm_conf_file.read_text(encoding="utf-8")
            self.assertIn("db_port = 15432", pycharm_conf_text)
            self.assertIn(f"addons_path = {(tenant_repo_path / 'addons').resolve()}", pycharm_conf_text)
            self.assertNotIn(str(runtime_repo_path / "addons"), pycharm_conf_text)

    def test_native_runtime_inspect_emits_key_value_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)
            output_buffer = io.StringIO()
            with contextlib.redirect_stdout(output_buffer):
                exit_code = run_native_runtime_inspect(manifest=manifest)

            self.assertEqual(exit_code, 0)
            inspect_output = output_buffer.getvalue()
            self.assertIn("context=opw", inspect_output)
            self.assertIn('"/odoo/addons"', inspect_output)
            self.assertIn(f'"{(tenant_repo_path / "addons").resolve()}"', inspect_output)
            self.assertIn("pycharm_addons_path=", inspect_output)
            self.assertIn("project_addons_host_path=", inspect_output)
            self.assertIn('"opw_custom"', inspect_output)

    def test_load_environment_prefers_control_plane_contract_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)

            loaded_environment = local_runtime.load_environment(
                repo_root=runtime_repo_path,
                context_name="opw",
                instance_name="local",
            )

        self.assertEqual(loaded_environment.merged_values["ODOO_MASTER_PASSWORD"], "control-plane-master")
        self.assertEqual(loaded_environment.merged_values["ODOO_DB_PASSWORD"], "control-plane-secret")

    def test_load_environment_fails_closed_when_control_plane_and_legacy_files_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            (runtime_repo_path / ".env").write_text("ODOO_MASTER_PASSWORD=legacy\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "legacy devkit-local env/secrets files still exist"):
                local_runtime.load_environment(
                    repo_root=runtime_repo_path,
                    context_name="opw",
                    instance_name="local",
                )

    def test_load_environment_requires_control_plane_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, local_runtime.CONTROL_PLANE_ROOT_ENV_VAR):
                    local_runtime.load_environment(
                        repo_root=runtime_repo_path,
                        context_name="opw",
                        instance_name="local",
                    )

    def test_load_environment_rejects_legacy_local_env_files_without_control_plane_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            (runtime_repo_path / ".env").write_text("ODOO_MASTER_PASSWORD=legacy\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "no longer supported"):
                    local_runtime.load_environment(
                        repo_root=runtime_repo_path,
                        context_name="opw",
                        instance_name="local",
                    )

    def test_runtime_environment_configuration_guidance_requires_control_plane_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            guidance = local_runtime.runtime_environment_configuration_guidance()

        self.assertIn(local_runtime.CONTROL_PLANE_ROOT_ENV_VAR, guidance)
        self.assertIn("config/runtime-environments.toml", guidance)

    def test_runtime_environment_configuration_guidance_mentions_control_plane_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            with mock.patch.dict(os.environ, {local_runtime.CONTROL_PLANE_ROOT_ENV_VAR: str(temp_root / "odoo-control-plane")}):
                guidance = local_runtime.runtime_environment_configuration_guidance(noun="it")

        self.assertIn(local_runtime.CONTROL_PLANE_ROOT_ENV_VAR, guidance)
        self.assertIn("config/runtime-environments.toml", guidance)

    def test_native_runtime_select_prefers_manifest_mounts_over_runtime_repo_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            shared_addons_repo_path = temp_root / "shared-addons-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                shared_addons_repo_path=shared_addons_repo_path,
                addons_paths=("sources/tenant/addons", "sources/shared-addons"),
            )

            manifest = load_workspace_manifest(manifest_path)

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_native_runtime_select(manifest=manifest)

            self.assertEqual(exit_code, 0)
            runtime_env_file = runtime_repo_path / ".platform" / "env" / "opw.local.env"
            runtime_env_text = runtime_env_file.read_text(encoding="utf-8")
            self.assertIn("DOCKER_IMAGE=odoo-opw-local", runtime_env_text)
            self.assertIn(f"ODOO_PROJECT_ADDONS_HOST_PATH={(tenant_repo_path / 'addons').resolve()}", runtime_env_text)
            self.assertIn(f"ODOO_SHARED_ADDONS_HOST_PATH={shared_addons_repo_path.resolve()}", runtime_env_text)
            self.assertIn(
                "ODOO_ADDONS_PATH=/odoo/addons,/opt/project/addons,/opt/project/addons/shared",
                runtime_env_text,
            )

    def test_native_runtime_up_runs_compose_up_without_build_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_up(manifest=manifest, build_images=False)

            self.assertEqual(exit_code, 0)
            run_mock.assert_called_once()
            command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertEqual(command[-3:], ["up", "-d", "--no-build"])
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())

    def test_native_runtime_workflow_runs_init_natively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="init")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertIn([*commands[0][:-2], "stop", "web"], commands)
            self.assertTrue(any(command[-3:] == ["up", "-d", "script-runner"] for command in commands))
            self.assertTrue(any("/odoo/odoo-bin" in command for command in commands))
            self.assertTrue(any(command[-3:] == ["up", "-d", "web"] for command in commands))

    def test_native_runtime_workflow_runs_openupgrade_natively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="openupgrade")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertTrue(any(command[-3:] == ["up", "-d", "script-runner"] for command in commands))
            self.assertTrue(any("/volumes/scripts/run_openupgrade.py" in command for command in commands))
            self.assertTrue(any(command[-3:] == ["up", "-d", "web"] for command in commands))

    def test_native_runtime_workflow_runs_update_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="update")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertTrue(any(command[-2:] == ["build", "web"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "database"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "script-runner"] for command in commands))
            update_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertIn("UPDATE_ONLY=1", update_command)
            self.assertIn("--update-only", update_command)

    def test_native_runtime_workflow_runs_bootstrap_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="bootstrap")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            bootstrap_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertIn("BOOTSTRAP=1", bootstrap_command)
            self.assertIn("--bootstrap", bootstrap_command)

    def test_native_runtime_restore_runs_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_restore(manifest=manifest)

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            restore_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertNotIn("UPDATE_ONLY=1", restore_command)
            self.assertNotIn("BOOTSTRAP=1", restore_command)
            self.assertTrue(any(command[-2:] == ["stop", "web"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "web"] for command in commands))

    def test_native_runtime_odoo_shell_executes_script_runner_with_script_and_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            script_path = tenant_repo_path / "tmp" / "shell-script.py"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("print('hello from shell')\n", encoding="utf-8")
            log_file_path = tenant_repo_path / "tmp" / "odoo-shell.log"

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_odoo_shell(
                        manifest=manifest,
                        service="script-runner",
                        database_name="opw-alt",
                        script_path=script_path,
                        log_file=log_file_path,
                        dry_run=False,
                    )

            self.assertEqual(exit_code, 0)
            command = run_mock.call_args.kwargs.get("args", run_mock.call_args.args[0])
            self.assertIn("exec", command)
            self.assertIn("-T", command)
            self.assertIn("script-runner", command)
            self.assertIn("shell", command)
            self.assertIn("opw-alt", command)
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())
            self.assertEqual(run_mock.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(run_mock.call_args.kwargs["input"], b"print('hello from shell')\n")
            self.assertTrue(log_file_path.parent.exists())

    def test_native_runtime_odoo_shell_dry_run_prints_redirects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            script_path = tenant_repo_path / "tmp" / "shell-script.py"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("print('hello from shell')\n", encoding="utf-8")
            log_file_path = tenant_repo_path / "tmp" / "odoo-shell.log"

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                output_buffer = io.StringIO()
                with contextlib.redirect_stdout(output_buffer):
                    exit_code = run_native_runtime_odoo_shell(
                        manifest=manifest,
                        service="script-runner",
                        database_name=None,
                        script_path=script_path,
                        log_file=log_file_path,
                        dry_run=True,
                    )

            self.assertEqual(exit_code, 0)
            run_mock.assert_not_called()
            output = output_buffer.getvalue()
            self.assertIn("<", output)
            self.assertIn(">", output)
            self.assertIn("odoo-shell.log", output)

    def test_native_runtime_restore_returns_none_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_remote_restore_workflow") as remote_restore:
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_restore(manifest=manifest)

            self.assertEqual(exit_code, 0)
            remote_restore.assert_called_once_with(manifest=manifest, runtime_repo_path=runtime_repo_path.resolve())

    def test_native_runtime_workflow_runs_remote_update_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_remote_update_workflow") as remote_update:
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="update")

            self.assertEqual(exit_code, 0)
            remote_update.assert_called_once_with(manifest=manifest, runtime_repo_path=runtime_repo_path.resolve())

    def test_native_runtime_workflow_runs_remote_bootstrap_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="prod",
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_remote_bootstrap_workflow") as remote_bootstrap:
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="bootstrap")

            self.assertEqual(exit_code, 0)
            remote_bootstrap.assert_called_once_with(manifest=manifest, runtime_repo_path=runtime_repo_path.resolve())

    def test_native_runtime_logs_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.stream_runtime_logs") as stream_runtime_logs:
                exit_code = run_native_runtime_logs(manifest=manifest, service="script-runner", tail_lines=25, follow=False)

            self.assertEqual(exit_code, 0)
            stream_runtime_logs.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                service="script-runner",
                tail_lines=25,
                follow=False,
            )

    def test_native_runtime_logs_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_logs(manifest=manifest, service="web", tail_lines=200, follow=True)

    def test_native_runtime_psql_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_psql_command") as run_psql_command:
                exit_code = run_native_runtime_psql(manifest=manifest, psql_arguments=("-c", "select 1"))

            self.assertEqual(exit_code, 0)
            run_psql_command.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                psql_arguments=("-c", "select 1"),
            )

    def test_native_runtime_psql_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="prod",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_psql(manifest=manifest, psql_arguments=())

    def test_native_runtime_odoo_shell_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_odoo_shell_command") as run_odoo_shell_command:
                exit_code = run_native_runtime_odoo_shell(
                    manifest=manifest,
                    service="script-runner",
                    database_name="opw-alt",
                    script_path=Path("tmp/script.py"),
                    log_file=Path("tmp/odoo-shell.log"),
                    dry_run=True,
                )

            self.assertEqual(exit_code, 0)
            run_odoo_shell_command.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                service="script-runner",
                database_name="opw-alt",
                script_path=Path("tmp/script.py"),
                log_file=Path("tmp/odoo-shell.log"),
                dry_run=True,
            )

    def test_native_runtime_odoo_shell_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_odoo_shell(
                    manifest=manifest,
                    service="script-runner",
                    database_name=None,
                    script_path=None,
                    log_file=None,
                    dry_run=False,
                )

    def test_cli_runtime_restore_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(manifest=manifest_path, runtime_instance="testing")

            with mock.patch("odoo_devkit.cli.run_native_runtime_restore", return_value=0) as runtime_restore:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_restore(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            overridden_manifest = runtime_restore.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.context, "opw")
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_logs_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                service="web",
                lines=50,
                follow=False,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_logs", return_value=0) as runtime_logs:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_logs(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_logs.call_args.kwargs["service"], "web")
            self.assertEqual(runtime_logs.call_args.kwargs["tail_lines"], 50)
            self.assertFalse(runtime_logs.call_args.kwargs["follow"])
            overridden_manifest = runtime_logs.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_psql_strips_leading_separator_and_supports_instance_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                psql_arguments=["--", "-c", "select 1"],
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_psql", return_value=0) as runtime_psql:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_psql(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_psql.call_args.kwargs["psql_arguments"], ("-c", "select 1"))
            overridden_manifest = runtime_psql.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_odoo_shell_supports_instance_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                script_path=Path("tmp/script.py"),
                service="script-runner",
                database_name="opw-alt",
                log_file=Path("tmp/odoo-shell.log"),
                dry_run=True,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_odoo_shell", return_value=0) as runtime_odoo_shell:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_odoo_shell(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["service"], "script-runner")
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["database_name"], "opw-alt")
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["script_path"], Path("tmp/script.py"))
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["log_file"], Path("tmp/odoo-shell.log"))
            self.assertTrue(runtime_odoo_shell.call_args.kwargs["dry_run"])
            overridden_manifest = runtime_odoo_shell.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_native_runtime_workflow_rejects_non_local_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_workflow(manifest=manifest, workflow="init")

    def test_native_runtime_workflow_rejects_non_local_openupgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_workflow(manifest=manifest, workflow="openupgrade")

    def test_cli_runtime_workflow_rejects_non_local_local_only_workflows_without_platform_fallback(self) -> None:
        workflow_cases = (
            ("init", "dev"),
            ("openupgrade", "testing"),
        )
        for workflow_name, instance_name in workflow_cases:
            with self.subTest(workflow=workflow_name, instance=instance_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temp_root = Path(temporary_directory)
                    tenant_repo_path = temp_root / "tenant-repo"
                    runtime_repo_path = temp_root / "runtime-repo"
                    tenant_repo_path.mkdir(parents=True, exist_ok=True)
                    runtime_repo_path.mkdir(parents=True, exist_ok=True)
                    manifest_path = self._write_manifest(
                        tenant_repo_path=tenant_repo_path,
                        runtime_repo_path=runtime_repo_path,
                        instance_name=instance_name,
                    )
                    arguments = argparse.Namespace(manifest=manifest_path, workflow=workflow_name)

                    with mock.patch("odoo_devkit.cli.run_runtime_platform_command") as platform_command:
                        with self.assertRaises(SystemExit) as captured_exit:
                            _handle_runtime_workflow(arguments)

                    self.assertIsInstance(captured_exit.exception.code, str)
                    self.assertIn("requires --instance local", str(captured_exit.exception.code))
                    platform_command.assert_not_called()

    @staticmethod
    def _runtime_data_workflow_side_effect() -> mock.Mock:
        def run_side_effect(*args: object, **kwargs: object) -> mock.Mock:
            command = kwargs.get("args") or args[0]
            assert isinstance(command, list)
            if command[-3:] == ["ps", "-q", "database"]:
                return mock.Mock(returncode=0, stdout="database-container\n")
            if command[-3:] == ["ps", "-q", "script-runner"]:
                return mock.Mock(returncode=0, stdout="script-runner-container\n")
            if command[:3] == ["docker", "inspect", "-f"]:
                return mock.Mock(returncode=0, stdout="running\n")
            return mock.Mock(returncode=0, stdout="")

        return run_side_effect

    def _load_environment_from_control_plane(
        self,
        *,
        control_plane_root: Path,
        context_name: str,
        instance_name: str,
    ) -> local_runtime.LoadedEnvironment:
        _ = control_plane_root, context_name, instance_name
        return local_runtime.LoadedEnvironment(
            env_file_path=self.control_plane_root / ".generated" / "runtime-env" / f"{context_name}.{instance_name}.env",
            merged_values={
                "ODOO_MASTER_PASSWORD": "control-plane-master",
                "ODOO_DB_USER": "odoo",
                "ODOO_DB_PASSWORD": "control-plane-secret",
                "ODOO_UPSTREAM_HOST": "example.internal",
                "ODOO_UPSTREAM_USER": "odoo",
                "ODOO_UPSTREAM_DB_NAME": "opw-source",
                "ODOO_UPSTREAM_DB_USER": "odoo",
                "ODOO_UPSTREAM_FILESTORE_PATH": "/srv/odoo/filestore/opw-source",
                "GITHUB_TOKEN": "gh-token",
                "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/example/runtime:19.0-runtime",
                "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/example/devtools:19.0-devtools",
            },
            collisions=(),
        )

    @staticmethod
    def _write_manifest(
        *,
        tenant_repo_path: Path,
        runtime_repo_path: Path,
        shared_addons_repo_path: Path | None = None,
        addons_paths: tuple[str, ...] = ("sources/tenant/addons",),
        instance_name: str = "local",
    ) -> Path:
        manifest_path = tenant_repo_path / "workspace.toml"
        (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
        shared_addons_block = ""
        if shared_addons_repo_path is not None:
            shared_addons_block = f"""

[repos.shared_addons]
name = "shared-addons-repo"
path = "{shared_addons_repo_path}"
"""
        rendered_addons_paths = ", ".join(f'"{addons_path}"' for addons_path in addons_paths)
        manifest_path.write_text(
            f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "{runtime_repo_path}"
{shared_addons_block}

[runtime]
context = "opw"
instance = "{instance_name}"
database = "opw"
addons_paths = [{rendered_addons_paths}]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return manifest_path

    @staticmethod
    def _write_runtime_repo(runtime_repo_path: Path) -> None:
        (runtime_repo_path / "platform" / "compose").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "addons" / "shared").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (runtime_repo_path / "platform" / "compose" / "base.yaml").write_text("services: {}\n", encoding="utf-8")
        (runtime_repo_path / "platform" / "stack.toml").write_text(
            """
schema_version = 1
odoo_version = "19.0"
addons_path = ["/odoo/addons", "/opt/project/addons"]
required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD"]

[contexts.opw]
database = "opw"
install_modules = ["opw_custom"]

[contexts.opw.instances.local]

[contexts.opw.instances.dev]

[contexts.opw.instances.testing]

[contexts.opw.instances.prod]
""".strip()
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _initialize_git_repository(repo_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Code"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "code@example.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, check=True, capture_output=True)
        return repo_path

    def _create_git_repo(self, repo_path: Path) -> Path:
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / "README.md").write_text(f"# {repo_path.name}\n", encoding="utf-8")
        return self._initialize_git_repository(repo_path)


if __name__ == "__main__":
    unittest.main()
