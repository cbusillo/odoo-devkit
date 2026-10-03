"""Run the Odoo-shell snippets that apply Launchplane overrides against a stub Odoo env.

The startup and data-workflow scripts hand these snippets to `odoo shell`, so they
never execute in the unit suite on their own. These tests capture the snippet each
script would send and run it, which checks the guard behaviour rather than its text.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_odoo_data_workflows
import test_odoo_startup

odoo_data_workflows = test_odoo_data_workflows.odoo_data_workflows
odoo_startup = test_odoo_startup.odoo_startup

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "docker" / "scripts"
SETTINGS_PAYLOAD = {"config_parameters": [{"key": "web.base.url", "value": "https://example.test"}]}


def _fake_env(*, installed_models: set[str]) -> MagicMock:
    env = MagicMock()
    env.registry = installed_models
    return env


def _payload_environment(payload: dict[str, object]) -> dict[str, str]:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {"ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": encoded}


def _run_snippet(script: str, namespace: dict[str, object]) -> None:
    original_sys_path = list(sys.path)
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))
    try:
        exec(compile(script, "<odoo shell snippet>", "exec"), namespace)
    finally:
        sys.path[:] = original_sys_path


def _startup_snippet() -> str:
    captured: list[str] = []
    with patch.object(odoo_startup, "_run_odoo_shell", side_effect=lambda _settings, script, *, label: captured.append(script)):
        odoo_startup._apply_environment_overrides_if_available(test_odoo_startup.OdooStartupDependencySyncTests._settings())
    return captured[0]


def _data_workflow_snippet() -> str:
    captured: list[str] = []
    runner = odoo_data_workflows.OdooDataWorkflowRunner(
        test_odoo_data_workflows.OdooDataWorkflowShellEnvironmentTests._local_settings(), upstream=None, env_file=None
    )
    with patch.object(runner, "_run_odoo_shell", side_effect=lambda script, _label: captured.append(script)):
        runner.apply_environment_overrides()
    return captured[0]


def _run_data_workflow_snippet(env: MagicMock) -> None:
    odoo_module = types.ModuleType("odoo")
    odoo_module.SUPERUSER_ID = 1
    odoo_module.api = types.SimpleNamespace(Environment=lambda _cr, _uid, _context: env)
    registry_module = types.ModuleType("odoo.modules.registry")
    registry_module.Registry = lambda _database: MagicMock()
    stub_modules = {
        "odoo": odoo_module,
        "odoo.modules": types.ModuleType("odoo.modules"),
        "odoo.modules.registry": registry_module,
    }
    with patch.dict(sys.modules, stub_modules):
        _run_snippet(_data_workflow_snippet(), {})


class OverrideSnippetGuardTests(unittest.TestCase):
    def test_startup_rejects_settings_payload_without_launchplane_settings_addon(self) -> None:
        env = _fake_env(installed_models=set())
        with patch.dict(os.environ, _payload_environment(SETTINGS_PAYLOAD), clear=True):
            with self.assertRaisesRegex(RuntimeError, "launchplane.settings is not installed"):
                _run_snippet(_startup_snippet(), {"env": env})

    def test_startup_applies_settings_when_addon_is_installed(self) -> None:
        env = _fake_env(installed_models={"launchplane.settings"})
        output = io.StringIO()
        with patch.dict(os.environ, _payload_environment(SETTINGS_PAYLOAD), clear=True):
            with contextlib.redirect_stdout(output):
                _run_snippet(_startup_snippet(), {"env": env})

        env.__getitem__.assert_called_with("launchplane.settings")
        env.__getitem__.return_value.sudo.return_value.apply_from_env.assert_called_once_with()
        self.assertIn("launchplane_settings_applied=true", output.getvalue())

    def test_startup_reports_no_settings_payload_honestly(self) -> None:
        for installed_models in (set(), {"launchplane.settings"}):
            for payload_environment in ({}, _payload_environment({})):
                with self.subTest(installed_models=installed_models, payload_environment=payload_environment):
                    env = _fake_env(installed_models=installed_models)
                    output = io.StringIO()
                    with patch.dict(os.environ, payload_environment, clear=True), contextlib.redirect_stdout(output):
                        _run_snippet(_startup_snippet(), {"env": env})
                    reason = "no_managed_settings" if payload_environment else "no_payload"
                    self.assertIn(f"launchplane_settings_applied=false reason={reason}", output.getvalue())
                    self.assertNotIn("launchplane_settings_applied=true", output.getvalue())
                    env.cr.commit.assert_called_once_with()
                    if installed_models:
                        env.__getitem__.return_value.sudo.return_value.apply_from_env.assert_called_once_with()

    def test_startup_does_not_report_success_when_settings_apply_fails(self) -> None:
        env = _fake_env(installed_models={"launchplane.settings"})
        env.__getitem__.return_value.sudo.return_value.apply_from_env.side_effect = RuntimeError("apply failed")
        output = io.StringIO()
        with patch.dict(os.environ, _payload_environment(SETTINGS_PAYLOAD), clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "apply failed"):
                _run_snippet(_startup_snippet(), {"env": env})
        self.assertNotIn("launchplane_settings_applied=true", output.getvalue())
        env.cr.commit.assert_not_called()

    def test_startup_enforces_required_payload_flag(self) -> None:
        env = _fake_env(installed_models={"launchplane.settings"})
        with patch.dict(os.environ, {"LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED": "true"}, clear=True):
            with self.assertRaises(RuntimeError):
                _run_snippet(_startup_snippet(), {"env": env})

    def test_data_workflow_rejects_settings_payload_without_launchplane_settings_addon(self) -> None:
        env = _fake_env(installed_models=set())
        with patch.dict(os.environ, _payload_environment(SETTINGS_PAYLOAD), clear=True):
            with self.assertRaisesRegex(RuntimeError, "launchplane.settings is not installed"):
                _run_data_workflow_snippet(env)

    def test_data_workflow_skips_settings_apply_without_payload_or_addon(self) -> None:
        env = _fake_env(installed_models=set())
        with patch.dict(os.environ, {}, clear=True):
            _run_data_workflow_snippet(env)

        env.__getitem__.assert_not_called()


if __name__ == "__main__":
    unittest.main()
