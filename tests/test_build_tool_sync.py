from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import odoo_devkit.build_tool_sync as build_tool_sync_module
from odoo_devkit.build_tool_sync import BuildToolSyncError, apply_build_tool_sync, plan_build_tool_sync


class BuildToolSyncTest(unittest.TestCase):
    def test_plan_updates_workspace_refs_and_managed_addon_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))

            plan = plan_build_tool_sync(
                tenant_root=tenant_root,
                devkit_root=devkit_root,
                devkit_ref=devkit_ref,
            )

            self.assertTrue(plan.changed)
            self.assertEqual(3, len(plan.changes))
            self.assertEqual("1.32.0", plan.catalog["hatchling"])
            rendered_addon = plan.rendered_files[plan.tenant_root / "addons" / "example" / "pyproject.toml"]
            self.assertIn('requires = ["hatchling==1.32.0", "tenant-builder==2.0.0"]', rendered_addon)
            self.assertIn("# preserved", rendered_addon)
            rendered_workspace = plan.rendered_files[plan.tenant_root / "workspace.toml"]
            self.assertEqual(2, rendered_workspace.count(f'ref = "{devkit_ref}"'))

    def test_apply_is_atomic_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            plan = plan_build_tool_sync(
                tenant_root=tenant_root,
                devkit_root=devkit_root,
                devkit_ref=devkit_ref,
            )
            originals = {path: path.read_bytes() for path in plan.rendered_files}

            with (
                mock.patch("odoo_devkit.build_tool_sync._validate_applied_plan", side_effect=BuildToolSyncError("boom")),
                self.assertRaisesRegex(BuildToolSyncError, "boom"),
            ):
                apply_build_tool_sync(plan)

            self.assertEqual(originals, {path: path.read_bytes() for path in plan.rendered_files})

    def test_apply_validates_and_writes_all_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            plan = plan_build_tool_sync(
                tenant_root=tenant_root,
                devkit_root=devkit_root,
                devkit_ref=devkit_ref,
            )

            apply_build_tool_sync(plan)

            self.assertIn("hatchling==1.32.0", (tenant_root / "addons" / "example" / "pyproject.toml").read_text())
            self.assertEqual(2, (tenant_root / "workspace.toml").read_text().count(f'ref = "{devkit_ref}"'))

    def test_apply_reports_incomplete_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)
            real_atomic_write = build_tool_sync_module._atomic_write_bytes
            call_count = 0

            def fail_first_rollback(*, path: Path, content: bytes) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == len(plan.rendered_files) + 1:
                    raise OSError("restore failed")
                real_atomic_write(path=path, content=content)

            with (
                mock.patch("odoo_devkit.build_tool_sync._validate_applied_plan", side_effect=BuildToolSyncError("boom")),
                mock.patch("odoo_devkit.build_tool_sync._atomic_write_bytes", side_effect=fail_first_rollback),
                self.assertRaisesRegex(BuildToolSyncError, "rollback was incomplete"),
            ):
                apply_build_tool_sync(plan)

    def test_plan_rejects_non_exact_managed_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            addon_path = tenant_root / "addons" / "example" / "pyproject.toml"
            addon_path.write_text(addon_path.read_text().replace("hatchling==1.31.0", "hatchling>=1.31.0"))

            with self.assertRaisesRegex(BuildToolSyncError, "must use an exact version"):
                plan_build_tool_sync(
                    tenant_root=tenant_root,
                    devkit_root=devkit_root,
                    devkit_ref=devkit_ref,
                )

    def test_plan_rejects_non_commit_devkit_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, _devkit_ref = self._write_fixture(Path(temporary_directory))

            with self.assertRaisesRegex(BuildToolSyncError, "40-character Git commit"):
                plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref="main")

    def test_plan_rejects_mismatched_devkit_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, _devkit_ref = self._write_fixture(Path(temporary_directory))

            with self.assertRaisesRegex(BuildToolSyncError, "does not match requested ref"):
                plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref="d" * 40)

    def test_plan_rejects_dirty_devkit_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            catalog_path = devkit_root / "docker" / "runtime-python" / "pyproject.toml"
            catalog_path.write_text(catalog_path.read_text().replace("1.32.0", "1.33.0"))

            with self.assertRaisesRegex(BuildToolSyncError, "uncommitted changes"):
                plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

    def test_plan_rejects_untracked_devkit_catalog_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            subprocess.run(
                ["git", "rm", "--cached", "--quiet", "docker/runtime-python/uv.lock"],
                cwd=devkit_root,
                check=True,
            )

            with self.assertRaisesRegex(BuildToolSyncError, "must be tracked"):
                plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

    def test_plan_ignores_ambient_git_repository_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            tenant_root, devkit_root, devkit_ref = self._write_fixture(root)

            with mock.patch.dict(os.environ, {"GIT_DIR": str(root / "wrong-repository")}, clear=False):
                plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertTrue(plan.build_tool_changed)

    def test_ref_only_plan_is_not_build_tool_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            addon_path = tenant_root / "addons" / "example" / "pyproject.toml"
            addon_path.write_text(addon_path.read_text().replace("hatchling==1.31.0", "hatchling==1.32.0"))

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertTrue(plan.changed)
            self.assertFalse(plan.build_tool_changed)

    @staticmethod
    def _write_fixture(root: Path) -> tuple[Path, Path, str]:
        tenant_root = root / "tenant"
        devkit_root = root / "devkit"
        (tenant_root / "addons" / "example").mkdir(parents=True)
        (devkit_root / "docker" / "runtime-python").mkdir(parents=True)
        (tenant_root / "workspace.toml").write_text(
            '[repos.devkit]\nname = "odoo-devkit"\npath = "../odoo-devkit"\nref = "main"\n\n'
            '[repos.runtime]\nname = "odoo-devkit"\npath = "../odoo-devkit"\nref = "main"\n'
        )
        (tenant_root / "pyproject.toml").write_text('[project]\nname = "tenant"\nversion = "0.0.0"\ndependencies = []\n')
        (tenant_root / "addons" / "example" / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["hatchling==1.31.0", "tenant-builder==2.0.0"]\n'
            'build-backend = "hatchling.build"\n\n[project]\nname = "example"\nversion = "0.0.0"\n'
            "dependencies = []  # preserved\n"
        )
        (devkit_root / "docker" / "runtime-python" / "pyproject.toml").write_text(
            '[project]\nname = "runtime"\nversion = "0.0.0"\n'
            'dependencies = ["hatchling==1.32.0", "tenant-builder==2.0.0", "passlib>=1.7.4"]\n'
        )
        (devkit_root / "docker" / "runtime-python" / "uv.lock").write_text("version = 1\n")
        subprocess.run(
            ["uv", "lock", "--offline", "--no-config"],
            cwd=tenant_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "init", "--quiet"], cwd=devkit_root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=devkit_root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=devkit_root, check=True)
        subprocess.run(["git", "add", "docker/runtime-python"], cwd=devkit_root, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=devkit_root, check=True)
        devkit_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=devkit_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return tenant_root, devkit_root, devkit_ref


if __name__ == "__main__":
    unittest.main()
