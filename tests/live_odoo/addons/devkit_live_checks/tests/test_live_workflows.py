"""Real Odoo/Postgres checks, each paired with a fault in the guarded product path."""

from __future__ import annotations

import base64
import configparser
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg2
import run_odoo_data_workflows as workflows
import run_odoo_startup as startup
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from passlib.context import CryptContext
from psycopg2 import sql

PASSWORD = "synthetic-'quoted\\password-with-space "
ADDONS = (
    "/opt/support-addons,/opt/extra_addons,/opt/extra_addons/ci_enterprise,/odoo/addons,/odoo/odoo/addons,"
    + startup.LAUNCHPLANE_ADDONS_PATH
)
CRYPT = CryptContext(["pbkdf2_sha512"])
REAL_RUN = subprocess.run


class ServerStarted(Exception):
    """Observe startup's final exec boundary without running an HTTP server."""


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return REAL_RUN(args, check=True, capture_output=True, timeout=180, **kwargs)


def query(database: str, statement: str, params: tuple = ()) -> list[tuple]:
    with psycopg2.connect(host="database", user="odoo", dbname=database) as connection, connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def database_command(statement: sql.Composed) -> None:
    connection = psycopg2.connect(host="database", user="odoo", dbname="postgres")
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(statement)
    finally:
        connection.close()


