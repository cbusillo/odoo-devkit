from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing
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
    def test_bootstrap_allows_configured_mail_but_sanitized_restores_block_it(self) -> None:
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute(
                "CREATE TABLE ir_mail_server (name TEXT, smtp_port INTEGER, smtp_host TEXT, smtp_encryption TEXT, "
                "active BOOLEAN, smtp_authentication TEXT, smtp_user TEXT, smtp_pass TEXT)"
            )
            runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(), upstream=None, env_file=None)
            runner.local.db_conn = types.SimpleNamespace(cursor=lambda: closing(database.cursor()), commit=database.commit)
            with patch.multiple(
                runner,
                _resolve_filestore_owner=MagicMock(return_value=None),
                database_exists=MagicMock(return_value=False),
                _clean_filestore=MagicMock(),
                normalize_filestore_permissions=MagicMock(),
                create_database=MagicMock(),
                _reset_db_connection=MagicMock(),
                needs_base_install=MagicMock(return_value=False),
                install_addons=MagicMock(),
                update_addons=MagicMock(),
                call_odoo_sql=MagicMock(return_value=[]),
                assert_install_queue_is_resolvable=MagicMock(),
                apply_environment_overrides=MagicMock(),
                ensure_admin_user=MagicMock(),
                assert_core_schema_healthy=MagicMock(),
                ensure_gpt_users=MagicMock(),
            ):
                runner.run_bootstrap(do_sanitize=True)
            self.assertEqual(database.execute("SELECT count(*) FROM ir_mail_server WHERE active = true").fetchone()[0], 0)
            database.execute(
                "INSERT INTO ir_mail_server VALUES ('Production', 587, 'smtp.example.test', 'starttls', true, 'login', "
                "'mailbox@example.test', 'copied-secret')"
            )
            with patch.object(runner, "call_odoo_sql", return_value=[]):
                runner.sanitize_database()
                runner.sanitize_database()
            self.assertEqual(
                database.execute("SELECT smtp_host, smtp_port FROM ir_mail_server WHERE active = true").fetchall(),
                [("invalid", 1025)],
            )
            self.assertEqual(
                database.execute(
                    "SELECT count(*) FROM ir_mail_server WHERE smtp_user IS NOT NULL OR smtp_pass IS NOT NULL"
                ).fetchone()[0],
                0,
            )

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


