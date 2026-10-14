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
