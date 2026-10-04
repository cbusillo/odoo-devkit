from __future__ import annotations

import argparse
import configparser
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from odoo_devkit.local_runtime import load_environment_from_explicit_payload

if TYPE_CHECKING:
    from docker.scripts import run_odoo_startup as odoo_startup
    from docker.scripts.run_odoo_startup import StartupSettings


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


if not TYPE_CHECKING:
    odoo_startup = _load_startup_module()


class _FakeUsers:
    """Stand-in for Odoo's res.users model as the default-password policy uses it."""

    def __init__(self, *, existing_logins: set[str], default_password_logins: set[str]) -> None:
        self.existing_logins = existing_logins
        self.default_password_logins = default_password_logins
        self.authenticated_logins: list[str] = []

    def sudo(self) -> _FakeUsers:
        return self

    def with_context(self, **_context: object) -> _FakeUsers:
        return self

    def search(self, domain: list[tuple[str, str, str]], limit: int) -> bool:
        _ = limit
        return domain[0][2] in self.existing_logins

    def authenticate(self, credential: dict[str, str], _user_agent_environment: dict[str, bool]) -> dict[str, int]:
        login = credential["login"]
        self.authenticated_logins.append(login)
        if login in self.default_password_logins and credential["password"] == "admin":
            return {"uid": 1}
        raise PermissionError


