from __future__ import annotations

import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


def _load_bootstrap_module() -> types.ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "docker" / "scripts" / "odoo_website_bootstrap.py"
    spec = importlib.util.spec_from_file_location("odoo_devkit_website_bootstrap_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


website_bootstrap = _load_bootstrap_module()


class FakeRecord:
    def __init__(
        self,
        *,
        record_id: int = 1,
        fields: tuple[str, ...] = (),
        truthy: bool = True,
        values: dict[str, object] | None = None,
    ) -> None:
        self.id = record_id
        self._fields = set(fields)
        self.writes: list[dict[str, object]] = []
        self.truthy = truthy
        self.persist_writes = True
        self.ignored_write_fields: set[str] = set()
        for field_name in self._fields:
            setattr(self, field_name, None)
        for field_name, value in (values or {}).items():
            setattr(self, field_name, value)

    def __bool__(self) -> bool:
        return self.truthy

    def sudo(self) -> FakeRecord:
        return self

    @property
    def _name(self) -> str:
        return str(getattr(self, "model_name", ""))

    def write(self, values: dict[str, object]) -> None:
        self.writes.append(values)
        if not self.persist_writes:
            return
        for field_name, value in values.items():
            if field_name in self.ignored_write_fields:
                continue
            setattr(self, field_name, value)


class FakeModel:
    def __init__(
        self, *, record: FakeRecord | None = None, fields: tuple[str, ...] = (), records: list[FakeRecord] | None = None
    ) -> None:
        self.record = record if record is not None else FakeRecord(truthy=False)
        self._fields = set(fields)
        self.records = records

    def sudo(self) -> FakeModel:
        return self

    def search(self, *unused_args: object, **unused_kwargs: object) -> FakeRecord:
        if self.records is not None:
            return self.records[0] if self.records else FakeRecord(truthy=False)
        return self.record

    def create(self, values: dict[str, object]) -> FakeRecord:
        self.record = FakeRecord(fields=tuple(self._fields))
        self.record.write(values)
        return self.record


class FakeConfigParameter:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.persist_writes = True

    def sudo(self) -> FakeConfigParameter:
        return self

    def set_param(self, key: str, value: str) -> None:
        if not self.persist_writes:
            return
        self.values[key] = value

    def get_param(self, key: str) -> str | None:
        return self.values.get(key)


class FakeEnv:
    def __init__(self) -> None:
        self.website = FakeRecord(fields=("name", "domain", "homepage_id", "homepage_url", "logo"))
        self.config_parameter = FakeConfigParameter()
        self.modules = FakeModel(record=FakeRecord(fields=(), truthy=True))
        self.pages = FakeModel(record=FakeRecord(fields=(), truthy=False))
        self.langs = FakeModel(record=FakeRecord(fields=(), truthy=False))
        self.refs: dict[str, FakeRecord] = {}
        self.registry = {"website": object()}

    def __getitem__(self, model_name: str) -> Any:
        return {
            "website": FakeModel(record=self.website, fields=("name", "domain", "homepage_id", "homepage_url", "logo")),
            "ir.config_parameter": self.config_parameter,
            "ir.module.module": self.modules,
            "website.page": self.pages,
            "res.lang": self.langs,
            "ir.http": FakeModel(record=FakeRecord()),
        }[model_name]

    def ref(self, xmlid: str, *unused_args: object, **unused_kwargs: object) -> FakeRecord | None:
        return self.refs.get(xmlid)


class WebsiteBootstrapHelperTests(unittest.TestCase):
    def test_controller_homepage_route_persists_homepage_url_and_clears_stale_page_homepage(self) -> None:
        env = FakeEnv()
        payload = {
            "website_bootstrap": {
                "name": "OPW",
                "canonical_url": "https://opw-testing.example.com",
                "homepage_url": "/shop",
                "routes_source": {"module": "website_sale"},
                "routes": [
                    {
                        "name": "Shop",
                        "url": "/shop",
                        "module": "website_sale",
                        "published": True,
                        "homepage": True,
                    }
                ],
            }
        }

        website_bootstrap.apply_website_bootstrap(env, payload)

        self.assertIn({"homepage_url": "/shop", "homepage_id": False}, env.website.writes)
        self.assertEqual(env.config_parameter.values["web.base.url"], "https://opw-testing.example.com")
        self.assertEqual(env.config_parameter.values["web.base.url.freeze"], "True")
        self.assertIn({"name": "OPW", "domain": "opw-testing.example.com"}, env.website.writes)

    def test_config_parameter_web_base_url_supplies_canonical_when_bootstrap_payload_omits_it(self) -> None:
        env = FakeEnv()
        payload = {
            "config_parameters": [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://cm-website-testing.example.com"},
                }
            ],
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "homepage_url": "/cell-mechanic",
                "routes_source": {"module": "cm_website"},
            },
        }

        website_bootstrap.apply_website_bootstrap(env, payload)

        self.assertEqual(env.config_parameter.values["web.base.url"], "https://cm-website-testing.example.com")
        self.assertEqual(env.website.domain, "cm-website-testing.example.com")
        self.assertEqual(env.website.name, "Cell Mechanic")

    def test_page_backed_homepage_requires_primary_page_and_persists_it(self) -> None:
        env = FakeEnv()
        page = FakeRecord(
            record_id=42,
            fields=("is_published", "website_published", "website_id"),
            values={"model_name": "website.page"},
        )
        env.refs["cm_website.website_page_cell_mechanic"] = page
        payload = {
            "config_parameters": [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://cm-website-testing.example.com"},
                }
            ],
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "homepage_url": "/cell-mechanic",
                "primary_page_xmlid": "cm_website.website_page_cell_mechanic",
                "routes_source": {"module": "cm_website"},
            },
        }

        website_bootstrap.apply_website_bootstrap(env, payload)

        self.assertEqual(env.website.homepage_id, page.id)
        self.assertEqual(env.website.homepage_url, "/cell-mechanic")
        self.assertIn({"is_published": True, "website_published": True, "website_id": 1}, page.writes)

    def test_missing_primary_page_fails_before_delegating_to_installed_module(self) -> None:
        env = FakeEnv()
        payload = {
            "config_parameters": [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://cm-website-testing.example.com"},
                }
            ],
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "homepage_url": "/cell-mechanic",
                "primary_page_xmlid": "cm_website.website_page_cell_mechanic",
                "routes_source": {"module": "cm_website"},
            },
        }

        with self.assertRaisesRegex(RuntimeError, "primary page XML ID not found"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_bad_primary_page_xmlid_fails_even_when_url_fallback_page_exists(self) -> None:
        env = FakeEnv()
        env.pages = FakeModel(
            record=FakeRecord(
                record_id=43,
                fields=("is_published", "website_published", "website_id"),
                values={"model_name": "website.page"},
            ),
            fields=("website_id",),
        )
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
                "homepage_url": "/cell-mechanic",
                "primary_page_xmlid": "cm_website.bad_page_xmlid",
            },
        }

        with self.assertRaisesRegex(RuntimeError, "primary page XML ID not found"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_route_homepage_readback_reports_final_route_homepage(self) -> None:
        env = FakeEnv()
        route_page = FakeRecord(
            record_id=44,
            fields=("is_published", "website_published", "website_id"),
            values={"model_name": "website.page"},
        )
        env.pages = FakeModel(record=route_page, fields=("website_id",))
        payload = {
            "website_bootstrap": {
                "name": "OPW",
                "canonical_url": "https://opw-testing.example.com",
                "routes": [
                    {
                        "name": "Shop",
                        "url": "/shop",
                        "published": True,
                        "homepage": True,
                    }
                ],
            }
        }

        output = io.StringIO()
        with redirect_stdout(output):
            website_bootstrap.apply_website_bootstrap(env, payload)

        self.assertEqual(env.website.homepage_id, route_page.id)
        self.assertEqual(env.website.homepage_url, "/shop")
        self.assertIn("website_bootstrap_homepage_url_matches=true", output.getvalue())
        self.assertIn("website_bootstrap_homepage_matches_page=true", output.getvalue())

    def test_logo_readback_mismatch_fails_before_success_marker(self) -> None:
        env = FakeEnv()
        env.website.logo = "existing-logo"
        env.website.ignored_write_fields.add("logo")
        logo_path = Path(__file__)
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
                "logo_path": str(logo_path),
            }
        }

        with self.assertRaisesRegex(RuntimeError, "failed to persist website logo"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_homepage_url_without_page_or_module_fails_when_route_is_not_verifiable(self) -> None:
        env = FakeEnv()
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
                "homepage_url": "/cell-mechanic",
            }
        }

        with self.assertRaisesRegex(RuntimeError, "route '/cell-mechanic' is not verifiable"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_missing_visible_website_fields_fail_before_success_marker(self) -> None:
        env = FakeEnv()
        env.website = FakeRecord(fields=("homepage_id", "homepage_url"))
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
            }
        }

        with self.assertRaisesRegex(RuntimeError, "missing fields: name"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_config_parameter_readback_mismatch_fails_before_success_marker(self) -> None:
        env = FakeEnv()
        env.config_parameter.persist_writes = False
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
            }
        }

        with self.assertRaisesRegex(RuntimeError, "failed to persist config parameter 'web.base.url'"):
            website_bootstrap.apply_website_bootstrap(env, payload)

    def test_website_field_readback_mismatch_fails_before_success_marker(self) -> None:
        env = FakeEnv()
        env.website.persist_writes = False
        payload = {
            "website_bootstrap": {
                "name": "Cell Mechanic",
                "canonical_url": "https://cm-website-testing.example.com",
            }
        }

        with self.assertRaisesRegex(RuntimeError, "failed to persist website name"):
            website_bootstrap.apply_website_bootstrap(env, payload)


if __name__ == "__main__":
    unittest.main()
