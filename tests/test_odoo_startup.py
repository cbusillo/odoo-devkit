from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_startup_module() -> types.ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "run_odoo_startup.py"
    spec = importlib.util.spec_from_file_location("odoo_devkit_run_odoo_startup_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module

    psycopg2_module = types.ModuleType("psycopg2")
    psycopg2_module.errors = types.SimpleNamespace(UndefinedTable=RuntimeError)

    def _unexpected_connect(*unused_args: object, **unused_kwargs: object) -> None:
        _ = unused_args, unused_kwargs
        raise AssertionError("psycopg2.connect should not be called in this test")

    psycopg2_module.connect = _unexpected_connect

    with patch.dict(sys.modules, {"psycopg2": psycopg2_module}):
        spec.loader.exec_module(module)
    return module


odoo_startup = _load_startup_module()


class OdooStartupDependencySyncTests(unittest.TestCase):
    def test_load_settings_reads_platform_instance(self) -> None:
        environment = {
            "PLATFORM_INSTANCE": "local",
            "ODOO_DB_NAME": "opw",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_PORT": "5432",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "database-password",
            "ODOO_MASTER_PASSWORD": "master-password",
            "ODOO_ADDONS_PATH": "/odoo/addons",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = odoo_startup._load_settings(argparse.Namespace(config_path="/tmp/generated.conf"))

        self.assertEqual(settings.platform_instance, "local")

    @staticmethod
    def test_sync_python_dependencies_runs_for_local_dev_runtime() -> None:
        settings = odoo_startup.StartupSettings(
            config_path="/tmp/generated.conf",
            base_config_path="/tmp/base.conf",
            platform_instance="local",
            database_name="opw",
            database_host="database",
            database_port=5432,
            database_user="odoo",
            database_password="database-password",
            master_password="master-password",
            admin_login="admin",
            admin_password="",
            addons_path="/odoo/addons",
            data_dir="/volumes/data",
            list_db="False",
            install_modules=("opw_custom",),
            data_workflow_lock_file="/volumes/data/.data_workflow_in_progress",
            data_workflow_lock_timeout_seconds=7200,
            ready_timeout_seconds=180,
            poll_interval_seconds=2.0,
        )

        with (
            patch.dict(os.environ, {"ODOO_DEV_MODE": "reload"}, clear=True),
            patch.object(odoo_startup, "_install_local_addon_dependencies") as mocked_install_dependencies,
        ):
            odoo_startup._sync_python_dependencies_if_needed(settings)

        mocked_install_dependencies.assert_called_once_with("dev")

    @staticmethod
    def test_sync_python_dependencies_skips_non_local_runtime() -> None:
        settings = odoo_startup.StartupSettings(
            config_path="/tmp/generated.conf",
            base_config_path="/tmp/base.conf",
            platform_instance="prod",
            database_name="opw",
            database_host="database",
            database_port=5432,
            database_user="odoo",
            database_password="database-password",
            master_password="master-password",
            admin_login="admin",
            admin_password="",
            addons_path="/odoo/addons",
            data_dir="/volumes/data",
            list_db="False",
            install_modules=("opw_custom",),
            data_workflow_lock_file="/volumes/data/.data_workflow_in_progress",
            data_workflow_lock_timeout_seconds=7200,
            ready_timeout_seconds=180,
            poll_interval_seconds=2.0,
        )

        with patch.object(odoo_startup, "_install_local_addon_dependencies") as mocked_install_dependencies:
            odoo_startup._sync_python_dependencies_if_needed(settings)

        mocked_install_dependencies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