class OdooStartupDependencySyncTests(unittest.TestCase):
    @staticmethod
    def _settings(
        *,
        platform_instance: str = "local",
        master_password: str = "master-password",
        admin_password: str = "",
    ) -> StartupSettings:
        return odoo_startup.StartupSettings(
            config_path="/tmp/generated.conf",
            base_config_path="/tmp/base.conf",
            platform_instance=platform_instance,
            database_name="opw",
            database_host="database",
            database_port=5432,
            database_user="odoo",
            database_password="database-password",
            master_password=master_password,
            admin_login="admin",
            admin_password=admin_password,
            addons_path="/odoo/addons",
            data_dir="/volumes/data",
            list_db="False",
            install_modules=("opw_custom",),
            data_workflow_lock_file="/volumes/data/.data_workflow_in_progress",
            data_workflow_lock_timeout_seconds=7200,
            ready_timeout_seconds=180,
            poll_interval_seconds=2.0,
        )

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
        self.assertEqual(settings.addons_path, "/opt/launchplane/addons,/odoo/addons")

    def test_load_settings_preserves_launchplane_addon_path_first(self) -> None:
        environment = {
            "PLATFORM_INSTANCE": "local",
            "ODOO_DB_NAME": "opw",
            "ODOO_DB_HOST": "database",
            "ODOO_DB_PORT": "5432",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "database-password",
            "ODOO_MASTER_PASSWORD": "master-password",
            "ODOO_ADDONS_PATH": "/opt/project/addons,/opt/launchplane/addons,/odoo/addons",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = odoo_startup._load_settings(argparse.Namespace(config_path="/tmp/generated.conf"))

        self.assertEqual(
            settings.addons_path,
            "/opt/launchplane/addons,/opt/project/addons,/odoo/addons",
        )

    @staticmethod
    def test_sync_python_dependencies_runs_for_local_dev_runtime() -> None:
        settings = OdooStartupDependencySyncTests._settings()

        with (
            patch.dict(os.environ, {"ODOO_DEV_MODE": "reload"}, clear=True),
            patch.object(odoo_startup, "_install_local_addon_dependencies") as mocked_install_dependencies,
        ):
            odoo_startup._sync_python_dependencies_if_needed(settings)

        mocked_install_dependencies.assert_called_once_with("dev")

    @staticmethod
    def test_sync_python_dependencies_skips_non_local_runtime() -> None:
        settings = OdooStartupDependencySyncTests._settings(platform_instance="prod")

        with patch.object(odoo_startup, "_install_local_addon_dependencies") as mocked_install_dependencies:
            odoo_startup._sync_python_dependencies_if_needed(settings)

        mocked_install_dependencies.assert_not_called()

    def test_public_runtime_rejects_default_master_password(self) -> None:
        settings = self._settings(
            platform_instance="preview",
            master_password="admin",
            admin_password="safe-admin-password",
        )

        with self.assertRaisesRegex(RuntimeError, "ODOO_MASTER_PASSWORD"):
            odoo_startup._enforce_public_credential_preflight(settings)

    def test_public_runtime_requires_configured_admin_password(self) -> None:
        for password in ("", " ", "\t", " \t "):
            with self.subTest(password=password):
                settings = self._settings(platform_instance="testing", admin_password=password)
                with self.assertRaisesRegex(RuntimeError, "ODOO_ADMIN_PASSWORD"):
                    odoo_startup._enforce_public_credential_preflight(settings)
                with patch.object(odoo_startup, "_run_odoo_shell") as run_shell:
                    odoo_startup._apply_admin_password_if_configured(settings)
                run_shell.assert_not_called()

    def test_public_runtime_accepts_non_default_configured_credentials(self) -> None:
        settings = self._settings(
            platform_instance="prod",
            admin_password="safe-admin-password",
        )

        odoo_startup._enforce_public_credential_preflight(settings)

    def test_empty_or_unset_platform_instance_requires_public_credentials(self) -> None:
        environment = {
            "ODOO_DB_NAME": "opw",
            "ODOO_MASTER_PASSWORD": "master-password",
        }
        for platform_instance in (None, "", "  "):
            with self.subTest(platform_instance=platform_instance):
                instance_environment = dict(environment)
                if platform_instance is not None:
                    instance_environment["PLATFORM_INSTANCE"] = platform_instance
                with patch.dict(os.environ, instance_environment, clear=True):
                    settings = odoo_startup._load_settings(argparse.Namespace(config_path="/tmp/generated.conf"))

                with self.assertRaisesRegex(RuntimeError, "ODOO_ADMIN_PASSWORD"):
                    odoo_startup._enforce_public_credential_preflight(settings)

    def test_explicit_local_instance_names_skip_public_credential_preflight(self) -> None:
        for platform_instance in ("local", "dev", "development", "Local"):
            with self.subTest(platform_instance=platform_instance):
                settings = self._settings(platform_instance=platform_instance, master_password="admin")

                odoo_startup._enforce_public_credential_preflight(settings)

    def test_database_filter_applies_unless_instance_is_explicitly_local(self) -> None:
        for platform_instance, expected_filter in (("", "^opw$"), ("preview", "^opw$"), ("local", None)):
            with self.subTest(platform_instance=platform_instance):
                settings = self._settings(platform_instance=platform_instance, admin_password="safe-admin-password")
                parser = configparser.ConfigParser(interpolation=None)

                with TemporaryDirectory() as directory:
                    settings = replace(settings, config_path=str(Path(directory) / "odoo.conf"), base_config_path="")
                    with patch.dict(os.environ, {}, clear=True):
                        odoo_startup._write_runtime_config(settings)
                    parser.read(settings.config_path)

                self.assertEqual(parser["options"].get("dbfilter"), expected_filter)
                odoo_command = odoo_startup._build_odoo_command(settings, stop_after_init=False)
                self.assertEqual(f"--db-filter={expected_filter}" in odoo_command, expected_filter is not None)

    def test_public_runtime_config_pins_http_database_filter_to_configured_database(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="safe-admin-password")
        parser = configparser.ConfigParser(interpolation=None)

        with TemporaryDirectory() as directory:
            settings = replace(settings, config_path=str(Path(directory) / "odoo.conf"), base_config_path="")
            with patch.dict(os.environ, {}, clear=True):
                odoo_startup._write_runtime_config(settings)
            parser.read(settings.config_path)

        self.assertEqual(parser["options"]["db_name"], "opw")
        self.assertEqual(parser["options"]["dbfilter"], "^opw$")

    def test_local_runtime_config_does_not_pin_http_database_filter(self) -> None:
        settings = self._settings()
        parser = configparser.ConfigParser(interpolation=None)

        with TemporaryDirectory() as directory:
            settings = replace(settings, config_path=str(Path(directory) / "odoo.conf"), base_config_path="")
            with patch.dict(os.environ, {}, clear=True):
                odoo_startup._write_runtime_config(settings)
            parser.read(settings.config_path)

        self.assertEqual(parser["options"]["db_name"], "opw")
        self.assertNotIn("dbfilter", parser["options"])

    def test_managed_mail_options_replace_base_config_without_logging_password(self) -> None:
        environment = {
            "ODOO_SMTP_SERVER": "smtp.example.test",
            "ODOO_SMTP_PORT": "587",
            "ODOO_SMTP_USER": "mailbox@example.test",
            "ODOO_SMTP_PASSWORD": "secret%with#punctuation",
            "ODOO_SMTP_SSL": "True",
            "ODOO_EMAIL_FROM": "support@example.test",
            "ODOO_FROM_FILTER": "support@example.test",
        }
        output = io.StringIO()
        with TemporaryDirectory() as directory:
            base = Path(directory) / "base.conf"
            target = Path(directory) / "runtime.conf"
            base.write_text("[options]\nsmtp_server = old.example.test\nsmtp_password = old-secret\n", encoding="utf-8")
            target.touch(mode=0o644)
            target.chmod(0o644)
            settings = replace(self._settings(), base_config_path=str(base), config_path=str(target))
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
                odoo_startup._write_runtime_config(settings)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(target)
            self.assertEqual(parser["options"]["smtp_server"], "smtp.example.test")
            self.assertEqual(parser["options"].getint("smtp_port"), 587)
            self.assertEqual(parser["options"]["smtp_user"], "mailbox@example.test")
            self.assertEqual(parser["options"]["smtp_password"], environment["ODOO_SMTP_PASSWORD"])
            self.assertTrue(parser["options"].getboolean("smtp_ssl"))
            self.assertEqual(parser["options"]["email_from"], "support@example.test")
            self.assertEqual(parser["options"]["from_filter"], "support@example.test")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {"ODOO_SMTP_PASSWORD": ""}, clear=True):
                odoo_startup._write_runtime_config(settings)
            parser.read(target)
            self.assertEqual(parser["options"]["smtp_password"], "")
        self.assertNotIn(environment["ODOO_SMTP_PASSWORD"], output.getvalue())

    def test_database_filter_escapes_database_name(self) -> None:
        pattern = odoo_startup._database_filter_pattern("tenant.prod")

        self.assertEqual(pattern, r"^tenant\.prod$")

    def test_public_odoo_server_command_pins_database_filter_to_configured_database(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="safe-admin-password")

        command = odoo_startup._build_odoo_command(settings, stop_after_init=False)

        self.assertIn("-d", command)
        self.assertIn("opw", command)
        self.assertIn("--db-filter=^opw$", command)

    def test_public_odoo_init_command_pins_database_filter_to_configured_database(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="safe-admin-password")

        command = odoo_startup._build_odoo_command(
            settings,
            initialize_modules=("opw_custom",),
            stop_after_init=True,
        )

        self.assertIn("--db-filter=^opw$", command)
        self.assertIn("--stop-after-init", command)

    def test_local_odoo_server_command_does_not_pin_database_filter(self) -> None:
        settings = self._settings()

        command = odoo_startup._build_odoo_command(settings, stop_after_init=False)

        self.assertNotIn("--db-filter=^opw$", command)

    def test_odoo_shell_command_does_not_pin_database_filter(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="safe-admin-password")

        command = odoo_startup._build_odoo_shell_command(settings)

        self.assertFalse(any(argument.startswith("--db-filter=") for argument in command))

    def test_local_runtime_allows_missing_admin_password(self) -> None:
        settings = self._settings()

        odoo_startup._enforce_public_credential_preflight(settings)

    def test_odoo_shell_subprocess_can_import_runtime_script_helpers(self) -> None:
        settings = self._settings()

        with (
            patch.dict(os.environ, {"PYTHONPATH": "/opt/custom:/volumes/scripts"}, clear=True),
            patch.object(odoo_startup.subprocess, "run") as run_mock,
        ):
            odoo_startup._run_odoo_shell(settings, "from odoo_website_bootstrap import apply_website_bootstrap", label="test")

        run_mock.assert_called_once()
        environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONPATH"], "/volumes/scripts:/opt/custom")

    def test_odoo_shell_subprocess_prepends_runtime_scripts_to_pythonpath(self) -> None:
        settings = self._settings()

        with (
            patch.dict(os.environ, {"PYTHONPATH": "/opt/custom"}, clear=True),
            patch.object(odoo_startup.subprocess, "run") as run_mock,
        ):
            odoo_startup._run_odoo_shell(settings, "from odoo_website_bootstrap import apply_website_bootstrap", label="test")

        run_mock.assert_called_once()
        environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONPATH"], "/volumes/scripts:/opt/custom")

    @staticmethod
    def _execute_admin_hardening(settings: StartupSettings, environment: MagicMock) -> str:
        exceptions = types.ModuleType("odoo.exceptions")
        exceptions.__dict__["AccessDenied"] = PermissionError

        def run_shell(_settings: StartupSettings, script: str, *, label: str) -> None:
            _ = label
            exec(script, {"env": environment})

        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"odoo.exceptions": exceptions}),
            patch.object(odoo_startup, "_run_odoo_shell", side_effect=run_shell),
            redirect_stdout(output),
        ):
            odoo_startup._apply_admin_password_if_configured(settings)
        return output.getvalue()

    def test_admin_hardening_only_writes_when_configured_password_changes(self) -> None:
        configured_password = " \tconfigured-'\"\\-password\t "
        values = load_environment_from_explicit_payload(
            raw_payload=json.dumps(
                {
                    "context": "probe",
                    "instance": "local",
                    "environment": {
                        "ODOO_DB_NAME": "probe",
                        "ODOO_MASTER_PASSWORD": "fake-master",
                        "ODOO_ADMIN_PASSWORD": configured_password,
                    },
                }
            ),
            context_name="probe",
            instance_name="local",
        ).merged_values
        with patch.dict(os.environ, values, clear=True):
            settings = odoo_startup._load_settings(argparse.Namespace(config_path="/tmp/generated.conf"))
        self.assertEqual(settings.admin_password, configured_password)
        environment = MagicMock()
        admin = environment["res.users"].sudo().with_context().search()
        admin.with_user.return_value = admin
        admin.with_context.return_value = admin
        admin.sudo.return_value = admin
        stored = {"password": "initial-password"}

        def check_credentials(credential: dict[str, str], _request_environment: dict[str, bool]) -> None:
            if credential["password"] != stored["password"]:
                raise PermissionError

        admin._check_credentials.side_effect = check_credentials
        admin.write.side_effect = stored.update
        self._execute_admin_hardening(settings, environment)
        self._execute_admin_hardening(settings, environment)
        admin.write.assert_called_once_with({"password": configured_password})

        rotated = replace(settings, admin_password="rotated-password")
        self._execute_admin_hardening(rotated, environment)
        self._execute_admin_hardening(rotated, environment)
        self.assertEqual(admin.write.call_count, 2)
        self.assertEqual(stored["password"], "rotated-password")
        environment.cr.commit.assert_called()

    def test_admin_hardening_skips_missing_configured_admin(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="configured-password")
        environment = MagicMock()
        users = environment["res.users"].sudo().with_context()
        users.search.return_value = None

        output = self._execute_admin_hardening(settings, environment)

        self.assertIn("configured_admin_user_found=false", output)
        environment.cr.commit.assert_not_called()

    def test_admin_hardening_does_not_write_after_unexpected_credential_check_failure(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="configured-password")
        environment = MagicMock()
        admin = environment["res.users"].sudo().with_context().search()
        admin.with_user.return_value = admin
        admin._check_credentials.side_effect = RuntimeError("credential backend unavailable")

        with self.assertRaisesRegex(RuntimeError, "credential backend unavailable"):
            self._execute_admin_hardening(settings, environment)

        admin.with_context.assert_not_called()
        environment.cr.commit.assert_not_called()

    _STARTUP_STEP_NAMES = (
        "_enforce_public_credential_preflight",
        "_write_runtime_config",
        "_wait_for_database",
        "_wait_for_data_workflow_lock",
        "_sync_python_dependencies_if_needed",
        "_run_initialization_if_needed",
        "_apply_environment_overrides_if_available",
        "_apply_admin_password_if_configured",
        "_assert_active_admin_password_is_not_default",
    )

    def _run_main(self, settings: StartupSettings, recorder: MagicMock) -> list[str]:
        with ExitStack() as stack:
            stack.enter_context(patch.object(odoo_startup, "_parse_arguments", return_value=argparse.Namespace()))
            stack.enter_context(patch.object(odoo_startup, "_load_settings", return_value=settings))
            for step_name in self._STARTUP_STEP_NAMES:
                stack.enter_context(patch.object(odoo_startup, step_name, getattr(recorder, step_name)))
            stack.enter_context(patch.object(odoo_startup.os, "execv", recorder.execv))
            stack.enter_context(redirect_stdout(io.StringIO()))
            odoo_startup.main()
        return [recorded_call[0] for recorded_call in recorder.mock_calls]

    def assertRunsBefore(self, call_names: list[str], earlier: str, later: str) -> None:
        self.assertIn(earlier, call_names)
        self.assertIn(later, call_names)
        self.assertLess(call_names.index(earlier), call_names.index(later), f"{earlier} must run before {later}")

    def test_startup_runs_admin_password_policy_for_public_runtimes_and_configured_passwords(self) -> None:
        cases = (
            ("public runtime", self._settings(platform_instance="testing", admin_password="configured-password"), True),
            ("local runtime with configured password", self._settings(admin_password="configured-password"), True),
            ("local runtime without configured password", self._settings(), False),
            ("local runtime with blank password", self._settings(admin_password=" \t "), False),
        )
        for case_name, settings, expects_policy in cases:
            with self.subTest(case_name):
                call_names = self._run_main(settings, MagicMock())

                self.assertEqual(call_names[-1], "execv")
                self.assertEqual(call_names[0], "_enforce_public_credential_preflight")
                self.assertRunsBefore(call_names, "_wait_for_data_workflow_lock", "_run_initialization_if_needed")
                self.assertRunsBefore(call_names, "_run_initialization_if_needed", "_apply_environment_overrides_if_available")
                self.assertRunsBefore(call_names, "_apply_environment_overrides_if_available", "_apply_admin_password_if_configured")
                if expects_policy:
                    self.assertRunsBefore(
                        call_names, "_apply_admin_password_if_configured", "_assert_active_admin_password_is_not_default"
                    )
                else:
                    self.assertNotIn("_assert_active_admin_password_is_not_default", call_names)

    def test_startup_stops_before_touching_odoo_when_credential_preflight_fails(self) -> None:
        recorder = MagicMock()
        recorder._enforce_public_credential_preflight.side_effect = RuntimeError("Insecure configuration")

        with self.assertRaisesRegex(RuntimeError, "Insecure configuration"):
            self._run_main(self._settings(platform_instance="testing"), recorder)

        self.assertEqual([recorded_call[0] for recorded_call in recorder.mock_calls], ["_enforce_public_credential_preflight"])

    def test_startup_does_not_start_server_when_admin_password_policy_fails(self) -> None:
        recorder = MagicMock()
        recorder._assert_active_admin_password_is_not_default.side_effect = ValueError("Insecure configuration")

        with self.assertRaisesRegex(ValueError, "Insecure configuration"):
            self._run_main(self._settings(platform_instance="testing", admin_password="configured-password"), recorder)

        recorder.execv.assert_not_called()

    @staticmethod
    def _execute_default_password_policy(settings: StartupSettings, users: _FakeUsers) -> None:
        exceptions = types.ModuleType("odoo.exceptions")
        exceptions.__dict__["AccessDenied"] = PermissionError

        def run_shell(_settings: StartupSettings, script: str, *, label: str) -> None:
            _ = label
            exec(script, {"env": {"res.users": users}})

        with (
            patch.dict(sys.modules, {"odoo.exceptions": exceptions}),
            patch.object(odoo_startup, "_run_odoo_shell", side_effect=run_shell),
            redirect_stdout(io.StringIO()),
        ):
            odoo_startup._assert_active_admin_password_is_not_default(settings)

    def test_default_password_policy_rejects_admin_password_on_default_or_configured_login(self) -> None:
        configured_login = "ops'lead\\\""
        settings = replace(
            self._settings(platform_instance="testing", admin_password="configured-password"), admin_login=configured_login
        )
        for login_with_default_password in ("admin", configured_login):
            with self.subTest(login=login_with_default_password):
                users = _FakeUsers(
                    existing_logins={"admin", configured_login},
                    default_password_logins={login_with_default_password},
                )

                with self.assertRaisesRegex(ValueError, "Insecure configuration"):
                    self._execute_default_password_policy(settings, users)

    def test_default_password_policy_accepts_hardened_accounts_and_checks_every_login(self) -> None:
        configured_login = "ops'lead\\\""
        settings = replace(
            self._settings(platform_instance="testing", admin_password="configured-password"), admin_login=configured_login
        )
        users = _FakeUsers(existing_logins={"admin", configured_login}, default_password_logins=set())

        self._execute_default_password_policy(settings, users)

        self.assertEqual(users.authenticated_logins, ["admin", configured_login])

    def test_default_password_policy_skips_missing_accounts(self) -> None:
        settings = self._settings(platform_instance="testing", admin_password="configured-password")
        users = _FakeUsers(existing_logins=set(), default_password_logins={"admin"})

        self._execute_default_password_policy(settings, users)

        self.assertEqual(users.authenticated_logins, [])

    def test_startup_waits_until_data_workflow_lock_is_released(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / ".data_workflow_in_progress"
            lock_path.write_text("pid=1\n", encoding="utf-8")
            settings = replace(self._settings(), data_workflow_lock_file=str(lock_path), data_workflow_lock_timeout_seconds=60)

            with (
                patch.object(odoo_startup.time, "sleep", side_effect=lambda _seconds: lock_path.unlink()) as sleep,
                redirect_stdout(io.StringIO()),
            ):
                odoo_startup._wait_for_data_workflow_lock(settings)

            sleep.assert_called_once()
            self.assertFalse(lock_path.exists())

    def test_startup_fails_when_data_workflow_lock_outlives_timeout(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / ".data_workflow_in_progress"
            lock_path.write_text("pid=1\n", encoding="utf-8")
            settings = replace(self._settings(), data_workflow_lock_file=str(lock_path), data_workflow_lock_timeout_seconds=0)

            wait_past_deadline = AssertionError("lock wait continued past its timeout")
            with patch.object(odoo_startup.time, "sleep", side_effect=wait_past_deadline), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, str(lock_path)):
                    odoo_startup._wait_for_data_workflow_lock(settings)

            self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
