from __future__ import annotations

import importlib.util
import sys
import types
import unittest
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
    def __init__(self, *, record_id: int = 1, fields: tuple[str, ...] = (), truthy: bool = True) -> None:
        self.id = record_id
        self._fields = set(fields)
        self.writes: list[dict[str, object]] = []
        self.truthy = truthy

    def __bool__(self) -> bool:
        return self.truthy

    def sudo(self) -> FakeRecord:
        return self

    def write(self, values: dict[str, object]) -> None:
        self.writes.append(values)


class FakeModel:
    def __init__(self, *, record: FakeRecord | None = None, fields: tuple[str, ...] = ()) -> None:
        self.record = record if record is not None else FakeRecord(truthy=False)
        self._fields = set(fields)

    def sudo(self) -> FakeModel:
        return self

    def search(self, *unused_args: object, **unused_kwargs: object) -> FakeRecord:
        return self.record

    def create(self, values: dict[str, object]) -> FakeRecord:
        self.record = FakeRecord(fields=tuple(self._fields))
        self.record.write(values)
        return self.record


class FakeConfigParameter:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def sudo(self) -> FakeConfigParameter:
        return self

    def set_param(self, key: str, value: str) -> None:
        self.values[key] = value


class FakeEnv:
    def __init__(self) -> None:
        self.website = FakeRecord(fields=("name", "domain", "homepage_id", "homepage_url"))
        self.config_parameter = FakeConfigParameter()
        self.modules = FakeModel(record=FakeRecord(fields=(), truthy=True))
        self.pages = FakeModel(record=FakeRecord(fields=(), truthy=False))
        self.langs = FakeModel(record=FakeRecord(fields=(), truthy=False))
        self.registry = {"website": object()}

    def __getitem__(self, model_name: str) -> Any:
        return {
            "website": FakeModel(record=self.website, fields=("name", "domain", "homepage_id", "homepage_url")),
            "ir.config_parameter": self.config_parameter,
            "ir.module.module": self.modules,
            "website.page": self.pages,
            "res.lang": self.langs,
            "ir.http": FakeModel(record=FakeRecord()),
        }[model_name]

    @staticmethod
    def ref(*unused_args: object, **unused_kwargs: object) -> None:
        return None


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
        self.assertIn({"name": "OPW", "domain": "opw-testing.example.com"}, env.website.writes)


if __name__ == "__main__":
    unittest.main()