class DataWorkflowGuardTests(unittest.TestCase):
    _LOCAL_ENVIRONMENT = {
        "ODOO_DB_HOST": "database",
        "ODOO_DB_USER": "odoo",
        "ODOO_DB_PASSWORD": "database-password",
        "ODOO_DB_NAME": "cm",
        "ODOO_FILESTORE_PATH": "/volumes/data/filestore/cm",
    }
    _UPSTREAM_ENVIRONMENT = {
        "ODOO_UPSTREAM_HOST": "upstream.example.test",
        "ODOO_UPSTREAM_USER": "backup",
        "ODOO_UPSTREAM_DB_NAME": "cm",
        "ODOO_UPSTREAM_DB_USER": "odoo",
        "ODOO_UPSTREAM_FILESTORE_PATH": "/srv/filestore/cm",
    }

    def setUp(self) -> None:
        super().setUp()
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.lock_path = Path(temporary_directory.name) / "data" / ".data_workflow_in_progress"

    def _run_main(
        self, arguments: list[str], *, with_upstream: bool, restore_error: Exception | None = None
    ) -> tuple[odoo_data_workflows.ExitCode, MagicMock, MagicMock]:
        environment = {**self._LOCAL_ENVIRONMENT, "ODOO_DATA_WORKFLOW_LOCK_FILE": str(self.lock_path)}
        if with_upstream:
            environment.update(self._UPSTREAM_ENVIRONMENT)
        runner_class = odoo_data_workflows.OdooDataWorkflowRunner
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(runner_class, "run_bootstrap") as run_bootstrap,
            patch.object(runner_class, "run_restore", side_effect=restore_error) as run_restore,
        ):
            result = odoo_data_workflows.main(arguments)
        return result, run_bootstrap, run_restore

    def test_missing_upstream_refuses_instead_of_bootstrapping(self) -> None:
        result, run_bootstrap, run_restore = self._run_main([], with_upstream=False)

        self.assertEqual(result, odoo_data_workflows.ExitCode.INVALID_ARGS)
        run_bootstrap.assert_not_called()
        run_restore.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_explicit_bootstrap_runs_without_upstream(self) -> None:
        result, run_bootstrap, run_restore = self._run_main(["--bootstrap"], with_upstream=False)

        self.assertEqual(result, odoo_data_workflows.ExitCode.SUCCESS)
        run_bootstrap.assert_called_once()
        run_restore.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_failed_restore_does_not_fall_back_to_bootstrap(self) -> None:
        result, run_bootstrap, _ = self._run_main(
            [], with_upstream=True, restore_error=odoo_data_workflows.OdooRestorerError("rsync failed")
        )

        self.assertEqual(result, odoo_data_workflows.ExitCode.RESTORE_FAILED)
        run_bootstrap.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_existing_workflow_lock_blocks_a_second_workflow_and_is_left_in_place(self) -> None:
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text("pid=1\n", encoding="utf-8")

        result, run_bootstrap, run_restore = self._run_main(["--bootstrap"], with_upstream=True)

        self.assertNotEqual(result, odoo_data_workflows.ExitCode.SUCCESS)
        run_bootstrap.assert_not_called()
        run_restore.assert_not_called()
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), "pid=1\n")

    def _restore_runner(self, **setting_overrides: object) -> tuple[object, MagicMock]:
        settings = odoo_data_workflows.LocalServerSettings(**self._LOCAL_ENVIRONMENT, **setting_overrides)
        upstream = odoo_data_workflows.UpstreamServerSettings(**self._UPSTREAM_ENVIRONMENT)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=upstream, env_file=None)
        runner.local.db_conn = MagicMock()
        filestore_process = MagicMock()
        filestore_process.wait.return_value = 0
        restore_steps = {
            "_assert_filestore_capacity": MagicMock(),
            "_resolve_filestore_owner": MagicMock(return_value=None),
            "overwrite_filestore": MagicMock(return_value=filestore_process),
            "overwrite_database": MagicMock(),
            "normalize_filestore_permissions": MagicMock(),
            "snapshot_module_states_before_openupgrade": MagicMock(),
            "run_openupgrade": MagicMock(),
            "sanitize_database": MagicMock(),
            "install_addons": MagicMock(),
            "update_addons": MagicMock(),
            "connect_to_db": MagicMock(),
            "reconcile_missing_manifest_install_queue": MagicMock(),
            "assert_install_queue_is_resolvable": MagicMock(),
            "apply_environment_overrides": MagicMock(),
            "assert_core_schema_healthy": MagicMock(),
            "ensure_gpt_users": MagicMock(),
            "drop_database": MagicMock(),
        }
        patcher = patch.multiple(runner, **restore_steps)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner, restore_steps["drop_database"]

    def test_restore_drops_the_half_restored_database_when_a_later_step_fails(self) -> None:
        failures = (
            ("OpenUpgrade", {"OPENUPGRADE_ENABLED": True}, "run_openupgrade", odoo_data_workflows.OdooRestorerError),
            ("sanitize", {}, "sanitize_database", odoo_data_workflows.OdooDatabaseUpdateError),
            ("environment overrides", {}, "apply_environment_overrides", odoo_data_workflows.OdooDatabaseUpdateError),
        )
        for step_label, setting_overrides, failing_step, error_type in failures:
            with self.subTest(step_label):
                runner, drop_database = self._restore_runner(**setting_overrides)
                getattr(runner, failing_step).side_effect = error_type(f"{step_label} failed")

                with self.assertRaises(error_type):
                    runner.run_restore()

                drop_database.assert_called_once_with()

    def test_successful_restore_keeps_the_database(self) -> None:
        runner, drop_database = self._restore_runner()

        runner.run_restore()

        drop_database.assert_not_called()
        runner.assert_core_schema_healthy.assert_called_once_with()

    def test_restore_does_not_start_when_upstream_settings_are_missing(self) -> None:
        settings = odoo_data_workflows.LocalServerSettings(**self._LOCAL_ENVIRONMENT)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)

        with patch.object(runner, "overwrite_database") as overwrite_database:
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore()

        overwrite_database.assert_not_called()

    def test_workflow_lock_admits_one_holder_until_released(self) -> None:
        settings = odoo_data_workflows.LocalServerSettings(
            **self._LOCAL_ENVIRONMENT, ODOO_DATA_WORKFLOW_LOCK_FILE=str(self.lock_path)
        )
        first_runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        second_runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)

        first_runner.acquire_data_workflow_lock()
        with self.assertRaises(odoo_data_workflows.OdooRestorerError):
            second_runner.acquire_data_workflow_lock()
        first_runner.release_data_workflow_lock()
        second_runner.acquire_data_workflow_lock()

        self.assertTrue(self.lock_path.exists())


