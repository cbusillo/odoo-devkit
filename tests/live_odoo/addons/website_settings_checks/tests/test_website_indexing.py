import hashlib
import io
import json
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

from lxml import html
from odoo import tools
from odoo.tests import HttpCase, tagged
from odoo_website_settings import (
    INDEXING_PARAMETER,
    apply_website_settings,
    plan_website_indexing,
    read_string_parameter,
    write_string_parameter,
)


@tagged("post_install", "-at_install")
class TestNativeWebsiteIndexing(HttpCase):
    def test_public_noindex_preserves_routes_robots_and_unmanaged_head(self) -> None:
        self.enterContext(patch.dict(tools.config.options, {"proxy_mode": True}))
        original = '<meta name="robots" content="nofollow"><script>window.synthetic = "preserved";</script>'
        website = self.env["website"].create(
            {
                "name": "Synthetic indexing",
                "domain": "https://indexing.example.test",
                "custom_code_head": original,
                "robots_txt": "User-agent: *\nDisallow: /private-fixture\n",
                "google_analytics_key": "G-SYNTHETIC",
                "cookies_bar": True,
                "block_third_party_domains": True,
            }
        )
        other = self.env["website"].create(
            {
                "name": "Unrelated synthetic",
                "domain": "https://unrelated.example.test",
                "custom_code_head": original,
            }
        )
        payload: dict[str, Any] = {
            "instance": "prod",
            "website_indexing": {
                "website_id": website.id,
                "public_origin": website.domain,
                "production_use": "prelaunch",
            },
        }

        def page(host: str = "indexing.example.test", path: str = "/contactus") -> str:
            response = self.url_open(path, headers={"X-Forwarded-Host": host, "X-Forwarded-Proto": "https"})
            self.assertEqual(response.status_code, 200)
            return response.text

        def managed_count(document: str) -> int:
            return sum(
                "noindex" in content.lower().replace(",", " ").split()
                for content in html.fromstring(document).xpath('//head/meta[@name="robots"]/@content')
            )

        def apply() -> dict[str, object]:
            output = io.StringIO()
            with redirect_stdout(output):
                apply_website_settings(self.env, payload, instance="prod")
            return json.loads(output.getvalue().split("website_indexing=")[1].splitlines()[0])

        baseline = page()
        self.assertEqual(managed_count(baseline), 0)
        canonical = html.fromstring(baseline).xpath('//link[@rel="canonical"]/@href')
        self.assertTrue(canonical)
        robots = page(path="/robots.txt")
        self.assertNotIn("Disallow: /\n", robots)
        before = website.read(
            ["domain", "custom_code_head", "google_analytics_key", "cookies_bar", "block_third_party_domains", "robots_txt"]
        )
        plan = plan_website_indexing(self.env, payload, instance="prod")
        self.assertEqual(website.read(list(before[0])), before)
        receipt = apply()
        self.assertEqual(receipt["after_sha256"], plan["after_sha256"])
        self.env.cr.execute("SELECT custom_code_head FROM website WHERE id=%s", (website.id,))
        protected = self.env.cr.fetchone()[0]
        self.assertEqual(hashlib.sha256(protected.encode()).hexdigest(), receipt["after_sha256"])
        protected_page = page()
        self.assertEqual(managed_count(protected_page), 1)
        self.assertEqual(html.fromstring(protected_page).xpath('//head/meta[@content="nofollow"]/@name'), ["robots"])
        self.assertIn('window.synthetic = "preserved";', protected_page)
        self.assertEqual(html.fromstring(protected_page).xpath('//link[@rel="canonical"]/@href'), canonical)
        self.assertEqual(page(path="/robots.txt"), robots)
        self.assertEqual(managed_count(page("unrelated.example.test")), 0)
        self.assertEqual(other.custom_code_head, original)
        self.assertFalse(apply()["changed"])
        payload["website_indexing"]["production_use"] = "live"
        parameters = self.env["ir.config_parameter"].sudo()
        write_string_parameter(parameters, INDEXING_PARAMETER, "0")
        payload["website_settings"] = {
            "operation": "apply",
            "website_id": website.id,
            "public_origin": website.domain,
            "public_indexing_enabled": True,
            "google_analytics_key": "G-CHANGED",
            "cookies_bar": True,
            "block_third_party_domains": True,
        }
        native_write = type(website).write

        def ignore_head(records: Any, values: dict[str, object]) -> bool:
            if "custom_code_head" in values:
                return True
            return native_write(records, values)

        with patch.object(type(website), "write", ignore_head), self.assertRaises(RuntimeError):
            apply()
        self.assertEqual(website.custom_code_head, protected)
        self.assertEqual(website.google_analytics_key, before[0]["google_analytics_key"])
        self.assertEqual(read_string_parameter(parameters, INDEXING_PARAMETER), "0")
        del payload["website_settings"]
        self.assertEqual(managed_count(page()), 1)
        try:
            with self.env.cr.savepoint():
                apply()
                raise RuntimeError("synthetic release failure")
        except RuntimeError as error:
            self.assertEqual(str(error), "synthetic release failure")
        self.assertEqual(website.custom_code_head, protected)
        self.assertEqual(managed_count(page()), 1)
        apply()
        self.assertEqual(website.read(list(before[0])), before)
        self.assertEqual(managed_count(page()), 0)
        self.assertEqual(page(path="/robots.txt"), robots)
        payload["website_indexing"]["production_use"] = "prelaunch"
        apply()
        self.assertEqual(managed_count(page()), 1)
        duplicate = self.env["website"].create({"name": "Ambiguous synthetic", "domain": "http://indexing.example.test"})
        with self.assertRaises(RuntimeError):
            apply()
        duplicate.unlink()
        payload["website_indexing"]["public_origin"] = other.domain
        with self.assertRaises(RuntimeError):
            apply()
        self.assertEqual(website.custom_code_head, protected)
        print("NATIVE_INDEXING_HTTP_PROOF " + json.dumps(receipt, sort_keys=True), flush=True)
