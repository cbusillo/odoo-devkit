from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import odoo_devkit.build_tool_sync as build_tool_sync_module
from odoo_devkit.build_tool_sync import BuildToolSyncError, apply_build_tool_sync, plan_build_tool_sync


class BuildToolSyncTest(unittest.TestCase):
    def test_plan_updates_workspace_refs_and_managed_addon_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))

            expected_requirements = self._expected_requirements(tenant_root, devkit_root)
            plan = plan_build_tool_sync(
                tenant_root=tenant_root,
                devkit_root=devkit_root,
                devkit_ref=devkit_ref,
            )

            self.assertTrue(plan.changed)
            self.assertEqual(3, len(plan.changes))
            rendered_addon = plan.rendered_files[plan.tenant_root / "addons" / "example" / "pyproject.toml"]
            self.assertEqual(tomllib.loads(rendered_addon)["build-system"]["requires"], expected_requirements)
            self.assertIn("# preserved", rendered_addon)
            rendered_workspace = plan.rendered_files[plan.tenant_root / "workspace.toml"]
            repositories = tomllib.loads(rendered_workspace)["repos"]
            self.assertEqual(repositories["devkit"]["ref"], devkit_ref)
            self.assertEqual(repositories["runtime"]["ref"], devkit_ref)

    def test_plan_preserves_unmanaged_requirements_and_addon_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(directory))
            addon_path = tenant_root / "addons" / "example" / "pyproject.toml"
            original_text = addon_path.read_text(encoding="utf-8")
            requirements = tomllib.loads(original_text)["build-system"]["requires"]
            changed_requirements = ["external-builder==3.4.5", *reversed(requirements)]
            addon_path.write_text(
                original_text.replace(json.dumps(requirements), json.dumps(changed_requirements)), encoding="utf-8"
            )
            self.assertEqual(tomllib.loads(addon_path.read_text())["build-system"]["requires"], changed_requirements)
            expected = self._expected_requirements(tenant_root, devkit_root)

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertEqual(
                tomllib.loads(plan.rendered_files[plan.tenant_root / "addons" / "example" / "pyproject.toml"])["build-system"][
                    "requires"
                ],
                expected,
            )

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
            subprocess.run(["uv", "lock", "--offline", "--no-config"], cwd=tenant_root, check=True, capture_output=True, text=True)
            expected_requirements = self._expected_requirements(tenant_root, devkit_root)
            plan = plan_build_tool_sync(
                tenant_root=tenant_root,
                devkit_root=devkit_root,
                devkit_ref=devkit_ref,
            )

            apply_build_tool_sync(plan)

            addon = tomllib.loads((tenant_root / "addons" / "example" / "pyproject.toml").read_text())
            self.assertEqual(addon["build-system"]["requires"], expected_requirements)
            repositories = tomllib.loads((tenant_root / "workspace.toml").read_text())["repos"]
            self.assertEqual(repositories["devkit"]["ref"], devkit_ref)
            self.assertEqual(repositories["runtime"]["ref"], devkit_ref)

    def test_apply_reports_incomplete_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)
            real_atomic_write = build_tool_sync_module.atomic_write_bytes
            call_count = 0

            def fail_first_rollback(*, path: Path, content: bytes) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == len(plan.rendered_files) + 1:
                    raise OSError("restore failed")
                real_atomic_write(path=path, content=content)

            with (
                mock.patch("odoo_devkit.build_tool_sync._validate_applied_plan", side_effect=BuildToolSyncError("boom")),
                mock.patch("odoo_devkit.build_tool_sync.atomic_write_bytes", side_effect=fail_first_rollback),
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

    def test_plan_reports_tenant_lock_drift_from_support_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            support_packages = {"shared-core": "2.0.0", "support-only": "1.0.0"}
            tenant_root, devkit_root, devkit_ref = self._write_fixture(
                Path(temporary_directory), support_lock_packages=support_packages
            )
            tenant_packages = {"shared-core": "1.9.0", "tenant-only": "3.0.0"}
            self._write_lock(tenant_root, "tenant", tenant_packages)
            addon_path = tenant_root / "addons" / "example" / "pyproject.toml"
            addon_path.write_text(addon_path.read_text().replace("hatchling==1.31.0", "hatchling==1.32.0"))

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertTrue(plan.build_tool_changed)
            self.assertEqual(plan.support_lock_pins, {"shared-core": support_packages["shared-core"]})
            lock_changes = [change for change in plan.changes if change.path == "uv.lock"]
            self.assertEqual(
                [(change.before, change.after) for change in lock_changes],
                [(f"shared-core=={tenant_packages['shared-core']}", f"shared-core=={support_packages['shared-core']}")],
            )

    def test_plan_ignores_support_packages_the_tenant_locks_only_for_development(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            support_packages = {"shared-core": "2.0.0"}
            tenant_root, devkit_root, devkit_ref = self._write_fixture(
                Path(temporary_directory), support_lock_packages=support_packages
            )
            self._write_lock(tenant_root, "tenant", {}, dev_packages={"shared-core": "1.9.0"})

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertEqual(plan.support_lock_pins, {})
            self.assertEqual([change for change in plan.changes if change.path == "uv.lock"], [])

    def test_plan_reports_same_version_from_a_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            support_packages = {"shared-core": "2.0.0"}
            tenant_root, devkit_root, devkit_ref = self._write_fixture(
                Path(temporary_directory), support_lock_packages=support_packages
            )
            self._write_lock(tenant_root, "tenant", {}, git_packages=support_packages)

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertEqual(plan.support_lock_pins, support_packages)

    def test_plan_accepts_tenant_lock_matching_support_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            support_packages = {"shared-core": "2.0.0"}
            tenant_root, devkit_root, devkit_ref = self._write_fixture(
                Path(temporary_directory), support_lock_packages=support_packages
            )
            self._write_lock(tenant_root, "tenant", support_packages)
            addon_path = tenant_root / "addons" / "example" / "pyproject.toml"
            addon_path.write_text(addon_path.read_text().replace("hatchling==1.31.0", "hatchling==1.32.0"))

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)

            self.assertFalse(plan.build_tool_changed)
            self.assertEqual(plan.support_lock_pins, {})

    def test_apply_restores_tenant_lock_when_alignment_leaves_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            support_packages = {"shared-core": "2.0.0"}
            tenant_root, devkit_root, devkit_ref = self._write_fixture(
                Path(temporary_directory), support_lock_packages=support_packages
            )
            self._write_lock(tenant_root, "tenant", {"shared-core": "1.9.0"})
            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)
            tenant_lock_path = tenant_root / "uv.lock"
            originals = {path: path.read_bytes() for path in (*plan.rendered_files, tenant_lock_path)}

            def unrelated_relock(*, tenant_root: Path, pins: dict[str, str]) -> None:
                tenant_lock_path.write_text(tenant_lock_path.read_text() + "\n")

            with (
                mock.patch("odoo_devkit.build_tool_sync._relock_tenant_support_packages", side_effect=unrelated_relock) as relock,
                self.assertRaisesRegex(BuildToolSyncError, "still differs from the devkit support lock"),
            ):
                apply_build_tool_sync(plan)

            relock.assert_called_once_with(tenant_root=plan.tenant_root, pins=plan.support_lock_pins)
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_apply_moves_devkit_workflow_pins_with_workspace_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tenant_root, devkit_root, devkit_ref = self._write_fixture(Path(temporary_directory))
            devkit_repository = "example/odoo-devkit"
            workspace_path = tenant_root / "workspace.toml"
            workspace_path.write_text(
                workspace_path.read_text().replace("[repos.devkit]\n", f'[repos.devkit]\nrepository = "{devkit_repository}"\n')
            )
            previous_ref = "a" * 40
            unrelated_pins = [f"example/other-tool@{previous_ref}", f"{devkit_repository}/.github/actions/addon-tests@main"]
            workflow_path = tenant_root / ".github" / "workflows" / "addon-tests.yml"
            workflow_path.parent.mkdir(parents=True)
            workflow_path.write_text(
                "jobs:\n  test:\n    steps:\n"
                f"      - uses: {devkit_repository}/.github/actions/addon-tests@{previous_ref}\n"
                + "".join(f"      - uses: {pin}\n" for pin in unrelated_pins)
            )

            plan = plan_build_tool_sync(tenant_root=tenant_root, devkit_root=devkit_root, devkit_ref=devkit_ref)
            apply_build_tool_sync(plan)

            workflow_text = workflow_path.read_text()
            self.assertIn(f"uses: {devkit_repository}/.github/actions/addon-tests@{devkit_ref}\n", workflow_text)
            self.assertNotIn(f"{devkit_repository}/.github/actions/addon-tests@{previous_ref}", workflow_text)
            for pin in unrelated_pins:
                self.assertIn(f"uses: {pin}\n", workflow_text)
            workflow_changes = [change for change in plan.changes if change.path == ".github/workflows/addon-tests.yml"]
            self.assertEqual([(change.before, change.after) for change in workflow_changes], [(previous_ref, devkit_ref)])

    @staticmethod
    def _write_lock(
        project_root: Path,
        project_name: str,
        packages: dict[str, str],
        *,
        dev_packages: dict[str, str] | None = None,
        git_packages: dict[str, str] | None = None,
    ) -> None:
        dev_packages = dev_packages or {}
        git_packages = git_packages or {}
        runtime_names = [*packages, *git_packages]
        entries = []
        for name, version in sorted({**packages, **dev_packages}.items()):
            wheel = f"https://example.invalid/{name.replace('-', '_')}-{version}-py3-none-any.whl"
            entries.append(
                f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
                f'wheels = [{{ url = "{wheel}", hash = "sha256:{"0" * 64}" }}]\n'
            )
        for name, version in sorted(git_packages.items()):
            entries.append(
                f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
                f'source = {{ git = "https://example.invalid/{name}?rev=v{version}#{"1" * 40}" }}\n'
            )
        dependencies = ", ".join(f'{{ name = "{name}" }}' for name in runtime_names)
        development = ", ".join(f'{{ name = "{name}" }}' for name in dev_packages)
        entries.append(
            f'[[package]]\nname = "{project_name}"\nversion = "0.0.0"\nsource = {{ virtual = "." }}\n'
            f"dependencies = [{dependencies}]\n\n[package.dev-dependencies]\ndev = [{development}]\n"
        )
        (project_root / "uv.lock").write_text('version = 1\nrevision = 3\nrequires-python = ">=3.13"\n\n' + "\n".join(entries))

    @staticmethod
    def _expected_requirements(tenant_root: Path, devkit_root: Path) -> list[str]:
        original = tomllib.loads((tenant_root / "addons" / "example" / "pyproject.toml").read_text())["build-system"]["requires"]
        dependencies = tomllib.loads((devkit_root / "docker" / "runtime-python" / "pyproject.toml").read_text())["project"][
            "dependencies"
        ]
        catalog = dict(requirement.split("==", 1) for requirement in dependencies if "==" in requirement)
        expected = []
        for requirement in original:
            name, version = requirement.split("==", 1)
            expected.append(f"{name}=={catalog.get(name, version)}")
        return expected

    @staticmethod
    def _write_fixture(root: Path, *, support_lock_packages: dict[str, str] | None = None) -> tuple[Path, Path, str]:
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
        BuildToolSyncTest._write_lock(devkit_root / "docker" / "runtime-python", "runtime", support_lock_packages or {})
        subprocess.run(["uv", "lock", "--offline", "--no-config"], cwd=tenant_root, check=True, capture_output=True, text=True)
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
