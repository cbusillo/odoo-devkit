from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.runtime import (
    build_runtime_platform_command,
    resolve_runtime_repo_path,
    run_native_runtime_inspect,
    run_native_runtime_restore,
    run_native_runtime_select,
    run_native_runtime_up,
    run_native_runtime_workflow,
    run_runtime_platform_command,
)


class RuntimeCommandTests(unittest.TestCase):
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

    def test_resolve_runtime_repo_path_infers_odoo_ai_root_from_shared_addons_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            shared_addons_repo_path = temp_root / "odoo-ai" / "addons" / "shared"
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
path = "../odoo-ai/addons/shared"

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

            self.assertEqual(resolve_runtime_repo_path(manifest), (temp_root / "odoo-ai").resolve())

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
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

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
            self.assertIn("ODOO_ADDONS_PATH=/odoo/addons,/opt/project/addons,/opt/project/addons/shared", runtime_env_text)
            pycharm_conf_text = pycharm_conf_file.read_text(encoding="utf-8")
            self.assertIn("db_port = 15432", pycharm_conf_text)

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
            self.assertIn('"opw_custom"', inspect_output)

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

    def _runtime_data_workflow_side_effect(self) -> mock.Mock:
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

    def _write_manifest(
        self,
        *,
        tenant_repo_path: Path,
        runtime_repo_path: Path,
        instance_name: str = "local",
    ) -> Path:
        manifest_path = tenant_repo_path / "workspace.toml"
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

[runtime]
context = "opw"
instance = "{instance_name}"
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
        return manifest_path

    def _write_runtime_repo(self, runtime_repo_path: Path) -> None:
        (runtime_repo_path / "platform" / "compose").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "addons" / "shared").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / ".env").write_text(
            "\n".join(
                (
                    "ODOO_MASTER_PASSWORD=master",
                    "ODOO_DB_USER=odoo",
                    "ODOO_DB_PASSWORD=secret",
                    "ODOO_UPSTREAM_HOST=example.internal",
                    "ODOO_UPSTREAM_USER=odoo",
                    "ODOO_UPSTREAM_DB_NAME=opw-source",
                    "ODOO_UPSTREAM_DB_USER=odoo",
                    "ODOO_UPSTREAM_FILESTORE_PATH=/srv/odoo/filestore/opw-source",
                )
            )
            + "\n",
            encoding="utf-8",
        )
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


if __name__ == "__main__":
    unittest.main()
