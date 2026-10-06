import tempfile
import unittest
from pathlib import Path

from odoo_devkit.addon_ci import discover_addons, test_result


class AddonCiTests(unittest.TestCase):
    def test_install_selection_includes_addons_without_tests_and_excludes_uninstallable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, installable in (("tested", True), ("no_tests", True), ("retired", False)):
                addon = root / name
                addon.mkdir()
                (addon / "__manifest__.py").write_text(repr({"installable": installable}))
            self.assertEqual(discover_addons(root), ["no_tests", "tested"])

    def test_no_addons_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            discover_addons(Path(temporary))

    def test_only_completed_nonempty_success_passes(self) -> None:
        self.assertEqual(test_result("0 failed, 0 error(s) of 12 tests", 0)["tests"], 12)
        for log, exit_code in (
            ("1 failed, 0 error(s) of 12 tests", 0),
            ("0 failed, 1 error(s) of 12 tests", 0),
            ("0 failed, 0 error(s) of 0 tests", 0),
            ("0 failed, 0 error(s) of 12 tests", 1),
            ("Modules loaded", 0),
        ):
            with self.subTest(log=log, exit_code=exit_code), self.assertRaises(ValueError):
                test_result(log, exit_code)
