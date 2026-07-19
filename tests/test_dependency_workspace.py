from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import dependency_workspace
from odoo_devkit.cli import build_parser
from odoo_devkit.dependency_workspace import (
    inspect_dependency_workspace,
    require_publishable_dependency_workspace,
    require_staged_build_requirements_supplied,
    stage_publishable_dependency_workspace,
)
from odoo_devkit.manifest import WorkspaceManifest, load_workspace_manifest


class DependencyWorkspaceTests(unittest.TestCase):
    def test_uv_lock_check_ignores_operator_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            staged_root = Path(temporary_directory_name)
            with mock.patch.dict(
                dependency_workspace.os.environ,
                {"KEEP_ME": "yes", "PIP_INDEX_URL": "https://private.invalid", "UV_INDEX_URL": "https://private.invalid"},
                clear=True,
            ):
                with mock.patch(
                    "odoo_devkit.dependency_workspace.subprocess.run",
                    return_value=mock.Mock(returncode=0),
                ) as run_mock:
                    self.assertTrue(dependency_workspace._uv_lock_is_current(staged_root))

            command = run_mock.call_args.args[0]
            execution_environment = run_mock.call_args.kwargs["env"]
            self.assertIn("--offline", command)
            self.assertIn("--no-config", command)
            self.assertEqual(execution_environment["KEEP_ME"], "yes")
            self.assertNotIn("PIP_INDEX_URL", execution_environment)
            self.assertNotIn("UV_INDEX_URL", execution_environment)

    def test_committed_dependency_reads_ignore_git_replace_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            repo_path = temp_root / "tenant"
            repo_path.mkdir()
            source_path = repo_path / "pyproject.toml"
            source_path.write_text("[tool.uv]\npackage = false\n", encoding="utf-8")
            self._commit_repo(repo_path)
            original_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True
            ).stdout.strip()
            source_path.write_text("[tool.uv]\npackage = true\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "replacement metadata"], cwd=repo_path, check=True, capture_output=True)
            replacement_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(
                ["git", "replace", original_commit, replacement_commit],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            destination_path = temp_root / "staged.toml"

            dependency_workspace._copy_regular_dependency_file(
                repo_path=repo_path,
                source_commit=original_commit,
                source_path=source_path,
                destination_path=destination_path,
                display_path="pyproject.toml",
            )

            self.assertEqual(destination_path.read_text(encoding="utf-8"), "[tool.uv]\npackage = false\n")

    def test_lockless_pure_addon_workspace_is_current_but_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "pure_addon")
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current)
            self.assertFalse(inspection.publishable)
            self.assertFalse(inspection.requires_tenant_lock)
            self.assertIsNone(inspection.tenant_lock_current)
            self.assertEqual(inspection.projects[0].path, "addons/pure_addon/pyproject.toml")
            with self.assertRaisesRegex(ValueError, "Artifact schema v2 requires"):
                require_publishable_dependency_workspace(manifest=manifest)

    def test_pure_addon_empty_workspace_is_publishable_with_tenant_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            pure_addon_root = tenant_repo_path / "addons" / "pure_addon"
            pure_addon_root.mkdir(parents=True)
            (pure_addon_root / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=())
            (tenant_repo_path / "uv.lock").unlink()
            subprocess.run(
                ["uv", "lock", "--offline", "--no-config"],
                cwd=tenant_repo_path,
                check=True,
                capture_output=True,
            )
            self._commit_repo(tenant_repo_path)
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)
            destination_root = temp_root / "staged"

            inspection = stage_publishable_dependency_workspace(
                manifest=manifest,
                destination_root=destination_root,
            )

            self.assertTrue(inspection.current)
            self.assertTrue(inspection.publishable)
            self.assertFalse(inspection.requires_tenant_lock)
            self.assertEqual(inspection.workspace_members, ())
            self.assertEqual(inspection.projects, ())
            self.assertTrue((destination_root / "pyproject.toml").is_file())
            self.assertTrue((destination_root / "uv.lock").is_file())
            self.assertTrue((destination_root / "addons" / "pure_addon").is_dir())
            self.assertFalse((destination_root / "addons" / "pure_addon" / "pyproject.toml").exists())

    def test_shared_workspace_glob_rejects_non_project_addon_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            shared_addon_root = shared_repo_path / "authentik_sso"
            shared_addon_root.mkdir(parents=True)
            (shared_addon_root / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon", "addons/shared/*"),
            )
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current") as uv_lock_is_current:
                inspection = inspect_dependency_workspace(manifest=manifest)

            uv_lock_is_current.assert_not_called()
            self.assertFalse(inspection.current)
            self.assertIn(
                "pyproject.toml workspace member pattern matches a directory without pyproject.toml: addons/shared/authentik_sso",
                inspection.findings,
            )

    def test_explicit_members_ignore_non_project_shared_addon_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            shared_addon_root = shared_repo_path / "authentik_sso"
            shared_addon_root.mkdir(parents=True)
            (shared_addon_root / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon",),
            )
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=True):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current)
            self.assertTrue(inspection.publishable)
            self.assertEqual(inspection.workspace_members, ("addons/tenant_addon",))

    def test_untracked_shared_addon_directory_does_not_affect_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon", "addons/shared/*"),
            )
            shared_repo_path.mkdir(parents=True)
            (shared_repo_path / "README.md").write_text("shared\n", encoding="utf-8")
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            untracked_addon_root = shared_repo_path / "authentik_sso"
            untracked_addon_root.mkdir()
            (untracked_addon_root / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=True):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current)
            self.assertTrue(inspection.publishable)
            self.assertEqual(inspection.workspace_members, ("addons/tenant_addon",))

    def test_workspace_members_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=())
            pyproject_path = tenant_repo_path / "pyproject.toml"
            pyproject_path.write_text(
                pyproject_path.read_text(encoding="utf-8").replace("members = []\n", ""),
                encoding="utf-8",
            )
            self._commit_repo(tenant_repo_path)
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current") as uv_lock_is_current:
                inspection = inspect_dependency_workspace(manifest=manifest)

            uv_lock_is_current.assert_not_called()
            self.assertFalse(inspection.current)
            self.assertIn("pyproject.toml workspace must define members", inspection.findings)

    def test_empty_workspace_members_cannot_hide_discovered_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=())
            self._commit_repo(tenant_repo_path)
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current") as uv_lock_is_current:
                inspection = inspect_dependency_workspace(manifest=manifest)

            uv_lock_is_current.assert_not_called()
            self.assertFalse(inspection.current)
            self.assertIn("missing=['addons/tenant_addon']", inspection.findings[-1])

    def test_runtime_dependencies_require_root_workspace_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "runtime_addon",
                dependencies=("httpx==0.28.1",),
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertTrue(inspection.requires_tenant_lock)
            self.assertIn(
                "Owned runtime dependency declarations require a tenant root pyproject.toml and uv.lock.",
                inspection.findings,
            )

    def test_dependency_workspace_requires_root_and_lock_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=("addons/*",))
            (tenant_repo_path / "uv.lock").unlink()
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertIn(
                "Tenant dependency workspace requires pyproject.toml and uv.lock as a complete pair.",
                inspection.findings,
            )

    def test_combined_tenant_and_shared_workspace_uses_uv_as_lock_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "tenant_addon",
                dependencies=("httpx==0.28.1",),
            )
            self._write_member_pyproject(
                shared_repo_path / "shared_addon",
                project_name="shared_addon",
                dependencies=("pydantic==2.13.4",),
            )
            lock_bytes = b"version = 1\n"
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon", "addons/shared/shared_addon"),
                lock_bytes=lock_bytes,
            )
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            def validate_staged_workspace(staged_root: Path) -> bool:
                self.assertTrue((staged_root / "addons" / "tenant_addon" / "pyproject.toml").is_file())
                self.assertTrue((staged_root / "addons" / "shared" / "shared_addon" / "pyproject.toml").is_file())
                return True

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", side_effect=validate_staged_workspace):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current)
            self.assertTrue(inspection.publishable)
            self.assertTrue(inspection.tenant_lock_current)
            self.assertEqual(inspection.tenant_lock_sha256, hashlib.sha256(lock_bytes).hexdigest())
            self.assertEqual(
                inspection.workspace_members,
                ("addons/shared/shared_addon", "addons/tenant_addon"),
            )
            self.assertEqual({project.owner for project in inspection.projects}, {"tenant", "shared_addons"})

    def test_workspace_member_drift_fails_before_uv_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_member_pyproject(shared_repo_path / "shared_addon", project_name="shared_addon")
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon",),
            )
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current") as uv_lock_is_current:
                inspection = inspect_dependency_workspace(manifest=manifest)

            uv_lock_is_current.assert_not_called()
            self.assertFalse(inspection.current)
            self.assertIn("missing=['addons/shared/shared_addon']", inspection.findings[-1])

    def test_mutable_vcs_dependency_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "runtime_addon",
                dependencies=("simple-zpl2 @ git+https://github.com/example/simple-zpl2@main",),
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertIn("VCS dependencies must use exact lowercase git commits", inspection.findings[0])

    def test_build_requirement_extras_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            addon_root = tenant_repo_path / "addons" / "runtime_addon"
            self._write_member_pyproject(addon_root)
            pyproject_path = addon_root / "pyproject.toml"
            pyproject_path.write_text(
                pyproject_path.read_text(encoding="utf-8").replace(
                    "hatchling==1.27.0",
                    "hatchling[extra]==1.27.0",
                ),
                encoding="utf-8",
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertTrue(any("build requirements must use exact registry versions" in item for item in inspection.findings))

    def test_uv_git_source_fields_match_strict_runtime_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            addon_root = tenant_repo_path / "addons" / "runtime_addon"
            self._write_member_pyproject(addon_root, dependencies=("private-package==1.0.0",))
            pyproject_path = addon_root / "pyproject.toml"
            pyproject_path.write_text(
                pyproject_path.read_text(encoding="utf-8")
                + '\n[tool.uv.sources]\nprivate-package = { git = "https://github.com/example/private-package", rev = "'
                + "a" * 40
                + '", lfs = true }\n',
                encoding="utf-8",
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertTrue(any("unsupported git source fields" in finding for finding in inspection.findings))

    def test_stale_lock_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=("addons/*",))
            self._commit_repo(tenant_repo_path)
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=False):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertFalse(inspection.tenant_lock_current)
            self.assertIn("Tenant uv.lock is not current", inspection.findings[-1])

    def test_publish_stage_preserves_exact_tenant_and_shared_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_member_pyproject(shared_repo_path / "shared_addon", project_name="shared_addon")
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/tenant_addon", "addons/shared/shared_addon"),
            )
            self._commit_repo(tenant_repo_path)
            self._commit_repo(shared_repo_path)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )
            destination_root = temp_root / "staged"

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=True):
                inspection = stage_publishable_dependency_workspace(
                    manifest=manifest,
                    destination_root=destination_root,
                )

            self.assertTrue(inspection.publishable)
            self.assertTrue((destination_root / "addons" / "tenant_addon" / "pyproject.toml").is_file())
            self.assertTrue((destination_root / "addons" / "shared" / "shared_addon" / "pyproject.toml").is_file())

    def test_publishable_workspace_requires_tracked_regular_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=("addons/*",))
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.publishable)
            self.assertTrue(any("requires a Git worktree" in finding for finding in inspection.findings))

    def test_custom_uv_package_sources_fail_without_echoing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "tenant_addon",
                dependencies=("private-package @ https://operator:secret@example.invalid/package.whl",),
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)
            serialized = json.dumps(inspection.to_dict(), sort_keys=True)

            self.assertFalse(inspection.current)
            self.assertNotIn("operator", serialized)
            self.assertNotIn("secret", serialized)
            self.assertEqual(inspection.projects[0].runtime_dependencies, ("private-package",))

    def test_custom_uv_index_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=("addons/*",))
            root_pyproject = tenant_repo_path / "pyproject.toml"
            root_pyproject.write_text(
                root_pyproject.read_text(encoding="utf-8")
                + '\n[[tool.uv.index]]\nname = "private"\nurl = "https://packages.example.invalid/simple"\n',
                encoding="utf-8",
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertTrue(any("cannot configure custom uv package sources" in finding for finding in inspection.findings))

    def test_tenant_dependency_metadata_cannot_shadow_shared_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            shared_repo_path = temp_root / "shared"
            self._write_member_pyproject(tenant_repo_path / "addons" / "shared" / "shadow")
            shared_repo_path.mkdir(parents=True)
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                shared_repo_path=shared_repo_path,
            )

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertTrue(any("reserved shared-addons namespace" in finding for finding in inspection.findings))

    def test_real_uv_lock_check_accepts_exact_staged_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(tenant_repo_path / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_repo_path, members=("addons/*",), lock_bytes=b"")
            (tenant_repo_path / "uv.lock").unlink()
            subprocess.run(
                ["uv", "lock", "--project", str(tenant_repo_path)],
                cwd=tenant_repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            self._commit_repo(tenant_repo_path)
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current, inspection.findings)
            self.assertTrue(inspection.publishable)

    def test_addon_build_requirements_must_be_supplied_by_a_lock_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            support_root = temp_root / "runtime"
            tenant_root = temp_root / "project"
            self._write_member_pyproject(tenant_root / "addons" / "tenant_addon")
            self._write_root_workspace(tenant_repo_path=tenant_root, members=("addons/*",))
            support_root.mkdir(parents=True)
            (support_root / "pyproject.toml").write_text(
                '[project]\nname = "runtime-support"\nversion = "0.0.0"\ndependencies = []\n\n[tool.uv]\npackage = false\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "hatchling==1.27.0"):
                require_staged_build_requirements_supplied(
                    support_root=support_root,
                    tenant_root=tenant_root,
                )

            support_pyproject = support_root / "pyproject.toml"
            support_pyproject.write_text(
                support_pyproject.read_text(encoding="utf-8").replace(
                    "dependencies = []",
                    'dependencies = ["hatchling==1.27.0"]',
                ),
                encoding="utf-8",
            )
            require_staged_build_requirements_supplied(
                support_root=support_root,
                tenant_root=tenant_root,
            )

    def test_dependency_inspection_checks_devkit_build_requirement_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            devkit_repo_path = temp_root / "devkit"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "tenant_addon",
                build_requirement="hatchling==1.31.0",
            )
            self._write_root_workspace(
                tenant_repo_path=tenant_repo_path,
                members=("addons/*",),
            )
            support_root = devkit_repo_path / "docker" / "runtime-python"
            support_root.mkdir(parents=True)
            support_pyproject = support_root / "pyproject.toml"
            support_pyproject.write_text(
                '[project]\nname = "runtime-support"\nversion = "0.0.0"\n'
                'dependencies = ["hatchling==1.27.0"]\n\n[tool.uv]\npackage = false\n',
                encoding="utf-8",
            )
            manifest = self._write_manifest(
                temp_root=temp_root,
                tenant_repo_path=tenant_repo_path,
                devkit_repo_path=devkit_repo_path,
            )
            self._commit_repo(tenant_repo_path)

            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=True):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertIn(
                "Addon build requirements must be supplied by the support/runtime or tenant lock catalog: hatchling==1.31.0",
                inspection.findings,
            )

            support_pyproject.write_text(
                support_pyproject.read_text(encoding="utf-8").replace(
                    "hatchling==1.27.0",
                    "hatchling==1.31.0",
                ),
                encoding="utf-8",
            )
            with mock.patch("odoo_devkit.dependency_workspace._uv_lock_is_current", return_value=True):
                inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertTrue(inspection.current, inspection.findings)

    def test_owned_requirements_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            addon_root = tenant_repo_path / "addons" / "tenant_addon"
            self._write_member_pyproject(addon_root)
            (addon_root / "requirements.txt").write_text("httpx==0.28.1\n", encoding="utf-8")
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)

            inspection = inspect_dependency_workspace(manifest=manifest)

            self.assertFalse(inspection.current)
            self.assertIn("requirements must move into pyproject.toml", inspection.findings[0])

    def test_cli_inspect_and_check_emit_structured_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temp_root = Path(temporary_directory_name)
            tenant_repo_path = temp_root / "tenant"
            self._write_member_pyproject(
                tenant_repo_path / "addons" / "runtime_addon",
                dependencies=("httpx==0.28.1",),
            )
            manifest = self._write_manifest(temp_root=temp_root, tenant_repo_path=tenant_repo_path)
            parser = build_parser()

            inspect_arguments = parser.parse_args(["dependencies", "inspect", "--manifest", str(manifest.manifest_path)])
            inspect_output = io.StringIO()
            with contextlib.redirect_stdout(inspect_output):
                inspect_arguments.handler(inspect_arguments)
            self.assertFalse(json.loads(inspect_output.getvalue())["current"])

            check_arguments = parser.parse_args(["dependencies", "check", "--manifest", str(manifest.manifest_path)])
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "1"):
                    check_arguments.handler(check_arguments)

    @staticmethod
    def _write_member_pyproject(
        project_root: Path,
        *,
        project_name: str = "tenant_addon",
        dependencies: tuple[str, ...] = (),
        build_requirement: str = "hatchling==1.27.0",
    ) -> None:
        project_root.mkdir(parents=True, exist_ok=True)
        dependency_lines = ",\n".join(f"    {json.dumps(dependency)}" for dependency in dependencies)
        if dependency_lines:
            dependency_lines += ",\n"
        (project_root / "pyproject.toml").write_text(
            "[build-system]\n"
            f"requires = [{json.dumps(build_requirement)}]\n"
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            f'name = "{project_name}"\n'
            'version = "0.0.0"\n'
            "dependencies = [\n"
            f"{dependency_lines}"
            "]\n\n"
            "[tool.uv]\n"
            "package = false\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_root_workspace(
        *,
        tenant_repo_path: Path,
        members: tuple[str, ...],
        lock_bytes: bytes = b"version = 1\n",
    ) -> None:
        tenant_repo_path.mkdir(parents=True, exist_ok=True)
        member_lines = ", ".join(json.dumps(member) for member in members)
        (tenant_repo_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "tenant-dependencies"\n'
            'version = "0.0.0"\n'
            "dependencies = []\n\n"
            "[tool.uv]\n"
            "package = false\n\n"
            "[tool.uv.workspace]\n"
            f"members = [{member_lines}]\n",
            encoding="utf-8",
        )
        (tenant_repo_path / "uv.lock").write_bytes(lock_bytes)

    @staticmethod
    def _commit_repo(repo_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo_path, check=True)
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "dependency workspace"], cwd=repo_path, check=True)

    @staticmethod
    def _write_manifest(
        *,
        temp_root: Path,
        tenant_repo_path: Path,
        shared_repo_path: Path | None = None,
        devkit_repo_path: Path | None = None,
    ) -> WorkspaceManifest:
        tenant_repo_path.mkdir(parents=True, exist_ok=True)
        shared_repo_table = ""
        if shared_repo_path is not None:
            shared_repo_path.mkdir(parents=True, exist_ok=True)
            shared_repo_table = f'\n[repos.shared_addons]\nname = "shared-addons"\npath = "{shared_repo_path}"\nref = "main"\n'
        devkit_repo_table = ""
        if devkit_repo_path is not None:
            devkit_repo_path.mkdir(parents=True, exist_ok=True)
            devkit_repo_table = f'\n[repos.devkit]\nname = "odoo-devkit"\npath = "{devkit_repo_path}"\nref = "main"\n'
        manifest_path = tenant_repo_path / "workspace.toml"
        manifest_path.write_text(
            "schema_version = 1\n"
            'tenant = "test"\n\n'
            "[workspace]\n"
            'name = "test"\n'
            'python = "3.13"\n'
            f'workspace_root = "{temp_root / "workspaces"}"\n\n'
            "[repos.tenant]\n"
            'name = "tenant"\n'
            'path = "."\n'
            'ref = "main"\n'
            f"{shared_repo_table}{devkit_repo_table}\n"
            "[runtime]\n"
            'context = "test"\n'
            'instance = "local"\n'
            'database = "test"\n'
            'addons_paths = ["sources/tenant/addons"]\n\n'
            "[ide]\n"
            'mode = "tenant_repo"\n'
            'focus_paths = ["addons"]\n'
            "attached_paths = []\n",
            encoding="utf-8",
        )
        return load_workspace_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
