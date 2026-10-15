from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tests.test_odoo_website_bootstrap import FakeRecord


def load_consumer() -> types.ModuleType:
    path = Path(__file__).resolve().parents[1] / "docker/scripts/odoo_website_settings.py"
    spec = importlib.util.spec_from_file_location("website_settings_consumer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


consumer = load_consumer()


class Website(FakeRecord):
    def __len__(self) -> int:
        return 1 if self else 0

    def exists(self) -> Website:
        return self

    def flush_recordset(self, fields: list[str]) -> None:
        self.flushed = True

    def invalidate_recordset(self, fields: list[str]) -> None:
        if getattr(self, "cache_only", False):
            self.google_analytics_key = "G-COPIED"


class Websites:
    def __init__(self, records: list[Website]) -> None:
        self.records = records

    def sudo(self) -> Websites:
        return self

    def browse(self, record_id: int) -> Website:
        return next((record for record in self.records if record.id == record_id), Website(truthy=False))

    def search(self, domain: list[object]) -> list[Website]:
        return self.records


class Parameters:
    def __init__(self) -> None:
        self.values = {consumer.INDEXING_PARAMETER: "1"}
        self.persist = True

    def sudo(self) -> Parameters:
        return self

    def set_param(self, key: str, value: str) -> None:
        if self.persist:
            self.values[key] = value

    def get_param(self, key: str) -> str | None:
        return self.values.get(key)


class Environment:
    registry = {"website"}

    def __init__(self) -> None:
        self.websites = Websites(
            [
                Website(
                    record_id=number,
                    fields=(*consumer.NATIVE_FIELDS, "domain"),
                    values={
                        "domain": "https://site.example",
                        "google_analytics_key": "G-COPIED",
                    },
                )
                for number in (1, 2)
            ]
        )
        self.parameters = Parameters()

    def __getitem__(self, key: str) -> Any:
        return self.websites if key == "website" else self.parameters


def settings() -> dict[str, object]:
    return {
        "operation": "apply",
        "website_id": 1,
        "public_origin": "https://site.example",
        "public_indexing_enabled": True,
        "google_analytics_key": "G-TEST123",
        "cookies_bar": True,
        "block_third_party_domains": True,
    }


class WebsiteSettingsTests(unittest.TestCase):
    def apply(self, env: Environment, values: dict[str, object] | None = None, *, instance: str = "prod") -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            result = consumer.apply_website_settings(
                env, {"instance": "prod", "website_settings": values or settings()}, instance=instance
            )
        return result

    def test_native_apply_selects_one_website_and_verifies_digest(self) -> None:
        env = Environment()
        output = self.apply(env)
        self.assertEqual(env.websites.records[0].google_analytics_key, settings()["google_analytics_key"])
        self.assertEqual(env.websites.records[1].google_analytics_key, "G-COPIED")
        self.assertTrue(env.websites.records[0].cookies_bar)
        self.assertEqual(output, hashlib.sha256(json.dumps(settings(), sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "1")

    def test_restored_production_settings_are_cleared_on_every_nonproduction_site(self) -> None:
        for instance in ("testing", "preview-pr-5", "", "unknown"):
            with self.subTest(instance=instance):
                env = Environment()
                self.apply(env, instance=instance)
                self.assertTrue(all(not record.google_analytics_key for record in env.websites.records))
                self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "0")

    def test_exclusion_does_not_require_a_launchplane_payload(self) -> None:
        env = Environment()
        consumer.apply_website_settings(env, None, instance="testing")
        self.assertTrue(all(not record.google_analytics_key for record in env.websites.records))

    def test_origin_and_consent_refusals_precede_native_writes(self) -> None:
        for change in (
            {"public_origin": "https://other.example"},
            {"cookies_bar": False},
            {"block_third_party_domains": False},
            {"website_id": True},
            {"untyped": "value"},
        ):
            with self.subTest(change=change):
                env = Environment()
                with self.assertRaises(RuntimeError):
                    self.apply(env, {**settings(), **change})
                self.assertFalse(env.websites.records[0].writes)

    def test_write_readback_failure_refuses_success(self) -> None:
        env = Environment()
        env.websites.records[0].persist_writes = False
        with self.assertRaises(RuntimeError):
            self.apply(env)

    def test_rollback_disables_forward_settings(self) -> None:
        env = Environment()
        self.apply(env)
        self.apply(env, {**settings(), "google_analytics_key": "", "public_indexing_enabled": False})
        self.assertFalse(env.websites.records[0].google_analytics_key)
        self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "0")

    def test_database_readback_rejects_a_cache_only_write(self) -> None:
        env = Environment()
        env.websites.records[0].cache_only = True
        with self.assertRaises(RuntimeError):
            self.apply(env)

    def test_web_startup_clears_indexing_reapplied_by_legacy_settings(self) -> None:
        from types import SimpleNamespace

        from tests.test_odoo_startup import OdooStartupDependencySyncTests, odoo_startup

        env = Environment()
        env.registry = {"website", "launchplane.settings"}

        def committed() -> None:
            self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "0")
            self.assertTrue(all(not record.google_analytics_key for record in env.websites.records))

        env.cr = SimpleNamespace(commit=Mock(side_effect=committed))
        env.parameters.apply_from_env = lambda: env.parameters.set_param(consumer.INDEXING_PARAMETER, "1")
        bootstrap = types.ModuleType("odoo_website_bootstrap")
        bootstrap.load_instance_override_payload = lambda: {"instance": "testing"}
        bootstrap.payload_has_launchplane_settings = lambda payload: True
        bootstrap.require_launchplane_payloads_if_configured = lambda payload: None
        bootstrap.apply_website_bootstrap = lambda environment, payload: None

        def shell(settings: object, program: str, *, label: str) -> None:
            exec(program, {"env": env})

        with (
            patch.dict("sys.modules", {"odoo_website_bootstrap": bootstrap, "odoo_website_settings": consumer}),
            patch.object(odoo_startup, "_run_odoo_shell", side_effect=shell),
        ):
            odoo_startup._apply_environment_overrides_if_available(
                OdooStartupDependencySyncTests._settings(platform_instance="testing")
            )
        self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "0")
        self.assertTrue(all(not record.google_analytics_key for record in env.websites.records))
        env.cr.commit.assert_called_once()

    def test_nonproduction_refuses_failed_indexing_readback(self) -> None:
        env = Environment()
        env.parameters.persist = False
        with self.assertRaises(RuntimeError):
            consumer.apply_website_settings(env, None, instance="testing")

    def test_data_workflow_script_excludes_copied_settings_before_final_commit(self) -> None:
        from contextlib import nullcontext
        from types import SimpleNamespace

        from tests.test_odoo_data_workflows import odoo_data_workflows as workflows

        env = Environment()
        env.registry = {"website", "launchplane.settings"}
        commits = []
        env.cr = SimpleNamespace(
            commit=lambda: commits.append(
                (
                    env.parameters.get_param(consumer.INDEXING_PARAMETER),
                    [record.google_analytics_key for record in env.websites.records],
                )
            )
        )
        env.parameters.apply_from_env = lambda: env.parameters.set_param(consumer.INDEXING_PARAMETER, "1")
        bootstrap = types.ModuleType("odoo_website_bootstrap")
        bootstrap.load_instance_override_payload = lambda: {"instance": "testing"}
        bootstrap.payload_has_launchplane_settings = lambda payload: True
        bootstrap.require_launchplane_payloads_if_configured = lambda payload: None
        bootstrap.apply_website_bootstrap = lambda environment, payload: None
        odoo = types.ModuleType("odoo")
        odoo.api = SimpleNamespace(Environment=lambda cursor, uid, context: env)
        odoo.SUPERUSER_ID = 1
        registry = types.ModuleType("odoo.modules.registry")
        registry.Registry = lambda database: SimpleNamespace(cursor=lambda: nullcontext(env.cr))
        runner = object.__new__(workflows.OdooDataWorkflowRunner)
        runner.local = SimpleNamespace(db_name="fixture-copy", platform_instance="testing")

        def shell(program: str, label: str) -> None:
            exec(program, {})

        with (
            patch.dict(
                "sys.modules",
                {
                    "odoo": odoo,
                    "odoo.modules.registry": registry,
                    "odoo_website_bootstrap": bootstrap,
                    "odoo_website_settings": consumer,
                },
            ),
            patch.object(runner, "_run_odoo_shell", side_effect=shell),
        ):
            runner.apply_environment_overrides()
        self.assertEqual(commits[-1][0], "0")
        self.assertTrue(all(not key for key in commits[-1][1]))

    def test_historical_rollback_recovers_from_an_invalid_forward_selector(self) -> None:
        env = Environment()
        self.apply(
            env,
            {
                **settings(),
                "operation": "disable_collection",
                "website_id": 99999,
                "public_origin": "https://wrong.example",
                "public_indexing_enabled": False,
                "google_analytics_key": "",
                "cookies_bar": None,
                "block_third_party_domains": None,
            },
        )
        self.assertTrue(all(not record.google_analytics_key for record in env.websites.records))
        self.assertEqual(env.parameters.get_param(consumer.INDEXING_PARAMETER), "0")

    def test_historical_rollback_preserves_unrequested_consent_controls(self) -> None:
        env = Environment()
        env.websites.records[0].cookies_bar = False
        env.websites.records[0].block_third_party_domains = True
        self.apply(
            env,
            {
                **settings(),
                "operation": "disable_collection",
                "google_analytics_key": "",
                "public_indexing_enabled": False,
                "cookies_bar": None,
                "block_third_party_domains": None,
            },
        )
        self.assertFalse(env.websites.records[0].google_analytics_key)
        self.assertFalse(env.websites.records[0].cookies_bar)
        self.assertTrue(env.websites.records[0].block_third_party_domains)
