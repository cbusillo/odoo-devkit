import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from odoo_devkit import addon_ci
from odoo_devkit.addon_ci import command, discover_addons, fetch_source, test_result
from odoo_devkit.base_python import BasePythonCatalog
from odoo_devkit.dependency_workspace import derive_base_python_lock


class AddonCiTests(unittest.TestCase):
    def test_addon_image_uses_derived_frozen_inputs_and_keeps_source_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = "registry.invalid/base@sha256:" + "a" * 64
            catalog = BasePythonCatalog(image, "linux/amd64", ("idna==3.10",))
            fixture = root / "fixture"
            for name in ("runtime-python", "addon-tests"):
                source = fixture / "docker" / name
                source.mkdir(parents=True)
                (source / "pyproject.toml").write_text(
                    '[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.13"\n'
                    'dependencies=["idna>=3"]\n[tool.uv]\npackage=false\n'
                )
                subprocess.run(["uv", "lock", "--no-config"], cwd=source, check=True, capture_output=True)
            context = root / "context"
            context.mkdir()
            source = fixture / "docker/runtime-python"
            source_lock = (source / "uv.lock").read_bytes()
            reference = root / "reference"
            shutil.copytree(source, reference)
            derive_base_python_lock(staged_root=reference, catalogs=(catalog,), python_version=">=3.13")
            original_command = addon_ci.command
            docker_calls = []

            def execute(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["docker", "build"]:
                    docker_calls.append(args)
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:3] == ["uv", "python", "install"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                return original_command(args, **kwargs)

            with (
                patch.object(addon_ci, "ROOT", fixture),
                patch.object(addon_ci, "command", side_effect=execute),
                patch("odoo_devkit.addon_ci.read_base_python_catalogs", return_value=(catalog,)),
                patch("odoo_devkit.local_runtime.resolve_image_digest", return_value=image.split("@", 1)[1]),
            ):
                addon_ci.build_test_image("registry.invalid/base:mutable", None, context, "fixture:test")
            self.assertIn(f"BASE_IMAGE={image}", docker_calls[0])
            self.assertEqual((context / "runtime-python/uv.lock").read_bytes(), (reference / "uv.lock").read_bytes())
            self.assertIn(catalog.requirements[0], (context / "requirements.txt").read_text())
            self.assertEqual((source / "uv.lock").read_bytes(), source_lock)

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
