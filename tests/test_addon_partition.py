from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from odoo_devkit.addon_shards.inventory import Inventory, read_inventory, split_classes
from odoo_devkit.addon_shards.runner import ParallelRun
from odoo_devkit.addon_shards.suite import select_worker_suite


class AddonPartitionTests(unittest.TestCase):
    def test_reopening_clone_never_repeats_at_install(self) -> None:
        module = ModuleType("odoo.tests.loader")
        module.make_suite = lambda names, position: list(names)
        select_worker_suite(module)
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

    def test_four_workers_launch_with_isolated_databases_files_and_ports(self) -> None:
        methods = [f"example.tests.Case{i}.test_native" for i in range(7)]
        expected = Inventory(
            post_install=methods, class_seconds=Counter({name.rsplit(".", 1)[0]: i for i, name in enumerate(methods)})
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            (data / "filestore/ci").mkdir(parents=True)
            (data / "filestore/ci/attachment").write_bytes(b"seed")
            args = [
                "--database",
                "ci",
                "--data-dir",
                str(data),
                "--limit-time-real",
                "60",
                "--db_host",
                "db",
                "--db_user",
                "odoo",
                "--init",
                "example",
            ]
            run = ParallelRun(args, expected, [], split_classes(expected, 4), root)
            ports = set()
            databases = set()
            temp_dirs = set()

            def launch(argv: list[str], *, env: dict[str, str], **_: object) -> Mock:
                part = env["ADDON_TEST_PART"]
                if part != "seed":
                    self.assertNotIn("--init", argv)
                    db = argv[argv.index("--database") + 1]
                    self.assertNotEqual(db, run.seed)
                    databases.add(db)
                    ports.add(argv[argv.index("--http-port") + 1])
                    temp_dirs.add(env["TMPDIR"])
                    clone = Path(argv[argv.index("--data-dir") + 1]) / "filestore" / db / "attachment"
                    self.assertEqual(clone.read_bytes(), b"seed")
                    clone.write_text(part)
                process = Mock()
                process.poll.return_value = process.wait.return_value = 0
                return process

            with (
                patch.object(subprocess, "Popen", side_effect=launch),
                patch.object(run, "validate"),
                patch.object(run, "postgres") as postgres,
                patch.object(run, "query", return_value=["postgres", "ci"]),
                patch("odoo_devkit.addon_shards.runner.EVIDENCE", root),
            ):
                self.assertEqual(run.run(), len(methods))
            self.assertEqual(len(databases), 4)
            self.assertEqual(len(ports), len(databases))
            self.assertEqual(len(temp_dirs), len(databases))
            self.assertCountEqual([method for group in run.groups for method in group], methods)
            self.assertEqual((data / "filestore/ci/attachment").read_bytes(), b"seed")
            for database in databases:
                postgres.assert_any_call("createdb", "-T", "ci", database)
                postgres.assert_any_call("dropdb", "--force", database)
        with self.assertRaises(ValueError):
            split_classes(Inventory(post_install=methods[:1], class_seconds=Counter({methods[0].rsplit(".", 1)[0]: 1})), 4)
