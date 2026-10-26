from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import local_runtime
from odoo_devkit.cli import build_parser
from tests.git_fixture import commit_fixture_repository, init_fixture_repository


class SourceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tenant = self.root / "tenant"
        init_fixture_repository(self.tenant)
        self.manifest = self.tenant / "workspace.toml"
        self.manifest.write_text(
            """schema_version = 1
tenant = "fixture"
[workspace]
name = "fixture"
python = "3.13"
[runtime]
context = "fixture"
instance = "local"
database = "fixture"
addons_paths = ["addons"]
[ide]
mode = "tenant"
focus_paths = ["addons"]
attached_paths = []
[repos.tenant]
name = "tenant"
path = "."
repository = "example/tenant"
[build]
odoo_version = "19.0"
base_runtime_image = "registry.invalid/private/runtime:19.0"
base_devtools_image = "registry.invalid/private/devtools:19.0"
""",
            encoding="utf-8",
        )
        (self.tenant / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nversion = "0.0.0"\nrequires-python = ">=3.13"\n'
            "dependencies = []\n[tool.uv]\npackage = false\n[tool.uv.workspace]\nmembers = []\n",
            encoding="utf-8",
        )
        subprocess.run(["uv", "lock", "--offline", "--no-config"], cwd=self.tenant, check=True, capture_output=True)
        self.inputs = self.tenant / "artifact-inputs.toml"
        self.inputs.write_text(
            'schema_version = 1\nsources = [{repository = "example/private-addon", selector = "main"}]\n',
            encoding="utf-8",
        )
        self.addons = self.tenant / "addons" / "fixture"
        self.addons.mkdir(parents=True)
        (self.addons / "__manifest__.py").write_text('{"name": "Fixture"}\n', encoding="utf-8")
        (self.addons / "__init__.py").write_text('raise RuntimeError("must never execute")\n', encoding="utf-8")
        (self.addons / "views.xml").write_text("<odoo/>\n", encoding="utf-8")
        commit_fixture_repository(self.tenant)

    def check_source(self) -> dict[str, object]:
        arguments = build_parser().parse_args(["workspace", "check-source", "--manifest", str(self.manifest)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            arguments.handler(arguments)
        return json.loads(output.getvalue())

    def test_source_only_checks_are_offline_and_cannot_publish(self) -> None:
        original_run = subprocess.run
        commands: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[0] == "git":
                self.assertNotIn(command[1], {"fetch", "clone", "ls-remote", "push", "pull"})
            else:
                self.assertEqual(command[0], "uv")
                self.assertTrue("--offline" in command or kwargs.get("env", {}).get("UV_OFFLINE") == "1")
            return original_run(command, **kwargs)

        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch.object(local_runtime, "publish_runtime_artifact", side_effect=AssertionError("publish called")),
            mock.patch.object(local_runtime, "resolve_source_github_token", side_effect=AssertionError("source auth called")),
            mock.patch.object(local_runtime, "resolve_ghcr_read_token", side_effect=AssertionError("registry auth called")),
        ):
            result = self.check_source()

        self.assertTrue(result["full_build_required"])
        self.assertTrue(result["dependencies"]["publishable"])
        self.assertGreater(result["syntax_files"], 0)
        self.assertTrue(any(command[:2] == ["uv", "export"] for command in commands))
        self.assertFalse((self.tenant / "artifact-manifest.json").exists())
        self.assertEqual(
            subprocess.run(["git", "status", "--porcelain"], cwd=self.tenant, check=True, capture_output=True, text=True).stdout,
            "",
        )

    def test_invalid_source_syntax_fails_without_importing(self) -> None:
        for filename, content in (("broken.py", "def broken(:\n"), ("broken.xml", "<odoo>")):
            with self.subTest(filename=filename):
                path = self.addons / filename
                path.write_text(content, encoding="utf-8")
                commit_fixture_repository(self.tenant)
                with self.assertRaisesRegex(SystemExit, filename):
                    self.check_source()
                path.unlink()
                commit_fixture_repository(self.tenant)

    def test_stale_or_missing_locks_fail(self) -> None:
        project = self.tenant / "pyproject.toml"
        project.write_text(project.read_text().replace("dependencies = []", 'dependencies = ["missing-fixture-package"]'))
        commit_fixture_repository(self.tenant)
        with self.assertRaises(SystemExit):
            self.check_source()
        (self.tenant / "uv.lock").unlink()
        commit_fixture_repository(self.tenant)
        with self.assertRaises(SystemExit):
            self.check_source()

    def test_incomplete_build_and_invalid_source_declarations_fail(self) -> None:
        original_manifest = self.manifest.read_text()
        self.manifest.write_text(original_manifest.replace('base_devtools_image = "registry.invalid/private/devtools:19.0"', ""))
        with self.assertRaises(SystemExit):
            self.check_source()
        self.manifest.write_text(original_manifest)
        for declaration in (
            '{repository = "example/private-addon", selector = "main", exact_ref = "bad"}',
            '{repository = "example/private-addon", exact_ref = "bad"}',
            '{repository = "example/private-addon", selector = "abc1234"}',
            '{repository = "example/private-addon", selector = "bad ref"}',
        ):
            self.inputs.write_text(f"schema_version = 1\nsources = [{declaration}]\n")
            with self.subTest(declaration=declaration), self.assertRaises(SystemExit):
                self.check_source()

    def test_source_symlinks_are_not_read(self) -> None:
        external = self.root / "outside.py"
        external.write_text("raise RuntimeError()\n")
        (self.addons / "linked.py").symlink_to(external)
        commit_fixture_repository(self.tenant)
        with self.assertRaises(SystemExit):
            self.check_source()


if __name__ == "__main__":
    unittest.main()
