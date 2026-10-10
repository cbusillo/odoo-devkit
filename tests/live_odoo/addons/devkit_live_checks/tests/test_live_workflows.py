"""Real Odoo/Postgres checks, each paired with a fault in the guarded product path."""

from __future__ import annotations

import base64
import configparser
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg2
import run_odoo_data_workflows as workflows
import run_odoo_startup as startup
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo_release_plan import digest
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
    try:
        return REAL_RUN(args, check=True, capture_output=True, timeout=180, **kwargs)
    except subprocess.CalledProcessError as error:
        # This lane has synthetic data and no credentials; preserve the nested
        # Odoo failure so a setup problem is distinguishable from a fault proof.
        print((error.stderr or b"").decode(errors="replace"), flush=True)
        raise


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
    def planned_payload(self, runner: workflows.OdooDataWorkflowRunner) -> dict[str, Any]:
        graph = runner.release_module_graph()
        image = "fixture/image@sha256:" + "a" * 64
        candidate = {
            "artifact_id": "isolated-candidate",
            "image": {"repository": "fixture/image", "digest": "sha256:" + "a" * 64},
            "odoo_install_modules": ["ci_boundary_install_probe"],
            "release_compatibility": {
                "complete": True,
                "modules": [{"name": name, "depends": sorted(deps)} for name, deps in graph.items()],
                "sources": [
                    {
                        "input_name": "addon:fixture/ci_probe",
                        "repository": "fixture/ci_probe",
                        "commit": "a" * 40,
                        "files": [
                            {
                                "path": "probe.xml",
                                "sha256": digest(Path("/opt/extra_addons/ci_probe/probe.xml").read_text()),
                                "kind": "database_data",
                                "module": "ci_probe",
                            }
                        ],
                    }
                ],
            },
        }
        declaration_file = self.root / (runner.local.db_name + "-image-declaration.json")
        declaration_file.write_text(json.dumps(candidate["release_compatibility"]))
        runner.os_env["ODOO_RELEASE_DECLARATION_FILE"] = str(declaration_file)
        return {
            "database": runner.local.db_name,
            "candidate_manifest": candidate,
            "release": {
                "candidate_artifact_id": candidate["artifact_id"],
                "candidate_image": image,
                "candidate_manifest_sha256": digest(candidate),
                "module_plan_complete": True,
                "classification": "database_changing",
                "install_modules": ["ci_boundary_install_probe"],
                "update_modules": ["ci_probe"],
                "changed_modules": ["ci_probe"],
                "changes": [{"module": "ci_probe", "kind": "database_data"}],
            },
        }

    def test_planned_maintenance_single_registry_and_full_run_timing(self) -> None:
        timings = {}
        for mode in ("baseline", "planned"):
            with self.target() as runner:
                runner.local.odoo_key = workflows.SecretStr("inert-'service\\key-for-isolated-fixture")
                query(runner.local.db_name, "UPDATE ir_config_parameter SET value='editor-owned' WHERE key='devkit.ci.editor'")
                query(
                    runner.local.db_name,
                    "UPDATE ir_config_parameter SET value='before-update' WHERE key IN ('devkit.ci.dependent','devkit.ci.enterprise')",
                )
                if mode == "planned":
                    payload = self.planned_payload(runner)
                    plan_file = self.root / (runner.local.db_name + "-plan.json")
                    plan_file.write_text(json.dumps(payload))
                    runner.os_env.update(
                        ODOO_RELEASE_MODULE_PLAN_FILE=str(plan_file), ODOO_RELEASE_IMAGE=payload["release"]["candidate_image"]
                    )
                logs = []

                def observed(*args: Any, _logs: list[str] = logs, **kwargs: Any) -> Any:
                    kwargs["capture_output"] = True
                    try:
                        result = REAL_RUN(*args, **kwargs)
                    except subprocess.CalledProcessError as error:
                        print((error.stderr or b"").decode(), flush=True)
                        raise
                    _logs.append((result.stdout or b"").decode() + (result.stderr or b"").decode())
                    return result

                started = time.monotonic()
                with patch.object(workflows.subprocess, "run", side_effect=observed):
                    self.credential_boundary_check(runner)
                timings[mode] = {
                    "seconds": time.monotonic() - started,
                    "registry_loads": sum(log.count("Registry loaded in") for log in logs),
                }
                if mode == "planned":
                    self.assertEqual(timings[mode]["registry_loads"], 1, "Planned maintenance loaded multiple registries")
                    self.assertEqual(runner.maintenance_receipt["update_modules"], ["ci_dependent_probe", "ci_probe"])
                    self.assertEqual(runner.maintenance_receipt["install_modules"], ["ci_boundary_install_probe"])
                    self.assert_password(runner)
                    parameters = dict(
                        query(runner.local.db_name, "SELECT key,value FROM ir_config_parameter WHERE key LIKE %s", ("devkit.ci.%",))
                    )
                    self.assertEqual(parameters["devkit.ci.editor"], "editor-owned")
                    self.assertEqual(parameters["devkit.ci.dependent"], "dependent-updated")
                    self.assertEqual(parameters["devkit.ci.enterprise"], "before-update")
                    self.assertEqual(
                        query(runner.local.db_name, "SELECT count(*) FROM res_users WHERE login IN ('gpt','gpt-admin')")[0][0],
                        len(workflows.OdooConfig.GPT_SERVICE_USERS),
                    )
                    self.assertEqual(
                        query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='web.base.url'")[0][0],
                        "https://fixture.example.test",
                    )
        self.assertGreater(timings["baseline"]["registry_loads"], timings["planned"]["registry_loads"])
        print("DEVKIT_MAINTENANCE_TIMING " + json.dumps(timings), flush=True)

    def test_planned_missing_change_and_failed_update_never_pass_readback(self) -> None:
        for fault in ("missing_change", "update_failure", "readback_failure", "late_boundary", "old_image", "skipped_update"):
            with self.target() as runner:
                query(
                    "postgres",
                    "CREATE TABLE IF NOT EXISTS devkit_boundary_events (id bigserial PRIMARY KEY, database_name text, event text)",
                )
                payload = self.planned_payload(runner)
                if fault == "old_image":
                    actual = json.loads(Path(runner.os_env["ODOO_RELEASE_DECLARATION_FILE"]).read_text())
                    actual["sources"][0]["commit"] = "b" * 40
                    Path(runner.os_env["ODOO_RELEASE_DECLARATION_FILE"]).write_text(json.dumps(actual))
                if fault == "missing_change":
                    payload["release"]["update_modules"] = []
                if fault == "update_failure":
                    runner.os_env["DEVKIT_FORCE_UPDATE_FAILURE"] = "1"
                if fault == "readback_failure":
                    payload["release"]["update_modules"] = ["ci_probe"]
                plan_file = self.root / (runner.local.db_name + "-plan.json")
                plan_file.write_text(json.dumps(payload))
                runner.os_env.update(
                    ODOO_RELEASE_MODULE_PLAN_FILE=str(plan_file), ODOO_RELEASE_IMAGE=payload["release"]["candidate_image"]
                )
                with ExitStack() as stack:
                    if fault == "late_boundary":
                        stack.enter_context(patch.object(runner, "prepare_credentials_before_registry"))
                        runner.os_env["DEVKIT_BOUNDARY_PROBE"] = "1"
                        runner.os_env["DEVKIT_LANE_KEY"] = "inert-lane-sentinel"
                    if fault == "readback_failure":

                        def failed_readback(*args: Any, **kwargs: Any) -> Any:
                            script = kwargs.get("input", b"")
                            if b"finish_planned_maintenance" in script:
                                kwargs["input"] = script.replace(
                                    b"runner.finish_planned_maintenance",
                                    b"runner._module_states_by_name = lambda: {}\nrunner.finish_planned_maintenance",
                                )
                            return REAL_RUN(*args, **kwargs)

                        stack.enter_context(patch.object(workflows.subprocess, "run", side_effect=failed_readback))
                    if fault == "skipped_update":

                        def skipped_update(*args: Any, **kwargs: Any) -> Any:
                            script = kwargs.get("input", b"")
                            if b"Registry.new" in script:
                                kwargs["input"] = script.replace(
                                    b"upgrade_modules=['ci_dependent_probe', 'ci_probe']", b"upgrade_modules=[]"
                                )
                            return REAL_RUN(*args, **kwargs)

                        stack.enter_context(patch.object(workflows.subprocess, "run", side_effect=skipped_update))
                    with self.assertRaises(workflows.OdooRestorerError):
                        runner.run_post_deploy_maintenance()
                self.assertFalse(hasattr(runner, "maintenance_receipt"))
                print("DEVKIT_FAULT_DETECTED planned_" + fault, flush=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.scratch = tempfile.TemporaryDirectory(prefix="devkit-live-")
        cls.root = Path(cls.scratch.name)
        cls.source = "devkit_ci_source_" + uuid.uuid4().hex[:10]
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.addClassCleanup(cls.drop, cls.source)
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
                "base,launchplane_settings,ci_probe,ci_enterprise_probe,ci_dependent_probe",
                "--without-demo",
                "all",
                "--stop-after-init",
                "--no-http",
            ],
            env={**os.environ, **cls.environment},
        )
        query(cls.source, "INSERT INTO ir_config_parameter (key,value) VALUES ('devkit.ci.origin','source')")
        cls.attachment_bytes = b"synthetic attachment bytes\x00\xff"
        encoded = base64.b64encode(cls.attachment_bytes).decode()
        run(
            ["/odoo/odoo-bin", "shell", "--config", str(cls.generated_config), "--database", cls.source, "--no-http"],
            input=(
                "attachment = env['ir.attachment'].create({"
                f"'name': 'devkit-ci-attachment', 'type': 'binary', 'datas': {encoded!r}"
                "})\nassert attachment.store_fname\nenv.cr.commit()\n"
            ).encode(),
            env={**os.environ, **cls.environment},
        )
        cls.attachment_name = query(cls.source, "SELECT store_fname FROM ir_attachment WHERE name='devkit-ci-attachment'")[0][0]
        cls.source_store = Path(cls.environment["ODOO_DATA_DIR"]) / "filestore" / cls.source
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
            "ODOO_FILESTORE_PATH": str(Path(self.environment["ODOO_DATA_DIR"]) / "filestore"),
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

    def guarded(self, check: Callable, fault: Callable, *, fault_message: str, empty: bool = False) -> None:
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
                patch.dict(os.environ, {"ODOO_ADMIN_LOGIN": settings.admin_login}),
                patch.object(sys, "argv", ["run_odoo_startup.py", "--config", settings.config_path]),
                patch.object(os, "execve", side_effect=ServerStarted),
                patch.object(subprocess, "run", side_effect=captured_run),
            ):
                try:
                    startup.main()
                except ServerStarted:
                    started = True
                except subprocess.CalledProcessError as error:
                    if b"active password for admin is 'admin'" not in (error.stderr or b""):
                        raise
            self.assertFalse(started, "Unsafe admin/admin reached the server exec boundary")

        self.guarded(
            check,
            lambda runner: patch.object(startup, "_assert_active_admin_password_is_not_default"),
            fault_message="Unsafe admin/admin reached the server exec boundary",
        )

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
                    patch.object(sys, "argv", ["run_odoo_startup.py", "--config", settings.config_path]),
                    patch.object(os, "execve", side_effect=ServerStarted),
                    self.assertRaises(ServerStarted),
                ):
                    startup.main()
        self.assertEqual(
            query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='devkit.ci.payload'"),
            [("applied",)],
            "Launchplane settings payload was not applied",
        )

    def test_startup_applies_real_launchplane_settings(self) -> None:
        self.guarded(
            lambda runner: self.settings_check(runner, data_workflow=False),
            lambda runner: patch.object(startup, "_apply_environment_overrides_if_available"),
            fault_message="Launchplane settings payload was not applied",
        )

    def test_data_workflow_applies_real_launchplane_settings(self) -> None:
        def omit_settings(runner: workflows.OdooDataWorkflowRunner) -> ExitStack:
            stack = ExitStack()
            # Parameter application now has an early SQL path and a later ORM
            # path. Disable both for the existing final-persistence fault; the
            # separate hook-order checks isolate failure of the early path.
            stack.enter_context(patch.object(runner, "_pre_registry_parameter_overrides", return_value={}))
            stack.enter_context(patch.object(runner, "apply_environment_overrides"))
            return stack

        self.guarded(
            lambda runner: self.settings_check(runner, data_workflow=True),
            omit_settings,
            fault_message="Launchplane settings payload was not applied",
        )

    def restore_check(self, runner: workflows.OdooDataWorkflowRunner) -> None:
        runner.run_restore()
        self.assertEqual(
            query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='devkit.ci.origin'"),
            [("source",)],
            "Source database did not replace target",
        )
        store = Path(runner.os_env["ODOO_DATA_DIR"]) / "filestore" / runner.local.db_name
        self.assertTrue((store / self.attachment_name).is_file(), "Source attachment was not copied")
        self.assertEqual((store / self.attachment_name).read_bytes(), self.attachment_bytes)
        self.assertFalse((store / "stale").exists(), "Stale target attachment survived replacement")
        run(
            runner._odoo_shell_command(),
            input=(
                "import base64\nattachment = env['ir.attachment'].search([('name', '=', 'devkit-ci-attachment')], limit=1)\n"
                f"assert attachment and base64.b64decode(attachment.datas) == {self.attachment_bytes!r}\n"
            ).encode(),
            env=runner.os_env,
        )
        self.assert_password(runner)

    def test_restore_replaces_database(self) -> None:
        self.guarded(
            self.restore_check,
            lambda runner: patch.object(runner, "overwrite_database"),
            fault_message="Source database did not replace target",
        )

    def test_restore_replaces_filestore(self) -> None:
        self.guarded(
            self.restore_check,
            lambda runner: patch.object(runner, "overwrite_filestore", side_effect=lambda owner: subprocess.Popen(["true"])),
            fault_message="Source attachment was not copied",
        )

    def test_restore_hardens_admin(self) -> None:
        self.guarded(
            self.restore_check,
            lambda runner: patch.object(runner, "ensure_admin_user"),
            fault_message="Configured admin password was not applied",
        )

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

        self.guarded(
            check,
            lambda runner: patch.object(runner, "drop_database"),
            fault_message="Failed restore database survived rollback",
        )

    def test_bootstrap_creates_schema_and_hardens_admin(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            runner.run_bootstrap(do_sanitize=True)
            self.assertEqual(query(runner.local.db_name, "SELECT state FROM ir_module_module WHERE name='base'"), [("installed",)])
            self.assert_password(runner)
            self.assertFalse((runner._local_database_filestore_path() / "stale").exists())

        self.guarded(
            check,
            lambda runner: patch.object(runner, "ensure_admin_user"),
            empty=True,
            fault_message="Configured admin password was not applied",
        )

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

        self.guarded(
            check,
            lambda runner: patch.object(runner, "_resolve_excluded_addon_roots", return_value=()),
            fault_message="AUTO upgraded an excluded Enterprise repository",
        )

    def credential_boundary_check(self, runner: workflows.OdooDataWorkflowRunner, *, restore: bool = False) -> None:
        before = (
            query(
                self.source,
                "SELECT key,value FROM ir_config_parameter WHERE key IN ('printnode.api_key','web_map.token_map_box')",
            )
            if restore
            else []
        )
        try:
            self._credential_boundary_check(runner, restore=restore)
        finally:
            if restore:
                query(self.source, "DELETE FROM ir_config_parameter WHERE key IN ('printnode.api_key','web_map.token_map_box')")
                for key, value in before:
                    query(self.source, "INSERT INTO ir_config_parameter (key,value) VALUES (%s,%s)", (key, value))

    def _credential_boundary_check(self, runner: workflows.OdooDataWorkflowRunner, *, restore: bool) -> None:
        query(
            "postgres",
            "CREATE TABLE IF NOT EXISTS devkit_boundary_events (id bigserial PRIMARY KEY, database_name text, event text)",
        )
        seed_database = self.source if restore else runner.local.db_name
        query(
            seed_database,
            "INSERT INTO ir_config_parameter (key,value) VALUES ('printnode.api_key','inert-production-sentinel'), "
            "('web_map.token_map_box','inert-map-sentinel') ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        )
        payload = {
            "config_parameters": [
                {"key": "printnode.api_key", "value": {"source": "secret_binding", "environment_variable": "DEVKIT_LANE_KEY"}},
                {"key": "web.base.url", "value": {"source": "literal", "value": "https://fixture.example.test"}},
            ]
        }
        runner.local.install_modules = "ci_boundary_install_probe"
        runner.local.update_modules = "ci_probe"
        runner.os_env.update(
            DEVKIT_BOUNDARY_PROBE="1",
            DEVKIT_LANE_KEY="inert-lane-sentinel",
            DEVKIT_EXPECT_STRIP="1" if restore else "",
            DEVKIT_EXPECT_SANITIZE="1" if restore else "",
            ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64=base64.b64encode(json.dumps(payload).encode()).decode(),
        )
        original = runner.prepare_credentials_before_registry

        def committed(**kwargs: Any) -> None:
            original(**kwargs)
            query(
                "postgres",
                "INSERT INTO devkit_boundary_events (database_name,event) VALUES (%s,'commit')",
                (runner.local.db_name,),
            )

        failure = None
        with patch.object(runner, "prepare_credentials_before_registry", side_effect=committed):
            try:
                if restore:
                    runner.run_restore()
                else:
                    runner.run_post_deploy_maintenance()
            except workflows.OdooRestorerError as error:
                failure = error
        events = [
            row[0]
            for row in query(
                "postgres", "SELECT event FROM devkit_boundary_events WHERE database_name=%s ORDER BY id", (runner.local.db_name,)
            )
        ]
        self.assertFalse(
            any(event.startswith("blocked_") for event in events), "Unstripped credential reached a blocked outbound sink"
        )
        if failure:
            raise failure
        self.assertTrue(events and events[0] == "commit", "Credential-capable hook ran before commit")
        for hook in ("register_hook", "pre_init_hook", "post_init_hook", "update_hook"):
            self.assertIn(hook, events, f"Real {hook} was not exercised")
        print("DEVKIT_BOUNDARY_ORDER " + json.dumps({"path": "restore" if restore else "post_deploy", "events": events}), flush=True)

    @staticmethod
    @contextmanager
    def late_boundary_fault(runner: workflows.OdooDataWorkflowRunner) -> Iterator[None]:
        prepare = runner.prepare_credentials_before_registry
        install = runner.install_addons
        deferred: dict[str, Any] = {}

        def defer(**kwargs: Any) -> None:
            deferred.update(kwargs)

        def install_before_boundary(**kwargs: Any) -> None:
            install(**kwargs)
            prepare(**deferred)

        # Reproduce the original ordering defect: registry/install hooks execute
        # before the real stripping transaction, rather than deleting the strip.
        with (
            patch.object(runner, "prepare_credentials_before_registry", side_effect=defer),
            patch.object(runner, "install_addons", side_effect=install_before_boundary),
        ):
            yield

    def test_post_deploy_credentials_commit_before_real_hooks(self) -> None:
        self.guarded(
            self.credential_boundary_check,
            self.late_boundary_fault,
            fault_message="Unstripped credential reached a blocked outbound sink",
        )

    def test_restore_credentials_commit_before_real_hooks(self) -> None:
        self.guarded(
            lambda runner: self.credential_boundary_check(runner, restore=True),
            self.late_boundary_fault,
            fault_message="Unstripped credential reached a blocked outbound sink",
        )

    def test_openupgrade_reasserts_sanitize_before_install_hooks(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            runner.local.openupgrade_enabled = True
            runner.local.openupgrade_skip_update_addons = False

            def migration() -> None:
                query(runner.local.db_name, "UPDATE ir_cron SET active=true")
                query(
                    runner.local.db_name,
                    "INSERT INTO ir_config_parameter (key,value) VALUES ('web_map.token_map_box','inert-migration-sentinel') "
                    "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                )

            # Exercise the real restore/OpenUpgrade branch with representative
            # migration SQL, without requiring an unrelated version migration.
            with patch.object(runner, "run_openupgrade", side_effect=migration):
                self.credential_boundary_check(runner, restore=True)

        def omit_post_migration_boundary(runner: workflows.OdooDataWorkflowRunner) -> Any:
            original = runner.prepare_credentials_before_registry
            calls = 0

            def prepare(**kwargs: Any) -> None:
                nonlocal calls
                calls += 1
                if calls != 2:
                    original(**kwargs)

            return patch.object(runner, "prepare_credentials_before_registry", side_effect=prepare)

        self.guarded(check, omit_post_migration_boundary, fault_message="Unstripped credential reached a blocked outbound sink")

    def test_production_alias_preserves_credentials_in_the_real_settings_consumer(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            query(
                runner.local.db_name,
                "INSERT INTO ir_config_parameter (key,value) VALUES ('shopify.api_token','inert-production-key')",
            )
            settings = runner.local.model_copy(update={"platform_instance": " production ", "db_conn": None})
            production = workflows.OdooDataWorkflowRunner(settings, None, None)
            try:
                production.apply_environment_overrides()
                self.assertEqual(
                    query(runner.local.db_name, "SELECT value FROM ir_config_parameter WHERE key='shopify.api_token'"),
                    [("inert-production-key",)],
                    "Production alias was treated as a non-production lane",
                )
            finally:
                production._reset_db_connection()

        original = workflows.OdooDataWorkflowRunner.__init__

        def omit_lane_normalization(_runner: workflows.OdooDataWorkflowRunner) -> Any:
            def initialize(instance: workflows.OdooDataWorkflowRunner, *args: Any, **kwargs: Any) -> None:
                original(instance, *args, **kwargs)
                instance.os_env["PLATFORM_INSTANCE"] = instance.local.platform_instance

            return patch.object(workflows.OdooDataWorkflowRunner, "__init__", new=initialize)

        self.guarded(check, omit_lane_normalization, fault_message="Production alias was treated as a non-production lane")

    def test_module_crons_are_disabled_before_followup_registry_hooks(self) -> None:
        def check(runner: workflows.OdooDataWorkflowRunner) -> None:
            update = runner.update_addons

            def update_with_cron_data(**kwargs: Any) -> None:
                update(**kwargs)
                # Representative module data committed by the update process.
                query(runner.local.db_name, "UPDATE ir_cron SET active=true")

            with patch.object(runner, "update_addons", side_effect=update_with_cron_data):
                self.credential_boundary_check(runner, restore=True)

        def omit_final_sanitize(runner: workflows.OdooDataWorkflowRunner) -> Any:
            prepare = runner.prepare_credentials_before_registry
            calls = 0

            def boundary(**kwargs: Any) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    kwargs["do_sanitize"] = False
                prepare(**kwargs)

            return patch.object(runner, "prepare_credentials_before_registry", side_effect=boundary)

        self.guarded(check, omit_final_sanitize, fault_message="Unstripped credential reached a blocked outbound sink")

    def test_failed_and_rolled_back_boundary_commit_starts_no_odoo_hook(self) -> None:
        for mode in ("failed", "rolled_back", "production_rolled_back"):
            with self.subTest(mode=mode), self.target() as runner:
                query(
                    "postgres",
                    "CREATE TABLE IF NOT EXISTS devkit_boundary_events (id bigserial PRIMARY KEY, database_name text, event text)",
                )
                connection = runner.connect_to_db()

                class BrokenCommit:
                    def __init__(self, wrapped: Any, failure_mode: str) -> None:
                        self.wrapped = wrapped
                        self.failure_mode = failure_mode

                    def __getattr__(self, name: str) -> Any:
                        return getattr(self.wrapped, name)

                    def commit(self) -> None:
                        self.wrapped.rollback()
                        if self.failure_mode == "failed":
                            raise workflows.OdooDatabaseUpdateError("planted commit failure")

                runner.local.db_conn = BrokenCommit(connection, mode)
                if mode == "production_rolled_back":
                    runner.local.platform_instance = "prod"
                runner.os_env.update(DEVKIT_BOUNDARY_PROBE="1", DEVKIT_LANE_KEY="inert-lane-sentinel")
                query(
                    runner.local.db_name,
                    "INSERT INTO ir_config_parameter (key,value) VALUES ('printnode.api_key','inert-production-sentinel') ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                )
                with self.assertRaises(workflows.OdooDatabaseUpdateError):
                    runner.run_post_deploy_maintenance()
                self.assertEqual(
                    query("postgres", "SELECT event FROM devkit_boundary_events WHERE database_name=%s", (runner.local.db_name,)), []
                )
                print(f"DEVKIT_FAULT_DETECTED boundary_commit_{mode}", flush=True)