class _FakeCronTable:
    """Tracks ir.cron activity through the SQL calls sanitize_database makes."""

    def __init__(self, cron_names: tuple[str, ...], *, stuck_cron_names: tuple[str, ...] = ()) -> None:
        self.active_by_name = dict.fromkeys(cron_names, True)
        self.stuck_cron_names = stuck_cron_names

    def call_odoo_sql(self, sql_call: object, call_type: object) -> list[tuple] | None:
        if sql_call.model != "ir.cron":
            return []
        if call_type == odoo_data_workflows.SqlCallType.UPDATE and sql_call.data.key == "active":
            for cron_name in self.active_by_name:
                if cron_name not in self.stuck_cron_names:
                    self.active_by_name[cron_name] = sql_call.data.value
            return None
        if call_type == odoo_data_workflows.SqlCallType.SELECT:
            return [
                (index, None, None, None, None, None, None, cron_name)
                for index, (cron_name, active) in enumerate(self.active_by_name.items(), start=1)
                if active
            ]
        return []


class SanitizeCronTests(unittest.TestCase):
    def _sanitize(self, cron_table: _FakeCronTable, **setting_overrides: object) -> None:
        settings = odoo_data_workflows.LocalServerSettings(
            ODOO_DB_HOST="database",
            ODOO_DB_USER="odoo",
            ODOO_DB_PASSWORD="database-password",
            ODOO_DB_NAME="cm",
            ODOO_FILESTORE_PATH="/volumes/data/filestore/cm",
            **setting_overrides,
        )
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE TABLE ir_mail_server (active BOOLEAN, smtp_user TEXT, smtp_pass TEXT)")
            connection = types.SimpleNamespace(cursor=lambda: closing(database.cursor()))
            with (
                patch.object(runner, "connect_to_db", return_value=connection),
                patch.object(runner, "call_odoo_sql", side_effect=cron_table.call_odoo_sql),
            ):
                runner.sanitize_database(block_smtp_fallback=False)

    def test_sanitize_disables_every_cron_by_default(self) -> None:
        cron_table = _FakeCronTable(("Mail: send queue", "Shopify: sync orders"))

        self._sanitize(cron_table)

        self.assertFalse(any(cron_table.active_by_name.values()))

    def test_sanitize_fails_when_a_cron_stays_active(self) -> None:
        cron_table = _FakeCronTable(("Mail: send queue", "Shopify: sync orders"), stuck_cron_names=("Shopify: sync orders",))

        with self.assertRaisesRegex(odoo_data_workflows.OdooDatabaseUpdateError, "Shopify: sync orders"):
            self._sanitize(cron_table)

    def test_sanitize_leaves_crons_alone_when_cron_disabling_is_turned_off(self) -> None:
        cron_table = _FakeCronTable(("Mail: send queue",))

        self._sanitize(cron_table, ENV_OVERRIDE_DISABLE_CRON=False)

        self.assertEqual(cron_table.active_by_name, {"Mail: send queue": True})


if __name__ == "__main__":
    unittest.main()