@tagged("post_install", "-at_install")
class TestLiveWorkflows(TransactionCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.scratch = tempfile.TemporaryDirectory(prefix="devkit-live-")
        cls.root = Path(cls.scratch.name)
        cls.source = "devkit_ci_source_" + uuid.uuid4().hex[:10]
        cls.environment = {
            "PGHOST": "database",
            "PGUSER": "odoo",
            "PGPASSWORD": "",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "",
            "ODOO_ADDONS_PATH": ADDONS,
            "ODOO_DATA_DIR": str(cls.root / "data"),
            "PLATFORM_INSTANCE": "testing",
            "ODOO_MASTER_PASSWORD": "synthetic-master",
            "ODOO_ADMIN_PASSWORD": PASSWORD,
            "ODOO_ADMIN_LOGIN": "admin",
            "ODOO_INSTALL_MODULES": "",
            "ODOO_UPDATE_MODULES": "base",
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": "",
            "ODOO_KEY": "",
            "OPENUPGRADE_ENABLED": "False",
            "ODOO_FROM_FILTER": "",
        }
        config = configparser.ConfigParser(interpolation=None)
        config["options"] = {
            "db_host": "database",
            "db_user": "odoo",
            "db_password": "",
            "addons_path": ADDONS,
            "data_dir": cls.environment["ODOO_DATA_DIR"],
            "max_cron_threads": "0",
            "workers": "0",
            "smtp_server": "127.0.0.1",
        }
        cls.generated_config = Path("/volumes/config/_generated.conf")
        with cls.generated_config.open("w") as stream:
            config.write(stream)
        run(
            [
                "/odoo/odoo-bin",
                "--config",
                str(cls.generated_config),
                "--database",
                cls.source,
                "--init",
                "base,launchplane_settings,ci_probe,ci_enterprise_probe",
                "--without-demo",
                "all",
                "--stop-after-init",
                "--no-http",
            ],
            env={**os.environ, **cls.environment},
        )
        query(cls.source, "INSERT INTO ir_config_parameter (key,value) VALUES ('devkit.ci.origin','source')")
        cls.source_store = cls.root / "source-store"
        cls.source_store.mkdir()
        (cls.source_store / "attachment").write_bytes(b"synthetic attachment bytes\x00\xff")
        # Replace only SSH transport with local execution. pg_dump, validation,
        # rsync, pg_restore and the full workflow remain the product implementation.
        cls.transport = cls.root / "transport.py"
        cls.transport.write_text("""import os, sys
args = sys.argv[1:]
if args[0] == '-l':
    args = args[2:]
args = args[1:]  # discard the synthetic destination host
command = ' '.join(args).replace('sudo -u odoo ', '')
os.execv('/bin/bash', ['bash', '-c', command])
""")
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.addClassCleanup(cls.drop, cls.source)

    @staticmethod
    def drop(database: str) -> None:
        query("postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database,))
        database_command(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))

    @contextmanager
    def target(self, *, empty: bool = False) -> Iterator[workflows.OdooDataWorkflowRunner]:
        name = "devkit_ci_target_" + uuid.uuid4().hex[:10]
        if not empty:
            database_command(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(self.source)))
            query(name, "UPDATE ir_config_parameter SET value='target' WHERE key='devkit.ci.origin'")
        environment = {
            **self.environment,
            "ODOO_DB_NAME": name,
            "ODOO_FILESTORE_PATH": str(self.root / "target-stores"),
            "ODOO_DATA_WORKFLOW_LOCK_FILE": str(self.root / (name + ".lock")),
            "ODOO_UPSTREAM_HOST": "synthetic-source",
            "ODOO_UPSTREAM_USER": "odoo",
            "ODOO_UPSTREAM_DB_NAME": self.source,
            "ODOO_UPSTREAM_DB_USER": "odoo",
            "ODOO_UPSTREAM_FILESTORE_PATH": str(self.source_store),
        }
        with patch.dict(os.environ, environment):
            runner = workflows.OdooDataWorkflowRunner(workflows.LocalServerSettings(), workflows.UpstreamServerSettings(), None)
            store = runner._local_database_filestore_path()
            store.mkdir(parents=True, exist_ok=True)
            (store / "stale").write_text("must disappear")
            with patch.object(runner, "_build_ssh_command", return_value=[sys.executable, str(self.transport)]):
                try:
                    yield runner
                finally:
                    runner._reset_db_connection()
                    self.drop(name)

    def guarded(self, check: Callable, fault: Callable, *, empty: bool = False, fault_message: str = "") -> None:
        with self.target(empty=empty) as runner:
            check(runner)
        with self.target(empty=empty) as runner, fault(runner):
            with self.assertRaisesRegex(AssertionError, fault_message, msg="Planted fault did not break its live behavior check"):
                check(runner)
        print(f"DEVKIT_FAULT_DETECTED {self._testMethodName}", flush=True)

    @staticmethod
    def password_state(runner: workflows.OdooDataWorkflowRunner) -> tuple:
        return query(runner.local.db_name, "SELECT password,write_date FROM res_users WHERE login='admin'")[0]

    def assert_password(self, runner: workflows.OdooDataWorkflowRunner) -> None:
        self.assertTrue(CRYPT.verify(PASSWORD, self.password_state(runner)[0]), "Configured admin password was not applied")

    @staticmethod
    def startup_settings(runner: workflows.OdooDataWorkflowRunner, **values: Any) -> startup.StartupSettings:
        return startup.StartupSettings(
            config_path=str(runner.local.filestore_path / (runner.local.db_name + ".conf")),
            base_config_path=str(TestLiveWorkflows.generated_config),
            platform_instance="testing",
            database_name=runner.local.db_name,
            database_host="database",
            database_port=5432,
            database_user="odoo",
            database_password="",
            master_password="synthetic-master",
            admin_login=values.get("admin_login", "admin"),
            admin_password=PASSWORD,
            addons_path=ADDONS,
            data_dir=TestLiveWorkflows.environment["ODOO_DATA_DIR"],
            list_db="False",
            install_modules=("base",),
            data_workflow_lock_file=str(runner.local.data_workflow_lock_file),
            data_workflow_lock_timeout_seconds=10,
            ready_timeout_seconds=10,
            poll_interval_seconds=0.1,
        )

    def test_public_startup_rejects_default_admin_before_exec(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            # A missing configured login leaves the active admin/admin untouched.
            # This reaches the real policy after the real initialization/hardening phases.
            settings = self.startup_settings(runner, admin_login="absent-configured-admin")
            started = False

            def captured_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
                return REAL_RUN(args, **kwargs, capture_output=True)

            with (
                patch.object(startup, "_load_settings", return_value=settings),
                patch.object(sys, "argv", ["run_odoo_startup.py", "--config", settings.config_path]),
                patch.object(os, "execve", side_effect=ServerStarted),
                patch.object(subprocess, "run", side_effect=captured_run),
            ):
                try:
                    startup.main()
                except ServerStarted:
                    started = True
                except subprocess.CalledProcessError as error:
                    self.assertIn(b"active password for admin is 'admin'", error.stderr)
            self.assertFalse(started, "Unsafe admin/admin reached the server exec boundary")

        self.guarded(check, lambda runner: patch.object(startup, "_assert_active_admin_password_is_not_default"))

    def test_startup_password_write_is_idempotent(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            settings = self.startup_settings(runner)
            original = self.password_state(runner)
            startup._write_runtime_config(settings)
            startup._apply_admin_password_if_configured(settings)
            first = self.password_state(runner)
            self.assert_password(runner)
            self.assertNotEqual(original[0], first[0])
            startup._apply_admin_password_if_configured(settings)
            self.assertEqual(first, self.password_state(runner), "Restart performed another password write")

        original_shell = startup._run_odoo_shell

        def fault(_runner: workflows.OdooDataWorkflowRunner) -> Any:
            def forced_write(settings: startup.StartupSettings, script: str, *, label: str) -> None:
                # Force the real credential check to deny only the comparison.
                script = script.replace(
                    "{'type': 'password', 'password': payload['password']}",
                    "{'type': 'password', 'password': 'wrong-comparison'}",
                )
                original_shell(settings, script, label=label)

            return patch.object(startup, "_run_odoo_shell", side_effect=forced_write)

        self.guarded(check, fault, fault_message="Restart performed another password write")

    def settings_check(self, runner: workflows.OdooDataWorkflowRunner, *, data_workflow: bool) -> None:
        payload = {"config_parameters": [{"key": "devkit.ci.payload", "value": {"source": "literal", "value": "applied"}}]}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        with patch.dict(os.environ, {"ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": encoded}):
            runner.os_env["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"] = encoded
            if data_workflow:
                runner.run_restore()
            else:
                settings = self.startup_settings(runner)
                with (
                    patch.object(startup, "_load_settings", return_value=settings),
                    patch.object(sys, "argv", ["run_odoo_startup.py", "--config", settings.config_path]),
                    patch.object(os, "execve", side_effect=ServerStarted),
                    self.assertRaises(ServerStarted),
                ):
                    startup.main()
        self.assertEqual(
            query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='devkit.ci.payload'"), [("applied",)]
        )

    def test_startup_applies_real_launchplane_settings(self) -> None:
        self.guarded(
            lambda runner: self.settings_check(runner, data_workflow=False),
            lambda runner: patch.object(startup, "_apply_environment_overrides_if_available"),
        )

    def test_data_workflow_applies_real_launchplane_settings(self) -> None:
        self.guarded(
            lambda runner: self.settings_check(runner, data_workflow=True),
            lambda runner: patch.object(runner, "apply_environment_overrides"),
        )

    def restore_check(self, runner: workflows.OdooDataWorkflowRunner) -> None:
        runner.run_restore()
        self.assertEqual(
            query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='devkit.ci.origin'"), [("source",)]
        )
        store = runner._local_database_filestore_path()
        self.assertTrue((store / "attachment").is_file(), "Source attachment was not copied")
        self.assertEqual((store / "attachment").read_bytes(), (self.source_store / "attachment").read_bytes())
        self.assertFalse((store / "stale").exists(), "Stale target attachment survived replacement")
        self.assert_password(runner)

    def test_restore_replaces_database(self) -> None:
        self.guarded(self.restore_check, lambda runner: patch.object(runner, "overwrite_database"))

    def test_restore_replaces_filestore(self) -> None:
        self.guarded(
            self.restore_check,
            lambda runner: patch.object(runner, "overwrite_filestore", side_effect=lambda owner: subprocess.Popen(["true"])),
        )

    def test_restore_hardens_admin(self) -> None:
        self.guarded(self.restore_check, lambda runner: patch.object(runner, "ensure_admin_user"))

    def test_failed_restore_drops_partial_database(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            with patch.object(
                runner, "apply_environment_overrides", side_effect=workflows.OdooDatabaseUpdateError("planted apply failure")
            ):
                with self.assertRaises(workflows.OdooDatabaseUpdateError):
                    runner.run_restore()
            self.assertFalse(
                query("postgres", "SELECT 1 FROM pg_database WHERE datname=%s", (runner.local.db_name,)),
                "Failed restore database survived rollback",
            )

        self.guarded(check, lambda runner: patch.object(runner, "drop_database"))

    def test_bootstrap_creates_schema_and_hardens_admin(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            runner.run_bootstrap(do_sanitize=True)
            self.assertEqual(query(runner.local.db_name, "SELECT state FROM ir_module_module WHERE name='base'"), [("installed",)])
            self.assert_password(runner)
            self.assertFalse((runner._local_database_filestore_path() / "stale").exists())

        self.guarded(check, lambda runner: patch.object(runner, "ensure_admin_user"), empty=True)

    def test_auto_updates_community_and_excludes_enterprise_repository(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            query(
                runner.local.db_name,
                "UPDATE ir_config_parameter SET value='before-update' WHERE key IN ('devkit.ci.community','devkit.ci.enterprise')",
            )
            runner.local.update_modules = "AUTO"
            runner.update_addons()
            values = dict(
                query(
                    runner.local.db_name,
                    "SELECT key,value FROM ir_config_parameter WHERE key IN ('devkit.ci.community','devkit.ci.enterprise')",
                )
            )
            self.assertEqual(values["devkit.ci.community"], "loaded-from-xml", "AUTO did not upgrade the community addon")
            self.assertEqual(values["devkit.ci.enterprise"], "before-update", "AUTO upgraded an excluded Enterprise repository")

        self.guarded(check, lambda runner: patch.object(runner, "_resolve_excluded_addon_roots", return_value=()))
