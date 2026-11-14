import copy
import hashlib
import io
import json
import unittest
from contextlib import contextmanager, redirect_stdout
from typing import Any

from tests.test_odoo_website_settings import Environment, consumer, settings


class Cursor:
    def __init__(self, env: Environment) -> None:
        self.env = env

    @contextmanager
    def savepoint(self) -> Any:
        before = copy.deepcopy(self.env.websites.records)
        parameters = copy.deepcopy(self.env.parameters.values)
        try:
            yield
        except BaseException:
            self.env.websites.records = before
            self.env.parameters.values = parameters
            raise


class IndexingEnvironment(Environment):
    def __init__(self) -> None:
        super().__init__()
        self.cr = Cursor(self)


class WebsiteIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IndexingEnvironment()
        self.original = '<script>window.fixture = "kept";</script><meta name="robots" content="nofollow">'
        for website in self.env.websites.records:
            website._fields.add("custom_code_head")
            website.custom_code_head = self.original
        self.env.websites.records[1].domain = "https://unrelated.example"
        self.payload: dict[str, Any] = {
            "instance": "prod",
            "website_indexing": {"website_id": 1, "public_origin": "https://site.example", "production_use": "prelaunch"},
        }

    def apply(self) -> dict[str, Any]:
        output = io.StringIO()
        with redirect_stdout(output):
            consumer.apply_website_settings(self.env, self.payload, instance="prod")
        return json.loads(output.getvalue().split("website_indexing=")[1].splitlines()[0])

    def test_plan_is_read_only_and_apply_preserves_every_unmanaged_setting(self) -> None:
        before = copy.deepcopy([vars(website) for website in self.env.websites.records])
        plan = consumer.plan_website_indexing(self.env, self.payload, instance="prod")
        self.assertEqual(before, [vars(website) for website in self.env.websites.records])
        self.assertEqual(plan["before_sha256"], hashlib.sha256(self.original.encode()).hexdigest())
        receipt = self.apply()
        website = self.env.websites.records[0]
        self.assertEqual(receipt["after_sha256"], hashlib.sha256(website.custom_code_head.encode()).hexdigest())
        self.assertEqual(receipt["after_sha256"], plan["after_sha256"])
        self.assertTrue(receipt["managed_noindex_after"])
        self.assertTrue(website.custom_code_head.endswith(self.original))
        self.assertEqual(website.google_analytics_key, "G-COPIED")
        self.assertEqual(website.domain, "https://site.example")
        self.assertEqual(vars(self.env.websites.records[1]), before[1])
        self.assertEqual(self.env.parameters.values, {consumer.INDEXING_PARAMETER: "1"})
        self.assertFalse(self.apply()["changed"])
        self.payload["website_indexing"]["production_use"] = "live"
        self.apply()
        self.assertEqual(website.custom_code_head, self.original)

    def test_invalid_or_ambiguous_bindings_refuse_before_legacy_writes(self) -> None:
        self.payload["website_settings"] = settings()
        for change in (
            {"website_id": True},
            {"website_id": 99},
            {"public_origin": "https://wrong.example"},
            {"public_origin": "https://site.example/path"},
            {"production_use": "unknown"},
            {"extra": True},
        ):
            with self.subTest(change=change):
                payload = copy.deepcopy(self.payload)
                payload["website_indexing"].update(change)
                with self.assertRaises(RuntimeError):
                    consumer.apply_website_settings(self.env, payload, instance="prod")
                self.assertFalse(any(website.writes for website in self.env.websites.records))
        self.env.websites.records[1].domain = "https://site.example/"
        with self.assertRaises(RuntimeError):
            consumer.apply_website_settings(self.env, self.payload, instance="prod")
        self.assertFalse(any(website.writes for website in self.env.websites.records))

    def test_failed_native_write_rolls_back_legacy_settings_and_keeps_protection(self) -> None:
        self.apply()
        protected = self.env.websites.records[0].custom_code_head
        self.payload["website_indexing"]["production_use"] = "live"
        self.payload["website_settings"] = settings()
        self.env.parameters.values[consumer.INDEXING_PARAMETER] = "0"
        self.env.websites.records[0].ignored_write_fields = {"custom_code_head"}
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.env.websites.records[0].custom_code_head, protected)
        self.assertEqual(self.env.websites.records[0].google_analytics_key, "G-COPIED")
        self.assertEqual(self.env.parameters.values[consumer.INDEXING_PARAMETER], "0")

    def test_combined_payload_cannot_select_different_websites(self) -> None:
        self.payload["website_settings"] = {**settings(), "website_id": 2, "public_origin": "https://unrelated.example"}
        with self.assertRaises(RuntimeError):
            consumer.apply_website_settings(self.env, self.payload, instance="prod")
        self.assertFalse(any(website.writes for website in self.env.websites.records))

    def test_nonproduction_payload_cannot_remove_protection(self) -> None:
        self.apply()
        protected = self.env.websites.records[0].custom_code_head
        self.payload["website_indexing"]["production_use"] = "live"
        with self.assertRaises(RuntimeError):
            consumer.apply_website_settings(self.env, self.payload, instance="testing")
        self.assertEqual(self.env.websites.records[0].custom_code_head, protected)
