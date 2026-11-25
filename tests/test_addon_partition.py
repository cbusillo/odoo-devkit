from __future__ import annotations

import os
import unittest
from types import ModuleType
from unittest.mock import patch

from odoo_devkit.addon_shards.inventory import read_inventory, split_classes
from odoo_devkit.addon_shards.suite import ShardLoader


class AddonPartitionTests(unittest.TestCase):
    def test_reopening_clone_never_repeats_at_install(self) -> None:
        loader = ShardLoader("odoo.tests.loader", "unused")
        module = ModuleType("odoo.tests.loader")
        module.make_suite = lambda names, position: list(names)
        with patch("importlib.machinery.SourceFileLoader.exec_module"):
            loader.exec_module(module)
        for part, expected in (("seed", ["example"]), ("0", []), ("1", [])):
            with self.subTest(part=part), patch.dict(os.environ, ADDON_TEST_PART=part):
                self.assertEqual(module.make_suite(["example"], "at_install"), expected)

    def test_equal_counts_do_not_hide_a_browser_skip(self) -> None:
        native = "odoo.addons.example.tests.test_js"
        wrapper = f"{native}.WebSuite.test_js.browser"
        lines = [
            "odoo.service.server: Starting post tests",
            f"{native}: Starting WebSuite.test_js ...",
            f"{wrapper}: [HOOT] Starting test suites",
            f'{wrapper}: [HOOT] Running test "@example/works"',
            f"{wrapper}: [HOOT] Passed 1 test (2 assertions)",
            f"{wrapper}: [HOOT] Test suite succeeded",
            "odoo.service.server: 1 post-tests in 1s",
            "odoo.tests.result: 0 failed, 0 error(s) of 1 tests when loading database 'ci'",
        ]

        def render(messages: list[str]) -> str:
            return "\n".join(f"2026-01-01 00:00:00,000 1 INFO ci {message}" for message in messages)

        self.assertEqual(len(read_inventory(render(lines)).hoot), 1)
        with self.assertRaises(ValueError):
            read_inventory(render([*lines[:2], f"{native}: skipped WebSuite.test_js : no browser", *lines[-2:]]))
        with self.assertRaises(ValueError):
            read_inventory(render([line for line in lines if "suite succeeded" not in line]))

    def test_class_split_preserves_phase_and_methods(self) -> None:
        prefix = "odoo.addons.example.tests.test_cases"
        messages = [
            f"{prefix}: Starting Install.test_once ...",
            "odoo.service.server: Starting post tests",
            f"{prefix}: Starting Heavy.test_one ...",
            f"{prefix}: Starting Heavy.test_two ...",
            f"{prefix}: Starting Other.test_three ...",
            "odoo.service.server: 3 post-tests in 30s",
            "odoo.tests.result: 0 failed, 0 error(s) of 4 tests when loading database 'ci'",
        ]
        log = "\n".join(f"2026-01-01 00:00:{i:02d},000 1 INFO ci {line}" for i, line in enumerate(messages))
        inventory = read_inventory(log)
        first, second = split_classes(inventory)
        self.assertEqual(first, inventory.post_install[:2])
        self.assertEqual(second, inventory.post_install[2:])
        self.assertEqual(len(inventory.at_install), 1)
