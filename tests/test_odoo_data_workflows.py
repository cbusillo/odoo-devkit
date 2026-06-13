from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_data_workflows_module() -> types.ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "run_odoo_data_workflows.py"
    spec = importlib.util.spec_from_file_location("odoo_devkit_run_odoo_data_workflows_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module

    psycopg2_module = types.ModuleType("psycopg2")
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
        spec.loader.exec_module(module)
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


if __name__ == "__main__":
    unittest.main()
