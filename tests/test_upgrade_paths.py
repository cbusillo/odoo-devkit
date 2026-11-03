from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path

HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/odoo_upgrade_paths.py"))


class UpgradeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def checkout(self, name: str, marker: str | None = None) -> Path:
        checkout = self.root / "_checkouts" / name
        (checkout / "src/util").mkdir(parents=True)
        (checkout / "src/util/__init__.py").write_text("")
        checkout.with_suffix(".odoo-source").write_text(marker or HELPERS["UPGRADE_UTIL_SOURCE"])
        return checkout

    def test_exact_checkout_without_addon_manifests_is_selected_once(self) -> None:
        source = self.checkout("official") / "src"
        self.assertEqual(HELPERS["resolve_upgrade_util"]([self.root, self.root]), source.resolve())

    def test_missing_wrong_ref_duplicate_and_incomplete_sources_refuse(self) -> None:
        with self.assertRaises(ValueError):
            HELPERS["resolve_upgrade_util"]([self.root])
        checkout = self.checkout("wrong", "different/source@unqualified")
        with self.assertRaises(ValueError):
            HELPERS["resolve_upgrade_util"]([self.root])
        checkout.with_suffix(".odoo-source").unlink()
        checkout = self.checkout("official")
        second = self.checkout("duplicate")
        with self.assertRaises(ValueError):
            HELPERS["resolve_upgrade_util"]([self.root])
        second.with_suffix(".odoo-source").unlink()
        (checkout / "src/util/__init__.py").unlink()
        with self.assertRaises(ValueError):
            HELPERS["resolve_upgrade_util"]([self.root])

    def test_escaping_utility_source_refuses(self) -> None:
        checkout = self.checkout("official")
        target = self.root / "outside.py"
        target.write_text("")
        script = checkout / "src/util/__init__.py"
        script.unlink()
        script.symlink_to(target)
        with self.assertRaises(ValueError):
            HELPERS["resolve_upgrade_util"]([self.root])
