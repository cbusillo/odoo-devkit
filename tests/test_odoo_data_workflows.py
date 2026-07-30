from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_data_workflows_module() -> types.ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "run_odoo_data_workflows.py"
    spec = importlib.util.spec_from_file_location("odoo_devkit_run_odoo_data_workflows_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    original_sys_path = list(sys.path)

    psycopg2_module = types.ModuleType("psycopg2")
    psycopg2_module.Error = Exception
    psycopg2_module.sql = types.SimpleNamespace(SQL=lambda value: value, Identifier=lambda value: value)
    psycopg2_extensions_module = types.ModuleType("psycopg2.extensions")
    psycopg2_extensions_module.connection = object

    with patch.dict(
        sys.modules,
        {
            "psycopg2": psycopg2_module,
            "psycopg2.extensions": psycopg2_extensions_module,
        },
    ):
        try:
            sys.path.insert(0, str(module_path.parent))
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = original_sys_path
    return module


odoo_data_workflows = _load_data_workflows_module()


class OdooDataWorkflowShellEnvironmentTests(unittest.TestCase):
    @staticmethod
    def _local_settings() -> object:
        return odoo_data_workflows.LocalServerSettings(
            ODOO_DB_HOST="database",
            ODOO_DB_PORT="5432",
            ODOO_DB_USER="odoo",
            ODOO_DB_PASSWORD="database-password",
            ODOO_DB_NAME="cm",
            ODOO_FILESTORE_PATH="/volumes/data/filestore/cm",
        )

    def test_data_workflow_shell_can_import_runtime_script_helpers(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/custom:/volumes/scripts"}, clear=True):
            runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)

        self.assertEqual(runner.os_env["PYTHONPATH"], "/volumes/scripts:/opt/custom")

    def test_data_workflow_shell_prepends_runtime_scripts_to_pythonpath(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/custom"}, clear=True):
            runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)

        self.assertEqual(runner.os_env["PYTHONPATH"], "/volumes/scripts:/opt/custom")

    def test_post_deploy_maintenance_runs_overrides_and_service_user_provisioning(self) -> None:
        calls: list[str] = []
        runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)

        with (
            patch.object(runner, "install_addons", side_effect=lambda **_kwargs: calls.append("install_addons")),
            patch.object(runner, "update_addons", side_effect=lambda **_kwargs: calls.append("update_addons")),
            patch.object(runner, "connect_to_db", side_effect=lambda: calls.append("connect_to_db")),
            patch.object(
                runner,
                "reconcile_missing_manifest_install_queue",
                side_effect=lambda: calls.append("reconcile_missing_manifest_install_queue"),
            ),
            patch.object(
                runner,
                "assert_install_queue_is_resolvable",
                side_effect=lambda: calls.append("assert_install_queue_is_resolvable"),
            ),
            patch.object(
                runner,
                "apply_environment_overrides",
                side_effect=lambda: calls.append("apply_environment_overrides"),
            ),
            patch.object(runner, "ensure_admin_user", side_effect=lambda: calls.append("ensure_admin_user")),
            patch.object(
                runner,
                "assert_core_schema_healthy",
                side_effect=lambda: calls.append("assert_core_schema_healthy"),
            ),
            patch.object(runner, "ensure_gpt_users", side_effect=lambda: calls.append("ensure_gpt_users")),
            patch.object(runner, "sanitize_database", side_effect=lambda: calls.append("sanitize_database")),
        ):
            runner.run_post_deploy_maintenance()

        self.assertEqual(
            calls,
            [
                "install_addons",
                "update_addons",
                "connect_to_db",
                "reconcile_missing_manifest_install_queue",
                "assert_install_queue_is_resolvable",
                "apply_environment_overrides",
                "ensure_admin_user",
                "connect_to_db",
                "assert_core_schema_healthy",
                "ensure_gpt_users",
            ],
        )
        self.assertNotIn("sanitize_database", calls)

    def test_update_only_and_post_deploy_maintenance_are_mutually_exclusive(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = odoo_data_workflows.main(["--update-only", "--post-deploy-maintenance"])

        self.assertEqual(result, odoo_data_workflows.ExitCode.INVALID_ARGS)

    def test_update_only_requires_configured_launchplane_payload_before_addon_update(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ODOO_DB_HOST": "database",
                    "ODOO_DB_USER": "odoo",
                    "ODOO_DB_PASSWORD": "database-password",
                    "ODOO_DB_NAME": "cm",
                    "ODOO_FILESTORE_PATH": "/volumes/data/filestore/cm",
                    "LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED": "true",
                },
                clear=True,
            ),
            patch.object(
                odoo_data_workflows.OdooDataWorkflowRunner,
                "acquire_data_workflow_lock",
            ),
            patch.object(
                odoo_data_workflows.OdooDataWorkflowRunner,
                "release_data_workflow_lock",
            ),
            patch.object(
                odoo_data_workflows.OdooDataWorkflowRunner,
                "update_addons",
            ) as update_addons,
        ):
            result = odoo_data_workflows.main(["--update-only"])

        self.assertEqual(result, odoo_data_workflows.ExitCode.BOOTSTRAP_FAILED)
        update_addons.assert_not_called()

    def test_module_update_releases_metadata_connection_before_odoo_command(self) -> None:
        runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("cm_website", "installed")]
        runner.local.db_conn = connection

        def assert_connection_released(_command: str) -> None:
            self.assertIsNone(runner.local.db_conn)
            connection.close.assert_called_once_with()

        with (
            patch.object(runner, "_resolve_addons_paths", return_value=(Path("/addons"),)),
            patch.object(runner, "run_command", side_effect=assert_connection_released),
        ):
            runner._apply_module_updates(
                ["cm_website"],
                modules_source_label="test",
                local_module_paths={"cm_website": Path("/addons/cm_website")},
            )

        self.assertIsNone(runner.local.db_conn)
        connection.close.assert_called_once_with()

    def test_module_update_keeps_connection_reset_when_odoo_command_fails(self) -> None:
        runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("cm_website", "installed")]
        runner.local.db_conn = connection

        def fail_after_connection_release(command: str) -> None:
            self.assertIsNone(runner.local.db_conn)
            raise subprocess.CalledProcessError(returncode=1, cmd=command)

        with (
            patch.object(runner, "_resolve_addons_paths", return_value=(Path("/addons"),)),
            patch.object(runner, "run_command", side_effect=fail_after_connection_release),
            self.assertRaises(odoo_data_workflows.OdooRestorerError),
        ):
            runner._apply_module_updates(
                ["cm_website"],
                modules_source_label="test",
                local_module_paths={"cm_website": Path("/addons/cm_website")},
            )

        self.assertIsNone(runner.local.db_conn)
        connection.close.assert_called_once_with()

    def test_openupgrade_snapshot_releases_metadata_connection(self) -> None:
        runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("base", "installed"), ("cm_website", "to upgrade")]
        runner.local.db_conn = connection

        runner.snapshot_module_states_before_openupgrade()

        self.assertEqual(
            runner._pre_openupgrade_module_states,
            {"base": "installed", "cm_website": "to upgrade"},
        )
        self.assertIsNone(runner.local.db_conn)
        connection.close.assert_called_once_with()

    def test_openupgrade_releases_cached_connection_before_odoo_command(self) -> None:
        settings = self._local_settings()
        settings.openupgrade_enabled = True
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        connection = MagicMock()
        runner.local.db_conn = connection

        def assert_connection_released(_command: str) -> None:
            self.assertIsNone(runner.local.db_conn)
            connection.close.assert_called_once_with()

        with (
            patch.object(
                runner,
                "_resolve_openupgrade_assets",
                return_value=([Path("/addons/openupgrade_scripts/scripts")], Path("/addons/openupgrade_framework")),
            ),
            patch.object(runner, "run_command", side_effect=assert_connection_released),
        ):
            runner.run_openupgrade()

        self.assertIsNone(runner.local.db_conn)
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
