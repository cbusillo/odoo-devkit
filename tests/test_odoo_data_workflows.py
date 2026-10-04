from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from odoo_devkit.local_runtime import load_environment_from_explicit_payload


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


class UpstreamRestoreFailureTests(unittest.TestCase):
    @contextmanager
    def restore_fixture(
        self, *, ssh_status: int = 0, validation_status: int = 0, restore_status: int = 0
    ) -> Iterator[tuple[Any, Path]]:
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            binary_directory = root / "bin"
            binary_directory.mkdir()
            programs = {
                "ssh": 'printf "fixture archive"\nexit "$FIXTURE_SSH_STATUS"\n',
                "pg_restore": (
                    'if [ "$1" = "--file=/dev/null" ]; then\n'
                    '  exit "$FIXTURE_VALIDATION_STATUS"\n'
                    "fi\n"
                    'if [ "$FIXTURE_RESTORE_STATUS" != 0 ]; then exit "$FIXTURE_RESTORE_STATUS"; fi\n'
                    'printf restored > "$FIXTURE_DATABASE"\n'
                ),
                "dropdb": 'printf dropped > "$FIXTURE_DATABASE"\n',
                "createdb": 'printf empty > "$FIXTURE_DATABASE"\n',
            }
            for name, program in programs.items():
                path = binary_directory / name
                path.write_text("#!/bin/sh\n" + program)
                path.chmod(0o755)
            database = root / "database"
            filestore = root / "filestore"
            database.write_text("original database")
            filestore.write_text("original filestore")
            runner = odoo_data_workflows.OdooDataWorkflowRunner(
                local=OdooDataWorkflowShellEnvironmentTests._local_settings(),
                upstream=odoo_data_workflows.UpstreamServerSettings(
                    ODOO_UPSTREAM_HOST="upstream.example.invalid",
                    ODOO_UPSTREAM_USER="backup",
                    ODOO_UPSTREAM_DB_NAME="source",
                    ODOO_UPSTREAM_DB_USER="odoo",
                    ODOO_UPSTREAM_FILESTORE_PATH="/source/filestore",
                ),
                env_file=None,
            )
            runner.os_env.update(
                PATH=str(binary_directory) + os.pathsep + os.environ["PATH"],
                FIXTURE_DATABASE=str(database),
                FIXTURE_SSH_STATUS=str(ssh_status),
                FIXTURE_VALIDATION_STATUS=str(validation_status),
                FIXTURE_RESTORE_STATUS=str(restore_status),
            )
            runner.local.filestore_path = root / "data" / "filestore"
            runner.local.data_workflow_lock_file = root / "data" / ".data_workflow_in_progress"

            def overwrite_filestore(_owner: str | None) -> MagicMock:
                filestore.write_text("restored filestore")
                process = MagicMock()
                process.wait.return_value = 0
                return process

            stack.enter_context(
                patch.multiple(
                    runner,
                    _assert_filestore_capacity=MagicMock(),
                    _resolve_filestore_owner=MagicMock(return_value=None),
                    overwrite_filestore=overwrite_filestore,
                    _set_database_allow_connections=MagicMock(),
                    terminate_all_db_connections=MagicMock(),
                    normalize_filestore_permissions=MagicMock(),
                    fingerprint_restored_credentials=MagicMock(),
                    neutralize_production_credentials=MagicMock(),
                    verify_production_credentials_cleared=MagicMock(),
                    install_addons=MagicMock(),
                    update_addons=MagicMock(),
                    connect_to_db=MagicMock(),
                    reconcile_missing_manifest_install_queue=MagicMock(),
                    assert_install_queue_is_resolvable=MagicMock(),
                    apply_environment_overrides=MagicMock(),
                    assert_core_schema_healthy=MagicMock(),
                    ensure_gpt_users=MagicMock(),
                )
            )
            yield runner, root

    def test_failed_ssh_dump_preserves_database_and_filestore(self) -> None:
        with self.restore_fixture(ssh_status=255) as (runner, root):
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "original database")
            self.assertEqual((root / "filestore").read_text(), "original filestore")
            self.assertEqual(list(root.rglob("database.*")), [])

    def test_pipeline_reports_failed_producer_even_when_compression_succeeds(self) -> None:
        with self.restore_fixture(ssh_status=255) as (runner, _root):
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_command("ssh upstream.example.invalid | gzip > /dev/null")

    def test_invalid_archive_preserves_database_and_filestore(self) -> None:
        with self.restore_fixture(validation_status=1) as (runner, root):
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "original database")
            self.assertEqual((root / "filestore").read_text(), "original filestore")

    def test_failed_target_restore_retains_verified_dump_for_recovery(self) -> None:
        with self.restore_fixture(restore_status=1) as (runner, root):
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            retained = list(root.rglob("database.dump"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_text(), "fixture archive")
            # A partial pg_restore can already hold copied credentials, so it is dropped.
            self.assertEqual((root / "database").read_text(), "dropped")

    def test_successful_restore_removes_temporary_dump_after_target_is_restored(self) -> None:
        with self.restore_fixture() as (runner, root):
            runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "restored")
            self.assertEqual((root / "filestore").read_text(), "restored filestore")
            self.assertEqual(list(root.rglob("database.*")), [])

    def test_failed_module_update_drops_the_restored_database_and_retains_dump(self) -> None:
        with self.restore_fixture() as (runner, root):
            runner.update_addons.side_effect = odoo_data_workflows.OdooRestorerError("module update failed")
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "dropped")
            retained = list(root.rglob("database.dump"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_text(), "fixture archive")

    def test_failed_recapture_preserves_the_previous_verified_dump(self) -> None:
        with self.restore_fixture(restore_status=1) as (runner, root):
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            retained = next(root.rglob("database.dump"))
            retained.write_text("previous verified archive")
            runner.os_env["FIXTURE_SSH_STATUS"] = "255"
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            self.assertEqual(retained.read_text(), "previous verified archive")
            self.assertEqual(list(root.rglob("database.partial")), [])

    def test_later_failed_restores_keep_one_verified_dump_on_the_data_volume(self) -> None:
        with self.restore_fixture(restore_status=1) as (runner, root):
            for _ in range(2):
                with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                    runner.run_restore(do_sanitize=False)
            retained = list((root / "data").rglob("database.dump"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(root.rglob("database.partial")), [])

    def test_interrupted_capture_does_not_leave_a_verified_dump(self) -> None:
        with self.restore_fixture() as (runner, root):

            def interrupt_capture(path: Path) -> None:
                path.write_text("partial archive")
                raise KeyboardInterrupt

            with patch.object(runner, "capture_upstream_database", side_effect=interrupt_capture):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_restore(do_sanitize=False)
            self.assertEqual(list(root.rglob("database.*")), [])
            self.assertEqual((root / "database").read_text(), "original database")
            self.assertEqual((root / "filestore").read_text(), "original filestore")

    def test_restore_uses_writable_lock_parent_when_filestore_root_is_read_only(self) -> None:
        with self.restore_fixture() as (runner, root):
            runner.local.filestore_path.mkdir(parents=True)
            runner.local.filestore_path.chmod(0o555)
            try:
                runner.run_restore(do_sanitize=False)
            finally:
                runner.local.filestore_path.chmod(0o755)
            self.assertEqual((root / "database").read_text(), "restored")
            self.assertEqual(list(root.rglob("database.*")), [])

    def test_capacity_failure_after_capture_preserves_target_and_verified_dump(self) -> None:
        with self.restore_fixture() as (runner, root):
            runner._assert_filestore_capacity.side_effect = [
                None,
                odoo_data_workflows.OdooRestorerError("Insufficient local storage after capture"),
            ]
            with self.assertRaises(odoo_data_workflows.OdooRestorerError):
                runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "original database")
            self.assertEqual((root / "filestore").read_text(), "original filestore")
            retained = list(root.rglob("database.dump"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_text(), "fixture archive")

    def test_recovery_cleanup_failure_does_not_report_a_failed_restore(self) -> None:
        with self.restore_fixture() as (runner, root):
            with patch.object(odoo_data_workflows.shutil, "rmtree", side_effect=PermissionError("cleanup denied")):
                runner.run_restore(do_sanitize=False)
            self.assertEqual((root / "database").read_text(), "restored")
            self.assertEqual(len(list(root.rglob("database.dump"))), 1)

    def test_restore_io_error_returns_the_restore_failure_exit_code(self) -> None:
        with self.restore_fixture() as (runner, root):
            with (
                patch.object(odoo_data_workflows, "LocalServerSettings", return_value=runner.local),
                patch.object(odoo_data_workflows, "UpstreamServerSettings", return_value=runner.upstream),
                patch.object(odoo_data_workflows, "OdooDataWorkflowRunner", return_value=runner),
                patch.object(runner, "run_restore", side_effect=PermissionError("capture directory denied")),
            ):
                result = odoo_data_workflows.main(["--no-sanitize"])
            self.assertEqual(result, odoo_data_workflows.ExitCode.RESTORE_FAILED)
            self.assertFalse(runner.local.data_workflow_lock_file.exists())
            self.assertEqual((root / "database").read_text(), "original database")

    def _run_main_restore(self, runner: Any) -> tuple[int, str]:
        with (
            patch.object(odoo_data_workflows, "LocalServerSettings", return_value=runner.local),
            patch.object(odoo_data_workflows, "UpstreamServerSettings", return_value=runner.upstream),
            patch.object(odoo_data_workflows, "OdooDataWorkflowRunner", return_value=runner),
            self.assertLogs(odoo_data_workflows._logger, level="INFO") as captured,
        ):
            result = odoo_data_workflows.main(["--no-sanitize"])
        return result, "\n".join(captured.output)

    def test_unreachable_source_exits_non_zero_before_anything_is_dropped(self) -> None:
        # ssh exits 255 for an unreachable host and for a failed host-key check.
        with self.restore_fixture(ssh_status=255) as (runner, root):
            result, log_output = self._run_main_restore(runner)
            self.assertEqual(result, odoo_data_workflows.ExitCode.RESTORE_FAILED)
            self.assertEqual((root / "database").read_text(), "original database")
            self.assertEqual((root / "filestore").read_text(), "original filestore")
            self.assertIn("before target database or filestore replacement", log_output)
            self.assertNotIn("dropdb", log_output)
            self.assertNotIn("intact", log_output)

    def test_failed_pg_restore_exits_non_zero_and_does_not_claim_the_target_is_intact(self) -> None:
        with self.restore_fixture(restore_status=1) as (runner, root):
            result, log_output = self._run_main_restore(runner)
            self.assertEqual(result, odoo_data_workflows.ExitCode.RESTORE_FAILED)
            # The target was recreated before pg_restore failed; the partial copy is dropped.
            self.assertEqual((root / "database").read_text(), "dropped")
            self.assertIn("Upstream restore failed", log_output)
            self.assertIn("may be partially changed", log_output)
            self.assertNotIn("intact", log_output)


class OdooDataWorkflowShellEnvironmentTests(unittest.TestCase):
    @staticmethod
    def _mail_database() -> sqlite3.Connection:
        database = sqlite3.connect(":memory:")
        database.execute(
            "CREATE TABLE ir_mail_server (name TEXT, smtp_port INTEGER, smtp_host TEXT, smtp_encryption TEXT, "
            "active BOOLEAN, smtp_authentication TEXT, smtp_user TEXT, smtp_pass TEXT)"
        )
        return database

    @staticmethod
    def _add_production_mail_server(database: sqlite3.Connection) -> None:
        database.execute(
            "INSERT INTO ir_mail_server VALUES ('Production', 587, 'smtp.example.test', 'starttls', true, 'login', "
            "'mailbox@example.test', 'copied-secret')"
        )

    def _mail_runner(self, database: sqlite3.Connection, platform_instance: str) -> Any:
        runner = odoo_data_workflows.OdooDataWorkflowRunner(self._local_settings(platform_instance), upstream=None, env_file=None)
        runner.local.db_conn = types.SimpleNamespace(cursor=lambda: closing(database.cursor()), commit=database.commit)
        return runner

    def _run_bootstrap(self, runner: Any) -> None:
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

    def test_production_bootstrap_allows_configured_mail_but_sanitized_restores_block_it(self) -> None:
        with closing(self._mail_database()) as database:
            runner = self._mail_runner(database, "prod")
            self._run_bootstrap(runner)
            self.assertEqual(database.execute("SELECT count(*) FROM ir_mail_server WHERE active = true").fetchone()[0], 0)
            self._add_production_mail_server(database)
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

    def test_non_production_bootstrap_blocks_configured_mail(self) -> None:
        with closing(self._mail_database()) as database:
            self._run_bootstrap(self._mail_runner(database, "testing"))
            self.assertEqual(
                database.execute("SELECT smtp_host, smtp_port FROM ir_mail_server WHERE active = true").fetchall(),
                [("invalid", 1025)],
            )

    def test_non_production_mail_block_is_repeatable_and_clears_credentials(self) -> None:
        with closing(self._mail_database()) as database:
            self._add_production_mail_server(database)
            runner = self._mail_runner(database, "testing")
            with patch.object(runner, "connect_to_db", return_value=runner.local.db_conn):
                runner.block_outgoing_mail_outside_production()
                runner.block_outgoing_mail_outside_production()
            self.assertEqual(
                database.execute("SELECT name, active, smtp_user, smtp_pass FROM ir_mail_server ORDER BY name").fetchall(),
                [("Production", 0, None, None), ("neutralization - disable emails", 1, None, None)],
            )

    def test_production_mail_is_left_alone(self) -> None:
        with closing(self._mail_database()) as database:
            self._add_production_mail_server(database)
            runner = self._mail_runner(database, "prod")
            with patch.object(runner, "connect_to_db", return_value=runner.local.db_conn):
                runner.block_outgoing_mail_outside_production()
            self.assertEqual(
                database.execute("SELECT name, active, smtp_user FROM ir_mail_server").fetchall(),
                [("Production", 1, "mailbox@example.test")],
            )

    @staticmethod
    def _local_settings(platform_instance: str = "") -> object:
        return odoo_data_workflows.LocalServerSettings(
            PLATFORM_INSTANCE=platform_instance,
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
            patch.object(
                runner,
                "block_outgoing_mail_outside_production",
                side_effect=lambda: calls.append("block_outgoing_mail_outside_production"),
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
                "block_outgoing_mail_outside_production",
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
        environment = {
            **self._LOCAL_ENVIRONMENT,
            "ODOO_DATA_WORKFLOW_LOCK_FILE": str(self.lock_path),
            **setting_overrides,
        }
        settings = odoo_data_workflows.LocalServerSettings(**environment)
        upstream = odoo_data_workflows.UpstreamServerSettings(**self._UPSTREAM_ENVIRONMENT)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=upstream, env_file=None)
        runner.local.db_conn = MagicMock()
        filestore_process = MagicMock()
        filestore_process.wait.return_value = 0

        def capture_archive(path: Path) -> None:
            path.write_bytes(b"fixture archive")

        restore_steps = {
            "_assert_filestore_capacity": MagicMock(),
            "capture_upstream_database": MagicMock(side_effect=capture_archive),
            "_resolve_filestore_owner": MagicMock(return_value=None),
            "overwrite_filestore": MagicMock(return_value=filestore_process),
            "overwrite_database": MagicMock(),
            "normalize_filestore_permissions": MagicMock(),
            "fingerprint_restored_credentials": MagicMock(),
            "neutralize_production_credentials": MagicMock(),
            "verify_production_credentials_cleared": MagicMock(),
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

    def test_restore_drops_the_restored_database_when_any_step_before_the_settings_apply_fails(self) -> None:
        database_error = odoo_data_workflows.psycopg2.Error
        failures = (
            ("credential clearing", {}, "neutralize_production_credentials", database_error),
            ("credential read-back", {}, "verify_production_credentials_cleared", odoo_data_workflows.OdooDatabaseUpdateError),
            ("filestore permissions", {}, "normalize_filestore_permissions", PermissionError),
            ("OpenUpgrade", {"OPENUPGRADE_ENABLED": True}, "run_openupgrade", odoo_data_workflows.OdooRestorerError),
            ("sanitize", {}, "sanitize_database", odoo_data_workflows.OdooDatabaseUpdateError),
            ("sanitize database error", {}, "sanitize_database", database_error),
            ("addon install", {}, "install_addons", odoo_data_workflows.OdooRestorerError),
            ("addon update", {}, "update_addons", odoo_data_workflows.OdooRestorerError),
            ("install queue", {}, "assert_install_queue_is_resolvable", odoo_data_workflows.OdooDatabaseUpdateError),
            ("environment overrides", {}, "apply_environment_overrides", odoo_data_workflows.OdooDatabaseUpdateError),
            ("interrupt", {}, "update_addons", KeyboardInterrupt),
        )
        for step_label, setting_overrides, failing_step, error_type in failures:
            with self.subTest(step_label):
                runner, drop_database = self._restore_runner(**setting_overrides)
                getattr(runner, failing_step).side_effect = error_type(f"{step_label} failed")

                with self.assertRaises(error_type):
                    runner.run_restore()

                drop_database.assert_called_once_with()

    def test_failed_filestore_copy_drops_the_restored_database(self) -> None:
        runner, drop_database = self._restore_runner()
        runner.overwrite_filestore.return_value.wait.return_value = 23

        with self.assertRaisesRegex(odoo_data_workflows.OdooRestorerError, "rsync failed"):
            runner.run_restore()

        drop_database.assert_called_once_with()
        runner.install_addons.assert_not_called()

    def test_a_failed_drop_still_raises_the_original_restore_error(self) -> None:
        runner, drop_database = self._restore_runner()
        runner.install_addons.side_effect = odoo_data_workflows.OdooRestorerError("install failed")
        drop_database.side_effect = odoo_data_workflows.OdooRestorerError("dropdb failed")

        with self.assertRaisesRegex(odoo_data_workflows.OdooRestorerError, "install failed"):
            runner.run_restore()

    def test_failures_after_the_settings_apply_keep_the_sanitized_database(self) -> None:
        runner, drop_database = self._restore_runner()
        runner.ensure_gpt_users.side_effect = odoo_data_workflows.OdooRestorerError("service user failed")

        with self.assertRaises(odoo_data_workflows.OdooRestorerError):
            runner.run_restore()

        drop_database.assert_not_called()

    def test_credentials_are_cleared_before_odoo_runs_and_again_before_the_settings_apply(self) -> None:
        for do_sanitize in (True, False):
            with self.subTest(do_sanitize=do_sanitize):
                runner, _drop_database = self._restore_runner(OPENUPGRADE_ENABLED=True, OPENUPGRADE_SKIP_UPDATE_ADDONS=False)
                calls: list[str] = []
                for step in (
                    "overwrite_database",
                    "fingerprint_restored_credentials",
                    "neutralize_production_credentials",
                    "run_openupgrade",
                    "sanitize_database",
                    "install_addons",
                    "update_addons",
                    "verify_production_credentials_cleared",
                    "apply_environment_overrides",
                ):
                    getattr(runner, step).side_effect = lambda *_args, _step=step, _calls=calls, **_kwargs: _calls.append(_step)

                runner.run_restore(do_sanitize=do_sanitize)

                expected = [
                    "overwrite_database",
                    "fingerprint_restored_credentials",
                    "neutralize_production_credentials",
                    "run_openupgrade",
                    *(["sanitize_database"] if do_sanitize else []),
                    "install_addons",
                    "update_addons",
                    "neutralize_production_credentials",
                    "verify_production_credentials_cleared",
                    "apply_environment_overrides",
                ]
                self.assertEqual(calls, expected)

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


PRODUCTION_PARAMETERS = {
    "shopify.shop_url_key": "production-store",
    "shopify.api_token": "production-shopify-token",
    "shopify.webhook_key": "production-webhook-key",
    "shopify.shop_url": "https://production-store.example.test",
    "shopify.test_store": "False",
    "shopify.last_product_import_time": "2026-09-28 14:57:00",
    "printnode.api_key": "production-printnode-key",
    "web_map.token_map_box": "production-mapbox-token",
    "unsplash.access_key": "production-unsplash-key",
    "discuss.tenor_api_key": "production-tenor-key",
    "mail.web_push_vapid_private_key": "production-vapid-private",
    "mail.web_push_vapid_public_key": "production-vapid-public",
    "database.secret": "production-database-secret",
    "fishbowl.host": "fishbowl.production.example.test",
    "fishbowl.db": "production-fishbowl",
    "fishbowl.user": "fishbowl-reader",
    "fishbowl.password": "production-fishbowl-password",
    "repairshopr.sync_db.host": "repairshopr.production.example.test",
    "repairshopr.sync_db.name": "repairshopr",
    "repairshopr.sync_db.user": "repairshopr-sync",
    "repairshopr.sync_db.password": "production-repairshopr-password",
    "cm_data.db.host": "cm-data.production.example.test",
    "cm_data.db.name": "cm_data",
    "cm_data.db.user": "cm-data-sync",
    "cm_data.db.password": "production-cm-data-password",
    "google_gmail_client_secret": "production-gmail-client-secret",
    "microsoft_outlook_client_secret": "production-outlook-client-secret",
}
IMPORT_SOURCE_PARAMETERS = {
    key: value for key, value in PRODUCTION_PARAMETERS.items() if key.startswith(("fishbowl.", "repairshopr.", "cm_data."))
}
UNRELATED_PARAMETERS = {
    "shopify.api_version": "2026-07",
    "shopify.pause_webhook_processing": "False",
    "web.base.url": "https://testing.example.test",
    "database.uuid": "copied-uuid",
    "fishbowl.port": "3306",
    "cm_data.last_sync_at": "2026-09-28 14:57:00",
}
# (id, code, state, stripe_secret_key, stripe_publishable_key, aps_sha_request, paypal_email_account, allow_tokenization)
PAYMENT_PROVIDERS = (
    (1, "stripe", "enabled", "sk_live_production", "pk_live_production", None, None, True),
    (2, "aps", "test", None, None, "production-sha-phrase", None, False),
    (3, "paypal", "disabled", None, None, None, "payments@example.test", False),
    (4, "custom", "enabled", None, None, None, None, False),
)
PRODUCTION_API_KEY_HASH = "$pbkdf2-sha512$600000$production-api-key-hash"


class _RestoredProductionCopy:
    """A SQLite stand-in for the tables sanitize touches, seeded like a restored production database."""

    def __init__(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.executescript(
            """
            CREATE TABLE ir_config_parameter (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE mail_push_device (id INTEGER PRIMARY KEY, endpoint TEXT);
            CREATE TABLE mail_push (id INTEGER PRIMARY KEY, mail_push_device_id INTEGER, payload TEXT);
            CREATE TABLE shopify_sync (id INTEGER PRIMARY KEY, mode TEXT, state TEXT);
            CREATE TABLE product_product (
                id INTEGER PRIMARY KEY, shopify_next_export BOOLEAN, shopify_next_export_quantity_change_amount INTEGER,
                shopify_last_exported_at TEXT, shopify_created_at TEXT
            );
            CREATE TABLE external_system (id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE external_id (
                id INTEGER PRIMARY KEY, system_id INTEGER, res_model TEXT, res_id INTEGER, resource TEXT, external_id TEXT
            );
            CREATE TABLE ir_model (id INTEGER PRIMARY KEY, model TEXT);
            CREATE TABLE ir_act_server (id INTEGER PRIMARY KEY, model_id INTEGER, code TEXT);
            CREATE TABLE ir_cron (id INTEGER PRIMARY KEY, cron_name TEXT, active BOOLEAN, ir_actions_server_id INTEGER);
            CREATE TABLE ir_model_data (id INTEGER PRIMARY KEY, module TEXT, name TEXT, model TEXT, res_id INTEGER);
            CREATE TABLE ir_mail_server (
                name TEXT, smtp_port INTEGER, smtp_host TEXT, smtp_encryption TEXT, active BOOLEAN,
                smtp_authentication TEXT, smtp_user TEXT, smtp_pass TEXT, google_gmail_refresh_token TEXT
            );
            CREATE TABLE payment_provider (
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, state TEXT NOT NULL, stripe_secret_key TEXT,
                stripe_publishable_key TEXT, aps_sha_request TEXT, paypal_email_account TEXT, allow_tokenization BOOLEAN
            );
            CREATE TABLE fetchmail_server (
                id INTEGER PRIMARY KEY, name TEXT, server TEXT, active BOOLEAN, password TEXT, microsoft_outlook_refresh_token TEXT
            );
            CREATE TABLE iap_account (id INTEGER PRIMARY KEY, service_id INTEGER, account_token TEXT);
            CREATE TABLE res_users_apikeys (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, key TEXT);
            ATTACH DATABASE ':memory:' AS information_schema;
            CREATE TABLE information_schema.columns (table_name TEXT, column_name TEXT, is_nullable TEXT, data_type TEXT);
            """
        )
        self.tables = {row[0] for row in self.database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.database.executemany(
            "INSERT INTO information_schema.columns VALUES ('payment_provider', ?, ?, ?)",
            [
                ("id", "NO", "integer"),
                ("code", "NO", "character varying"),
                ("state", "NO", "character varying"),
                ("stripe_secret_key", "YES", "character varying"),
                ("stripe_publishable_key", "YES", "character varying"),
                ("aps_sha_request", "YES", "character varying"),
                ("paypal_email_account", "YES", "character varying"),
                ("allow_tokenization", "YES", "boolean"),
            ],
        )
        self.database.executemany("INSERT INTO payment_provider VALUES (?, ?, ?, ?, ?, ?, ?, ?)", PAYMENT_PROVIDERS)
        self.database.execute(
            "INSERT INTO fetchmail_server VALUES "
            "(1, 'Support inbox', 'imap.example.test', true, 'production-imap-password', 'production-outlook-refresh-token')"
        )
        self.database.execute("INSERT INTO iap_account VALUES (1, 1, 'production-iap-token')")
        self.database.executemany(
            "INSERT INTO res_users_apikeys VALUES (?, ?, ?, ?)",
            [(1, 7, "Data access", PRODUCTION_API_KEY_HASH), (2, 8, "Integration", "$pbkdf2-sha512$600000$second-hash")],
        )
        self.database.create_function("to_regclass", 1, lambda name: name if name in self.tables else None)
        parameters = {**PRODUCTION_PARAMETERS, **UNRELATED_PARAMETERS}
        self.database.executemany("INSERT INTO ir_config_parameter VALUES (?, ?)", parameters.items())
        self.database.executemany(
            "INSERT INTO mail_push_device VALUES (?, ?)", [(1, "https://push.example.test/a"), (2, "https://push.example.test/b")]
        )
        self.database.execute("INSERT INTO mail_push VALUES (1, 1, 'queued notification')")
        self.database.executemany(
            "INSERT INTO shopify_sync VALUES (?, ?, ?)",
            [
                (1, "export_changed_products", "running"),
                (2, "import_then_export_products", "queued"),
                (3, "export_changed_products", "draft"),
                (4, "import_products", "success"),
            ],
        )
        self.database.executemany(
            "INSERT INTO product_product VALUES (?, ?, ?, ?, ?)",
            [(1, True, 3, "2026-09-28 15:40:00", "2024-01-02 03:04:05"), (2, False, 0, None, None)],
        )
        self.database.executemany("INSERT INTO external_system VALUES (?, ?)", [(1, "shopify"), (2, "ebay")])
        self.database.executemany(
            "INSERT INTO external_id VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, "product.product", 1, "product", "8000000000001"),
                (2, 1, "product.product", 1, "variant", "4000000000001"),
                (3, 1, "res.partner", 7, "customer", "6000000000001"),
                (4, 2, "product.type", 3, "category", "ebay-category-9"),
            ],
        )
        self.database.executemany("INSERT INTO ir_model VALUES (?, ?)", [(1, "shopify.sync"), (2, "mail.mail")])
        self.database.executemany(
            "INSERT INTO ir_act_server VALUES (?, ?, ?)",
            [(1, 1, "model._cron_dispatch_next()"), (2, 2, "model.process_email_queue()"), (3, 2, "env['shopify.sync'].run()")],
        )
        self.database.executemany(
            "INSERT INTO ir_cron VALUES (?, ?, ?, ?)",
            [(1, "Shopify Sync - Dispatcher", True, 1), (2, "Mail: send queue", True, 2), (3, "Shopify reconcile", True, 3)],
        )
        self.database.execute("INSERT INTO ir_model_data VALUES (1, 'shopify_sync', 'ir_cron_shopify_sync_dispatch', 'ir.cron', 1)")
        self.database.execute(
            "INSERT INTO ir_mail_server VALUES ('Production', 587, 'smtp.example.test', 'starttls', true, 'login', "
            "'mailbox@example.test', 'copied-secret', 'production-gmail-refresh-token')"
        )

    def cursor(self) -> closing:
        return closing(_ParamstyleCursor(self.database.cursor()))

    def commit(self) -> None:
        self.database.commit()

    def close(self) -> None:
        self.database.close()

    def parameters(self) -> dict[str, str]:
        return dict(self.database.execute("SELECT key, value FROM ir_config_parameter"))

    def scalar(self, query: str) -> object:
        return self.database.execute(query).fetchone()[0]

    def active_mail_servers(self) -> list[tuple]:
        return self.database.execute("SELECT smtp_host, smtp_user, smtp_pass FROM ir_mail_server WHERE active = true").fetchall()

    def payment_providers(self) -> list[tuple]:
        return self.database.execute("SELECT * FROM payment_provider ORDER BY id").fetchall()


class _ParamstyleCursor:
    """Runs the workflow's psycopg2-style (%s) SQL against SQLite."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def execute(self, query: str, parameters: Sequence[object] = ()) -> None:
        self._cursor.execute(str(query).replace("%s", "?"), tuple(parameters))

    def fetchone(self) -> tuple | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[tuple]:
        return self._cursor.fetchall()

    @property
    def description(self) -> object:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def close(self) -> None:
        self._cursor.close()


class ProductionCredentialSanitizeTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.copy = _RestoredProductionCopy()
        self.addCleanup(self.copy.close)

    def _runner(self, platform_instance: str | None = "testing", **extra_environment: object) -> object:
        environment: dict[str, object] = {
            "ODOO_DB_HOST": "database",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "database-password",
            "ODOO_DB_NAME": "opw",
            "ODOO_FILESTORE_PATH": "/volumes/data/filestore/opw",
            "ENV_OVERRIDE_DISABLE_CRON": False,
        }
        if platform_instance is not None:
            environment["PLATFORM_INSTANCE"] = platform_instance
        environment.update(extra_environment)
        with patch.dict(os.environ, {}, clear=True):
            settings = odoo_data_workflows.LocalServerSettings(**environment)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        runner.local.db_conn = self.copy
        patcher = patch.object(runner, "connect_to_db", return_value=self.copy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner

    def _assert_production_credentials_cleared(self) -> None:
        parameters = self.copy.parameters()
        for key, production_value in PRODUCTION_PARAMETERS.items():
            with self.subTest(key=key):
                self.assertNotEqual(parameters.get(key), production_value)
        self.assertEqual({key: parameters[key] for key in UNRELATED_PARAMETERS}, UNRELATED_PARAMETERS)

    def test_restored_copy_on_a_non_production_instance_loses_every_production_credential(self) -> None:
        runner = self._runner("testing")

        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        runner.verify_production_credentials_cleared()

        self._assert_production_credentials_cleared()
        parameters = self.copy.parameters()
        cleared = set(PRODUCTION_PARAMETERS) - {"database.secret"}
        self.assertEqual(cleared & set(parameters), set())
        self.assertTrue(parameters["database.secret"])
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM mail_push_device"), 0)
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM mail_push"), 0)
        self.assertEqual(
            dict(self.copy.database.execute("SELECT id, state FROM shopify_sync")),
            {1: "canceled", 2: "canceled", 3: "canceled", 4: "success"},
        )
        self.assertEqual(
            self.copy.scalar(
                "SELECT count(*) FROM product_product WHERE shopify_next_export OR shopify_next_export_quantity_change_amount <> 0"
                " OR shopify_last_exported_at IS NOT NULL OR shopify_created_at IS NOT NULL"
            ),
            0,
        )
        self.assertEqual(
            dict(self.copy.database.execute("SELECT cron_name, active FROM ir_cron")),
            {"Shopify Sync - Dispatcher": 0, "Mail: send queue": 1, "Shopify reconcile": 0},
        )
        # Every production-store Shopify ID is gone; other systems' external IDs stay.
        self.assertEqual(self.copy.database.execute("SELECT id, external_id FROM external_id").fetchall(), [(4, "ebay-category-9")])
        self._assert_table_credentials_cleared()

    def _assert_table_credentials_cleared(self) -> None:
        # Remote providers are disabled and lose their credentials; the provider identity fields stay.
        self.assertEqual(
            self.copy.payment_providers(),
            [
                (1, "stripe", "disabled", None, None, None, None, True),
                (2, "aps", "disabled", None, None, None, None, False),
                (3, "paypal", "disabled", None, None, None, "payments@example.test", False),
                (4, "custom", "enabled", None, None, None, None, False),
            ],
        )
        self.assertEqual(
            self.copy.database.execute("SELECT active, password, microsoft_outlook_refresh_token FROM fetchmail_server").fetchall(),
            [(0, None, None)],
        )
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM ir_mail_server WHERE google_gmail_refresh_token IS NOT NULL"), 0)
        iap_token = self.copy.scalar("SELECT account_token FROM iap_account")
        self.assertTrue(iap_token)
        self.assertNotEqual(iap_token, "production-iap-token")
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM res_users_apikeys"), 0)

    def test_each_restore_regenerates_a_different_database_secret(self) -> None:
        runner = self._runner("testing")

        runner.neutralize_production_credentials()
        first_secret = self.copy.parameters()["database.secret"]
        runner.neutralize_production_credentials()

        self.assertNotEqual(self.copy.parameters()["database.secret"], first_secret)

    def test_empty_or_missing_instance_is_treated_as_non_production(self) -> None:
        for platform_instance in ("", "  ", None, "staging-unknown"):
            with self.subTest(platform_instance=platform_instance):
                self.copy.close()
                self.copy = _RestoredProductionCopy()
                runner = self._runner(platform_instance)

                runner.fingerprint_restored_credentials()
                runner.neutralize_production_credentials()
                runner.verify_production_credentials_cleared()

                self._assert_production_credentials_cleared()

    def test_production_instance_keeps_every_value(self) -> None:
        for platform_instance in ("prod", "PROD", " production "):
            with self.subTest(platform_instance=platform_instance):
                runner = self._runner(platform_instance)
                before = self.copy.parameters()

                runner.fingerprint_restored_credentials()
                runner.neutralize_production_credentials()
                runner.verify_production_credentials_cleared()

                self.assertEqual(self.copy.parameters(), before)
                self.assertEqual(self.copy.scalar("SELECT count(*) FROM mail_push_device"), 2)
                self.assertEqual(self.copy.scalar("SELECT count(*) FROM ir_cron WHERE active"), 3)
                self.assertEqual(self.copy.scalar("SELECT count(*) FROM external_id"), 4)
                self.assertEqual(self.copy.scalar("SELECT count(*) FROM product_product WHERE shopify_last_exported_at"), 1)
                self.assertEqual(self.copy.payment_providers(), list(PAYMENT_PROVIDERS))
                self.assertEqual(self.copy.scalar("SELECT count(*) FROM res_users_apikeys"), 2)
                self.assertEqual(self.copy.scalar("SELECT password FROM fetchmail_server WHERE active"), "production-imap-password")

    def test_read_back_fails_when_a_restored_credential_survives(self) -> None:
        runner = self._runner("testing")
        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        self.copy.database.execute(
            "INSERT INTO ir_config_parameter VALUES ('printnode.api_key', ?)", (PRODUCTION_PARAMETERS["printnode.api_key"],)
        )
        self.copy.database.execute("INSERT INTO mail_push_device VALUES (9, 'https://push.example.test/copied')")

        with self.assertRaisesRegex(
            odoo_data_workflows.OdooDatabaseUpdateError, r"printnode\.api_key.*1 copied web push subscription"
        ) as raised:
            runner.verify_production_credentials_cleared()

        self.assertNotIn(PRODUCTION_PARAMETERS["printnode.api_key"], str(raised.exception))

    def test_restore_keeps_only_the_integrations_the_request_allows(self) -> None:
        runner = self._runner("testing", ODOO_RESTORE_KEPT_INTEGRATIONS="fishbowl, payment incoming_mail")

        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        runner.verify_production_credentials_cleared()

        parameters = self.copy.parameters()
        fishbowl = {key: value for key, value in IMPORT_SOURCE_PARAMETERS.items() if key.startswith("fishbowl.")}
        self.assertEqual({key: parameters.get(key) for key in fishbowl}, fishbowl)
        for key in set(IMPORT_SOURCE_PARAMETERS) - set(fishbowl):
            with self.subTest(key=key):
                self.assertNotIn(key, parameters)
        self.assertNotIn("printnode.api_key", parameters)
        for key in ("google_gmail_client_secret", "microsoft_outlook_client_secret"):
            with self.subTest(key=key):
                self.assertEqual(parameters[key], PRODUCTION_PARAMETERS[key])
        self.assertEqual(self.copy.payment_providers(), list(PAYMENT_PROVIDERS))
        self.assertEqual(
            self.copy.database.execute("SELECT active, password, microsoft_outlook_refresh_token FROM fetchmail_server").fetchall(),
            [(1, "production-imap-password", "production-outlook-refresh-token")],
        )
        # User API keys and signing keys are never kept, whatever the request says.
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM res_users_apikeys"), 0)
        self.assertNotEqual(parameters["database.secret"], PRODUCTION_PARAMETERS["database.secret"])

    def test_restore_request_cannot_keep_web_push_or_unknown_integrations(self) -> None:
        runner = self._runner("testing", ODOO_RESTORE_KEPT_INTEGRATIONS="web_push,res_users_apikeys,not_an_integration")

        with self.assertLogs(odoo_data_workflows._logger, "WARNING") as logs:
            runner.fingerprint_restored_credentials()
            runner.neutralize_production_credentials()
            runner.verify_production_credentials_cleared()

        self.assertIn("not_an_integration, res_users_apikeys, web_push", "\n".join(logs.output))
        self._assert_production_credentials_cleared()
        self._assert_table_credentials_cleared()
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM mail_push_device"), 0)

    def test_read_back_fails_when_a_restored_import_source_payment_mail_or_api_key_survives(self) -> None:
        runner = self._runner("testing")
        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        self.copy.database.execute(
            "INSERT INTO ir_config_parameter VALUES ('cm_data.db.password', ?)", (PRODUCTION_PARAMETERS["cm_data.db.password"],)
        )
        self.copy.database.execute(
            "UPDATE payment_provider SET state = 'enabled', stripe_secret_key = 'sk_live_production' WHERE id = 1"
        )
        self.copy.database.execute(
            "UPDATE fetchmail_server SET active = true, microsoft_outlook_refresh_token = 'production-outlook-refresh-token'"
        )
        self.copy.database.execute("UPDATE ir_mail_server SET google_gmail_refresh_token = 'production-gmail-refresh-token'")
        self.copy.database.execute("INSERT INTO res_users_apikeys VALUES (9, 7, 'Data access', ?)", (PRODUCTION_API_KEY_HASH,))

        with self.assertRaises(odoo_data_workflows.OdooDatabaseUpdateError) as raised:
            runner.verify_production_credentials_cleared()

        message = str(raised.exception)
        for expected in (
            "unchanged restored value: cm_data.db.password",
            "unchanged restored value: payment_provider.stripe_secret_key",
            "unchanged restored value: res_users_apikeys.key",
            "unchanged restored value: fetchmail_server.microsoft_outlook_refresh_token",
            "unchanged restored value: ir_mail_server.google_gmail_refresh_token",
            "1 payment provider(s) still enabled",
            "1 incoming mail server(s) still active",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, message)
        for secret in (
            PRODUCTION_PARAMETERS["cm_data.db.password"],
            "sk_live_production",
            PRODUCTION_API_KEY_HASH,
            "production-outlook-refresh-token",
            "production-gmail-refresh-token",
        ):
            self.assertNotIn(secret, message)

    def test_read_back_rejects_each_restored_mail_oauth_client_secret(self) -> None:
        runner = self._runner("testing")
        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()

        for key in ("google_gmail_client_secret", "microsoft_outlook_client_secret"):
            with self.subTest(key=key):
                self.copy.database.execute("INSERT INTO ir_config_parameter VALUES (?, ?)", (key, PRODUCTION_PARAMETERS[key]))

                with self.assertRaises(odoo_data_workflows.OdooDatabaseUpdateError) as raised:
                    runner.verify_production_credentials_cleared()

                self.assertIn(key, str(raised.exception))
                self.assertNotIn(PRODUCTION_PARAMETERS[key], str(raised.exception))
                self.copy.database.execute("DELETE FROM ir_config_parameter WHERE key = ?", (key,))

    def test_read_back_accepts_new_values_set_after_the_clearing(self) -> None:
        runner = self._runner("testing")
        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        self.copy.database.execute("INSERT INTO ir_config_parameter VALUES ('fishbowl.password', 'lane-read-only-password')")
        self.copy.database.execute("INSERT INTO res_users_apikeys VALUES (9, 7, 'Lane key', '$pbkdf2-sha512$600000$lane-hash')")
        self.copy.database.execute("UPDATE payment_provider SET stripe_secret_key = 'sk_test_lane' WHERE id = 1")

        runner.verify_production_credentials_cleared()

    def test_read_back_fails_when_a_production_shopify_external_id_survives(self) -> None:
        runner = self._runner("testing")
        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        self.copy.database.execute("INSERT INTO external_id VALUES (9, 1, 'product.product', 2, 'product', '8000000000002')")

        with self.assertRaisesRegex(
            odoo_data_workflows.OdooDatabaseUpdateError, r"1 copied production Shopify external ID"
        ) as raised:
            runner.verify_production_credentials_cleared()

        self.assertNotIn("8000000000002", str(raised.exception))

    def test_credential_clearing_tolerates_a_database_without_optional_addons(self) -> None:
        for table in (
            "mail_push",
            "mail_push_device",
            "shopify_sync",
            "product_product",
            "ir_cron",
            "external_id",
            "external_system",
            "payment_provider",
            "fetchmail_server",
            "iap_account",
            "res_users_apikeys",
        ):
            self.copy.database.execute(f"DROP TABLE {table}")
            self.copy.tables.discard(table)
        runner = self._runner("testing")

        runner.fingerprint_restored_credentials()
        runner.neutralize_production_credentials()
        runner.verify_production_credentials_cleared()

        self._assert_production_credentials_cleared()

    def test_restore_clears_credentials_but_keeps_what_launchplane_applies_afterwards(self) -> None:
        runner = self._runner("testing")
        development_store = {
            "shopify.shop_url_key": "development-store",
            "shopify.api_token": "development-token",
            "shopify.webhook_key": "development-webhook-key",
            "shopify.test_store": "True",
            "printnode.api_key": "testing-printnode-key",
        }

        def reenable_dispatcher(**_kwargs: object) -> None:
            # An install hook restoring a pre-migration snapshot turns the dispatcher back on.
            self.copy.database.execute("UPDATE ir_cron SET active = TRUE WHERE id = 1")

        def apply_launchplane_settings() -> None:
            self.copy.database.executemany(
                "INSERT INTO ir_config_parameter VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                development_store.items(),
            )

        filestore_process = MagicMock()
        filestore_process.wait.return_value = 0
        with patch.multiple(
            runner,
            _resolve_filestore_owner=MagicMock(return_value=None),
            overwrite_filestore=MagicMock(return_value=filestore_process),
            overwrite_database=MagicMock(),
            normalize_filestore_permissions=MagicMock(),
            sanitize_database=MagicMock(),
            install_addons=MagicMock(side_effect=reenable_dispatcher),
            update_addons=MagicMock(),
            reconcile_missing_manifest_install_queue=MagicMock(),
            assert_install_queue_is_resolvable=MagicMock(),
            apply_environment_overrides=MagicMock(side_effect=apply_launchplane_settings),
            assert_core_schema_healthy=MagicMock(),
            ensure_gpt_users=MagicMock(),
            drop_database=MagicMock(),
        ):
            runner._restore_from_verified_dump(Path("/unused/database.dump"), do_sanitize=True)
            runner.drop_database.assert_not_called()

        parameters = self.copy.parameters()
        self.assertEqual({key: parameters[key] for key in development_store}, development_store)
        for key in ("web_map.token_map_box", "mail.web_push_vapid_public_key", "shopify.last_product_import_time"):
            self.assertNotIn(key, parameters)
        self.assertNotEqual(parameters["database.secret"], PRODUCTION_PARAMETERS["database.secret"])
        self.assertEqual(self.copy.scalar("SELECT active FROM ir_cron WHERE id = 1"), 0)
        self.assertEqual(self.copy.active_mail_servers(), [("invalid", None, None)])
        self.assertEqual(self.copy.scalar("SELECT count(*) FROM ir_mail_server WHERE smtp_pass IS NOT NULL"), 0)
        self._assert_table_credentials_cleared()

    def test_restore_that_fails_after_pg_restore_drops_the_production_copy(self) -> None:
        runner = self._runner("testing")
        filestore_process = MagicMock()
        filestore_process.wait.return_value = 0
        dropped: list[bool] = []
        with patch.multiple(
            runner,
            _resolve_filestore_owner=MagicMock(return_value=None),
            overwrite_filestore=MagicMock(return_value=filestore_process),
            overwrite_database=MagicMock(),
            normalize_filestore_permissions=MagicMock(),
            sanitize_database=MagicMock(),
            install_addons=MagicMock(side_effect=odoo_data_workflows.OdooRestorerError("install failed")),
            apply_environment_overrides=MagicMock(),
            drop_database=MagicMock(side_effect=lambda: dropped.append(True)),
        ):
            with self.assertRaisesRegex(odoo_data_workflows.OdooRestorerError, "install failed"):
                runner._restore_from_verified_dump(Path("/unused/database.dump"), do_sanitize=True)
            runner.apply_environment_overrides.assert_not_called()

        self.assertEqual(dropped, [True])


class UpdateAddonsModuleDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.core_addons = self.root / "odoo" / "addons"
        self.tenant_addons = self.root / "tenant" / "addons"
        self.shared_addons = self.root / "shared-addons"
        self._write_module(self.core_addons, "sale")
        self._write_module(self.tenant_addons, "tenant_core", depends=("base", "sale", "tenant_helper"))
        self._write_module(self.tenant_addons, "tenant_helper", depends=("tenant_deep",))
        self._write_module(self.tenant_addons, "tenant_deep")
        self._write_module(self.tenant_addons, "tenant_unused")
        self._write_module(self.tenant_addons, "tenant_legacy", manifest_name="__openerp__.py")
        (self.tenant_addons / "notes").mkdir()
        self._write_module(self.shared_addons, "shared_tools")

    @staticmethod
    def _write_module(
        addons_root: Path, module_name: str, *, depends: tuple[str, ...] = (), manifest_name: str = "__manifest__.py"
    ) -> None:
        module_path = addons_root / module_name
        module_path.mkdir(parents=True)
        manifest = {"name": module_name, "depends": list(depends)}
        (module_path / manifest_name).write_text(repr(manifest), encoding="utf-8")

    def _update(
        self, *, installed_modules: set[str], update_modules: str | None = None, explicit_modules: list[str] | None = None
    ) -> MagicMock:
        settings_values: dict[str, object] = {
            "ODOO_DB_HOST": "database",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "database-password",
            "ODOO_DB_NAME": "cm",
            "ODOO_FILESTORE_PATH": "/volumes/data/filestore/cm",
            "ODOO_ADDONS_PATH": f"{self.core_addons},{self.tenant_addons}",
            "LOCAL_ADDONS_DIRS": str(self.shared_addons),
        }
        if update_modules is not None:
            settings_values["ODOO_UPDATE_MODULES"] = update_modules
        settings = odoo_data_workflows.LocalServerSettings(**settings_values)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        with (
            patch.object(runner, "_installed_modules", return_value=installed_modules),
            patch.object(runner, "_apply_module_updates") as apply_module_updates,
        ):
            runner.update_addons(explicit_modules=explicit_modules)
        return apply_module_updates

    def test_auto_updates_installed_local_modules_and_their_uninstalled_local_dependencies(self) -> None:
        installed_modules = {"base", "sale", "tenant_core", "tenant_legacy", "shared_tools"}
        for update_modules in (None, "AUTO", "auto"):
            with self.subTest(update_modules=update_modules):
                apply_module_updates = self._update(installed_modules=installed_modules, update_modules=update_modules)

                apply_module_updates.assert_called_once()
                desired_modules = apply_module_updates.call_args.args[0]
                self.assertEqual(
                    sorted(desired_modules),
                    ["shared_tools", "tenant_core", "tenant_deep", "tenant_helper", "tenant_legacy"],
                )
                local_module_paths = apply_module_updates.call_args.kwargs["local_module_paths"]
                self.assertEqual(local_module_paths["tenant_core"], self.tenant_addons / "tenant_core")
                self.assertNotIn("sale", local_module_paths)

    def test_auto_skips_the_update_when_no_local_module_is_installed(self) -> None:
        apply_module_updates = self._update(installed_modules={"base", "sale"})

        apply_module_updates.assert_not_called()

    def test_configured_module_list_is_used_without_auto_detection(self) -> None:
        apply_module_updates = self._update(installed_modules=set(), update_modules=" tenant_unused , sale ,")

        apply_module_updates.assert_called_once()
        self.assertEqual(list(apply_module_updates.call_args.args[0]), ["tenant_unused", "sale"])
        self.assertIsNone(apply_module_updates.call_args.kwargs["local_module_paths"])

    def test_configured_module_list_also_upgrades_its_installed_local_dependencies(self) -> None:
        # tenant_core depends on sale (core), tenant_helper and, through it, tenant_deep.
        apply_module_updates = self._update(
            installed_modules={"base", "sale", "tenant_core", "tenant_helper"}, update_modules="tenant_core"
        )

        apply_module_updates.assert_called_once()
        self.assertEqual(list(apply_module_updates.call_args.args[0]), ["tenant_core", "tenant_helper"])

    def test_explicit_modules_override_configured_modules(self) -> None:
        apply_module_updates = self._update(
            installed_modules=set(), update_modules="tenant_unused", explicit_modules=["website", " "]
        )

        apply_module_updates.assert_called_once()
        self.assertEqual(list(apply_module_updates.call_args.args[0]), ["website"])


class EnsureAdminUserTests(unittest.TestCase):
    def _runner(self, admin_password: str) -> Any:
        environment: dict[str, object] = {
            "ODOO_DB_HOST": "database",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "database-password",
            "ODOO_DB_NAME": "cm_website",
            "ODOO_FILESTORE_PATH": "/volumes/data/filestore/cm_website",
            "ODOO_ADMIN_PASSWORD": admin_password,
            "PLATFORM_INSTANCE": "testing",
        }
        with patch.dict(os.environ, {}, clear=True):
            loaded = load_environment_from_explicit_payload(
                raw_payload=json.dumps({"context": "probe", "instance": "local", "environment": environment}),
                context_name="probe",
                instance_name="local",
            )
            settings = odoo_data_workflows.LocalServerSettings(**loaded.merged_values)
        runner = odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
        cursor = MagicMock()
        cursor.fetchone.side_effect = lambda: (2, 3) if "res_users" in cursor.execute.call_args.args[0] else ("admin@localhost",)
        runner.local.db_conn = MagicMock()
        runner.local.db_conn.cursor.return_value.__enter__.return_value = cursor
        for name in ("connect_to_db", "_reset_db_connection"):
            patcher = patch.object(runner, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        return runner

    @staticmethod
    def _run_admin_hardening(runner: Any, environment: MagicMock) -> None:
        odoo_module = types.ModuleType("odoo")
        odoo_module.__dict__.update(api=MagicMock(Environment=MagicMock(return_value=environment)), SUPERUSER_ID=1)
        exceptions = types.ModuleType("odoo.exceptions")
        exceptions.__dict__["AccessDenied"] = PermissionError
        registry_module = types.ModuleType("odoo.modules.registry")
        registry_module.__dict__["Registry"] = MagicMock()

        def run_shell(script: str, label: str) -> None:
            if label == "admin hardening":
                exec(script, {})

        with (
            patch.dict(
                sys.modules,
                {"odoo": odoo_module, "odoo.exceptions": exceptions, "odoo.modules.registry": registry_module},
            ),
            patch.object(runner, "_run_odoo_shell", side_effect=run_shell),
        ):
            runner.ensure_admin_user()

    def test_post_deploy_admin_hardening_only_writes_when_configured_password_changes(self) -> None:
        environment = MagicMock()
        admin = environment["res.users"].sudo().search()
        admin.with_user.return_value = admin
        admin.with_context.return_value = admin
        admin.sudo.return_value = admin
        stored = {"password": "initial-password"}

        def check_credentials(credential: dict[str, str], _request_environment: dict[str, bool]) -> None:
            if credential["password"] != stored["password"]:
                raise PermissionError

        admin._check_credentials.side_effect = check_credentials
        admin.write.side_effect = stored.update
        configured_password = " \tconfigured-'\"\\-password\t "

        self._run_admin_hardening(self._runner(configured_password), environment)
        self._run_admin_hardening(self._runner(configured_password), environment)
        admin.write.assert_called_once_with({"password": configured_password})

        self._run_admin_hardening(self._runner("rotated-password"), environment)
        self._run_admin_hardening(self._runner("rotated-password"), environment)
        self.assertEqual(admin.write.call_count, 2)
        self.assertEqual(stored["password"], "rotated-password")

    def test_blank_admin_password_does_not_request_an_update(self) -> None:
        for password in ("", " ", "\t", " \t "):
            with self.subTest(password=password):
                runner = self._runner(password)
                environment = MagicMock()
                self._run_admin_hardening(runner, environment)
                environment["res.users"].sudo().search().write.assert_not_called()
                self.assertIsNone(runner.local.admin_password)

    def test_post_deploy_admin_hardening_does_not_write_after_unexpected_credential_check_failure(self) -> None:
        environment = MagicMock()
        admin = environment["res.users"].sudo().search()
        admin.with_user.return_value = admin
        admin._check_credentials.side_effect = RuntimeError("credential backend unavailable")

        with self.assertRaisesRegex(RuntimeError, "credential backend unavailable"):
            self._run_admin_hardening(self._runner("configured-password"), environment)

        admin.with_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
