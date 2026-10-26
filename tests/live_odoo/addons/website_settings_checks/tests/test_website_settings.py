"""Exercise the consumer against native Odoo fields using synthetic records."""

import hashlib
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo_website_settings import INDEXING_PARAMETER, apply_website_settings, read_string_parameter, write_string_parameter


@tagged("post_install", "-at_install")
class TestNativeWebsiteSettings(TransactionCase):
    def test_every_loaded_website_state_excludes_copied_collection(self) -> None:
        module = self.env["ir.module.module"].search([("name", "=", "website")])
        original_state = module.state
        parameters = self.env["ir.config_parameter"].sudo()
        website = self.env["website"].create({"name": "Synthetic pending website"})
        try:
            for state in ("installed", "to upgrade", "to remove"):
                module.state = state
                website.google_analytics_key = "G-SYNTHETIC"
                write_string_parameter(parameters, INDEXING_PARAMETER, "1")
                apply_website_settings(self.env, None, instance="preview")
                self.assertFalse(website.google_analytics_key)
                self.assertEqual(read_string_parameter(parameters, INDEXING_PARAMETER), "0")
        finally:
            module.state = original_state

    def test_native_fields_bootstrap_domain_and_collection_only_rollback(self) -> None:
        website = self.env["website"].create({"name": "Synthetic website", "domain": "settings.example.test"})
        settings = {
            "operation": "apply",
            "website_id": website.id,
            "public_origin": "https://settings.example.test",
            "public_indexing_enabled": True,
            "google_analytics_key": "G-SYNTHETIC",
            "cookies_bar": True,
            "block_third_party_domains": True,
        }
        payload = {"instance": "prod", "website_settings": settings}
        digest = apply_website_settings(self.env, payload, instance="prod")
        self.assertEqual(digest, hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.env.cr.execute(
            "SELECT google_analytics_key, cookies_bar, block_third_party_domains FROM website WHERE id=%s", (website.id,)
        )
        self.assertEqual(self.env.cr.fetchone(), (settings["google_analytics_key"], True, True))
        website.write({"cookies_bar": False})
        settings = {
            **settings,
            "operation": "disable_collection",
            "public_indexing_enabled": False,
            "google_analytics_key": "",
            "cookies_bar": None,
            "block_third_party_domains": None,
        }
        apply_website_settings(self.env, {"instance": "prod", "website_settings": settings}, instance="prod")
        self.assertFalse(website.google_analytics_key)
        self.assertFalse(website.cookies_bar)
        self.assertTrue(website.block_third_party_domains)
        self.assertEqual(read_string_parameter(self.env["ir.config_parameter"].sudo(), INDEXING_PARAMETER), "0")

    def test_native_readback_refuses_a_planted_noop_write(self) -> None:
        website = self.env["website"].create({"name": "Synthetic fault", "domain": "fault.example.test"})
        settings = {
            "operation": "apply",
            "website_id": website.id,
            "public_origin": "https://fault.example.test",
            "public_indexing_enabled": True,
            "google_analytics_key": "G-SYNTHETIC",
            "cookies_bar": True,
            "block_third_party_domains": True,
        }
        with patch.object(type(website), "write", return_value=True), self.assertRaises(RuntimeError):
            apply_website_settings(self.env, {"instance": "prod", "website_settings": settings}, instance="prod")
        print("DEVKIT_FAULT_DETECTED native_website_noop_write", flush=True)
