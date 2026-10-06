import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from odoo_devkit.addon_ci import command, discover_addons, fetch_source, test_result


class AddonCiTests(unittest.TestCase):
    def test_source_auth_is_ephemeral_to_git_and_absent_from_other_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"ADDON_CI_SOURCE_TOKEN": "synthetic-read-token"}):
            subprocess.run(["git", "init", "--quiet", temporary], check=True)
            header = fetch_source(["git", "-C", temporary, "config", "--get", "http.https://github.com/.extraheader"], capture=True)
            authorization = header.stdout.strip().removeprefix("AUTHORIZATION: basic ")
            self.assertEqual(base64.b64decode(authorization).decode().split(":", 1)[1], os.environ["ADDON_CI_SOURCE_TOKEN"])
            local_header = subprocess.run(
                ["git", "-C", temporary, "config", "--local", "--get", "http.https://github.com/.extraheader"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(local_header.returncode, 1)
            child = command(
                ["uv", "run", "--no-project", "python", "-c", "import os; print('ADDON_CI_SOURCE_TOKEN' in os.environ)"],
                capture=True,
            )
            self.assertEqual(child.stdout.strip(), "False")

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
