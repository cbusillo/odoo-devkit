from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import artifact_inputs, local_runtime
from odoo_devkit.cli import (
    _handle_runtime_build,
    _handle_runtime_down,
    _handle_runtime_logs,
    _handle_runtime_odoo_shell,
    _handle_runtime_psql,
    _handle_runtime_restore,
    _handle_runtime_workflow,
)
from odoo_devkit.manifest import load_workspace_manifest
from odoo_devkit.runtime import (
    build_runtime_platform_command,
    resolve_runtime_repo_path,
    run_native_runtime_build,
    run_native_runtime_down,
    run_native_runtime_inspect,
    run_native_runtime_logs,
    run_native_runtime_odoo_shell,
    run_native_runtime_psql,
    run_native_runtime_publish,
    run_native_runtime_restore,
    run_native_runtime_select,
    run_native_runtime_up,
    run_native_runtime_workflow,
    run_runtime_platform_command,
)
from odoo_devkit.workspace import sync_workspace


class RuntimeCommandTests(unittest.TestCase):
    artifact_image_digest = "sha256:" + "1" * 64

    def setUp(self) -> None:
        super().setUp()
        self.explicit_payload_loader = local_runtime.load_environment_from_explicit_payload
        self.remote_source_commit_verifier = local_runtime.require_remote_source_commit
        self.environment_patch = mock.patch.dict(os.environ, {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: "{}"})
        self.environment_patch.start()
        self.remote_source_commit_patch = mock.patch("odoo_devkit.local_runtime.require_remote_source_commit")
        self.remote_source_commit_patch.start()
        self.load_environment_patch = mock.patch(
            "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
            side_effect=self._load_environment_from_explicit_payload,
        )
        self.load_environment_patch.start()

    def tearDown(self) -> None:
        self.load_environment_patch.stop()
        self.remote_source_commit_patch.stop()
        self.environment_patch.stop()
        super().tearDown()

    def _write_buildx_metadata_for_command(self, command: list[str]) -> None:
        metadata_file = Path(command[command.index("--metadata-file") + 1])
        metadata_file.write_text(json.dumps({"containerimage.digest": self.artifact_image_digest}) + "\n", encoding="utf-8")

    def _write_artifact_build_outputs_for_command(self, command: list[str]) -> None:
        if "--metadata-file" in command:
            self._write_buildx_metadata_for_command(command)
            return
        if "--output" in command:
            self._write_dependency_evidence_for_command(command)

    @staticmethod
    def _base_image_provenance(*, role: str, image_reference: str | None = None) -> local_runtime.BaseImageProvenance:
        digest_character = "2" if role == "runtime" else "3"
        digest = "sha256:" + digest_character * 64
        repository, tags = local_runtime.split_image_reference(image_reference or f"ghcr.io/example/{role}:19.0-{role}")
        return local_runtime.BaseImageProvenance(
            role=role,
            repository=repository,
            digest=digest,
            digest_reference=f"{repository}@{digest}",
            tags=tags,
            source_repository="example/odoo-enterprise-docker",
            source_ref="4" * 40,
        )

    def _resolve_base_image_provenance_fixture(
        self,
        *,
        image_reference: str,
        role: str,
        required_platforms: tuple[str, ...],
    ) -> local_runtime.BaseImageProvenance:
        self.assertTrue(required_platforms)
        return self._base_image_provenance(role=role, image_reference=image_reference)

    @staticmethod
    def _write_dependency_evidence_for_command(command: list[str]) -> None:
        output_value = command[command.index("--output") + 1]
        output_options = dict(option.split("=", 1) for option in output_value.split(",") if "=" in option)
        evidence_root = Path(output_options["dest"])
        staged_root = Path(command[-1])
        support_source = json.loads(
            (staged_root / "runtime" / local_runtime.DEPENDENCY_SOURCE_MARKER_FILE).read_text(encoding="utf-8")
        )
        tenant_source = json.loads(
            (staged_root / "project" / local_runtime.DEPENDENCY_SOURCE_MARKER_FILE).read_text(encoding="utf-8")
        )
        uv_locks = [
            {
                "scope": "support_runtime",
                "source_repository": support_source["repository"],
                "source_ref": support_source["ref"],
                "path": support_source["lock_path"],
                "sha256": hashlib.sha256((staged_root / "runtime" / "uv.lock").read_bytes()).hexdigest(),
            },
            {
                "scope": "tenant",
                "source_repository": tenant_source["repository"],
                "source_ref": tenant_source["ref"],
                "path": tenant_source["lock_path"],
                "sha256": hashlib.sha256((staged_root / "project" / "uv.lock").read_bytes()).hexdigest(),
            },
        ]
        packages: list[dict[str, object]] = []
        packages_sha256 = hashlib.sha256(
            json.dumps(packages, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for platform in command[command.index("--platform") + 1].split(","):
            output_directory = evidence_root / platform.replace("/", "_")
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / "dependency-provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "layout": "two_lock",
                        "publishable": True,
                        "target_platform": platform,
                        "uv_locks": uv_locks,
                        "python_environment": {
                            "python_version": "3.13.7",
                            "packages": packages,
                            "package_count": 0,
                            "packages_sha256": packages_sha256,
                        },
                        "external_compatibility_inputs": [],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    def _configure_publish_runtime_payload(
        self,
        *,
        context: str = "opw",
        instance: str = "testing",
        odoo_version: str | None = "19.0",
        environment: dict[str, str] | None = None,
        include_deployment_secrets: bool = True,
    ) -> None:
        payload_environment = {
            "GITHUB_TOKEN": "gh-token",
            "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/example/runtime:19.0-runtime",
            "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/example/devtools:19.0-devtools",
        }
        if include_deployment_secrets:
            payload_environment.update(
                {
                    "ODOO_MASTER_PASSWORD": "runtime-payload-master",
                    "ODOO_DB_USER": "odoo",
                    "ODOO_DB_PASSWORD": "runtime-payload-database",
                }
            )
        if odoo_version is not None:
            payload_environment["ODOO_VERSION"] = odoo_version
        payload_environment.update(environment or {})
        environment_patch = mock.patch.dict(
            os.environ,
            {
                local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: json.dumps(
                    {
                        "context": context,
                        "instance": instance,
                        "environment": payload_environment,
                    }
                )
            },
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def test_resolve_runtime_repo_path_prefers_explicit_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            self.assertEqual(resolve_runtime_repo_path(manifest), runtime_repo_path.resolve())

    def test_resolve_buildx_metadata_image_digest_reads_primary_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "build-metadata.json"
            metadata_file.write_text(json.dumps({"containerimage.digest": self.artifact_image_digest}) + "\n", encoding="utf-8")

            self.assertEqual(local_runtime.resolve_buildx_metadata_image_digest(metadata_file), self.artifact_image_digest)

    def test_resolve_buildx_metadata_image_digest_reads_descriptor_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "build-metadata.json"
            metadata_file.write_text(
                json.dumps({"containerimage.descriptor": {"digest": self.artifact_image_digest}}) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(local_runtime.resolve_buildx_metadata_image_digest(metadata_file), self.artifact_image_digest)

    def test_resolve_buildx_metadata_image_digest_normalizes_hex_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "build-metadata.json"
            metadata_file.write_text(
                json.dumps({"containerimage.digest": "sha256:" + "A" * 64}) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                local_runtime.resolve_buildx_metadata_image_digest(metadata_file),
                "sha256:" + "a" * 64,
            )

    def test_resolve_buildx_metadata_image_digest_rejects_missing_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "build-metadata.json"
            metadata_file.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "valid container image digest"):
                local_runtime.resolve_buildx_metadata_image_digest(metadata_file)

    def test_resolve_base_image_provenance_inspects_the_resolved_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        image_metadata = {
            platform: {
                "config": {
                    "Labels": {
                        "org.opencontainers.image.source": "https://github.com/example/odoo-enterprise-docker",
                        "org.opencontainers.image.revision": "b" * 40,
                    }
                }
            }
            for platform in ("linux/amd64", "linux/arm64")
        }
        with mock.patch("odoo_devkit.local_runtime.resolve_image_digest", return_value=digest):
            with mock.patch(
                "odoo_devkit.local_runtime.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=json.dumps(image_metadata), stderr=""),
            ) as run_mock:
                provenance = local_runtime.resolve_base_image_provenance(
                    image_reference="ghcr.io/example/runtime:stable",
                    role="runtime",
                    required_platforms=("linux/amd64", "linux/arm64"),
                )

        self.assertEqual(provenance.digest_reference, f"ghcr.io/example/runtime@{digest}")
        self.assertEqual(provenance.tags, ("stable",))
        self.assertEqual(provenance.source_repository, "example/odoo-enterprise-docker")
        self.assertEqual(
            run_mock.call_args.args[0][:5],
            ["docker", "buildx", "imagetools", "inspect", f"ghcr.io/example/runtime@{digest}"],
        )

    def test_resolve_base_image_provenance_accepts_single_platform_image_shape(self) -> None:
        digest = "sha256:" + "a" * 64
        image_metadata = {
            "architecture": "arm64",
            "os": "linux",
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/example/odoo-enterprise-docker",
                    "org.opencontainers.image.revision": "b" * 40,
                }
            },
        }
        with mock.patch("odoo_devkit.local_runtime.resolve_image_digest", return_value=digest):
            with mock.patch(
                "odoo_devkit.local_runtime.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=json.dumps(image_metadata), stderr=""),
            ):
                provenance = local_runtime.resolve_base_image_provenance(
                    image_reference=f"ghcr.io/example/runtime@{digest}",
                    role="runtime",
                    required_platforms=("linux/arm64",),
                )

        self.assertEqual(provenance.digest_reference, f"ghcr.io/example/runtime@{digest}")

    def test_copy_required_path_materializes_only_committed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_path = self._create_git_repo(temp_root / "source-repo")
            source_path = repo_path / "payload"
            source_path.mkdir()
            (source_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
            (source_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tracked payload"], cwd=repo_path, check=True, capture_output=True)
            (source_path / "operator.secret").write_text("do not stage\n", encoding="utf-8")
            destination_path = temp_root / "destination"
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            local_runtime.copy_required_path(
                repo_path=repo_path,
                source_commit=source_commit,
                source_path=source_path,
                destination_path=destination_path,
                label="test payload",
            )

            self.assertEqual((destination_path / "tracked.txt").read_text(encoding="utf-8"), "tracked\n")
            self.assertFalse((destination_path / "operator.secret").exists())

    def test_copy_required_path_requires_git_root_and_rejects_source_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_path = self._create_git_repo(temp_root / "source-repo")
            payload_root = repo_path / "payload"
            payload_root.mkdir()
            (payload_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            symlink_path = repo_path / "payload-link"
            symlink_path.symlink_to("payload", target_is_directory=True)
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "payload paths"], cwd=repo_path, check=True, capture_output=True)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True
            ).stdout.strip()

            with self.assertRaisesRegex(ValueError, "Git worktree root"):
                local_runtime.copy_required_path(
                    repo_path=payload_root,
                    source_commit=source_commit,
                    source_path=payload_root,
                    destination_path=temp_root / "nested-output",
                    label="nested payload",
                )
            with self.assertRaisesRegex(ValueError, "source-repository symlinks"):
                local_runtime.copy_required_path(
                    repo_path=repo_path,
                    source_commit=source_commit,
                    source_path=symlink_path,
                    destination_path=temp_root / "symlink-output",
                    label="symlink payload",
                )

    def test_artifact_git_reads_ignore_and_reject_replace_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_path = self._create_git_repo(temp_root / "source-repo")
            payload_path = repo_path / "payload.txt"
            payload_path.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "payload.txt"], cwd=repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "original payload"], cwd=repo_path, check=True, capture_output=True)
            original_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True
            ).stdout.strip()
            payload_path.write_text("replacement\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "replacement payload"], cwd=repo_path, check=True, capture_output=True)
            replacement_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(
                ["git", "replace", original_commit, replacement_commit],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            destination_path = temp_root / "staged-payload.txt"
            local_runtime.copy_required_path(
                repo_path=repo_path,
                source_commit=original_commit,
                source_path=payload_path,
                destination_path=destination_path,
                label="test payload",
            )

            self.assertEqual(destination_path.read_text(encoding="utf-8"), "original\n")
            with self.assertRaisesRegex(ValueError, "rejects git replace refs"):
                local_runtime.require_clean_git_commit(repo_path=repo_path, label="source-repo")

    def test_remote_source_commit_must_be_advertised_by_origin_ref(self) -> None:
        commit = "a" * 40
        with mock.patch(
            "odoo_devkit.local_runtime.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=f"{'b' * 40}\trefs/heads/main\n", stderr=""),
        ) as run_mock:
            with self.assertRaisesRegex(ValueError, "advertised by a ref"):
                self.remote_source_commit_verifier(
                    repository="example/source-repo",
                    commit=commit,
                    label="source-repo",
                    github_token="secret-token",
                )

        self.assertEqual(run_mock.call_args.args[0], ["git", "ls-remote", "https://github.com/example/source-repo.git"])
        execution_environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(execution_environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(execution_environment["ODOO_DEVKIT_GITHUB_TOKEN"], "secret-token")

    def test_clean_git_source_verifies_normalized_origin_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_path = self._create_git_repo(Path(temporary_directory) / "source-repo")

            source = local_runtime.require_clean_git_source(repo_path=repo_path, label="source-repo")

        self.assertEqual(source.repository, "example/source-repo")
        local_runtime.require_remote_source_commit.assert_called_once_with(
            repository="example/source-repo",
            commit=source.commit,
            label="source-repo",
            github_token=None,
        )

    def test_embedded_dependency_source_markers_are_reserved_for_devkit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            addons_root = Path(temporary_directory) / "addons"
            marker_path = addons_root / "shared" / "example" / local_runtime.DEPENDENCY_SOURCE_MARKER_FILE
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text('{"repository":"spoofed/source","ref":"' + "a" * 40 + '"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "reserved dependency source markers"):
                local_runtime.require_no_embedded_dependency_source_markers(roots=((addons_root, "owned addons"),))

    def test_clean_git_commit_rejects_assume_unchanged_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_path = self._create_git_repo(Path(temporary_directory) / "source-repo")
            subprocess.run(
                ["git", "update-index", "--assume-unchanged", "README.md"],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
            (repo_path / "README.md").write_text("hidden edit\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "rejects assume-unchanged"):
                local_runtime.require_clean_git_commit(repo_path=repo_path, label="source-repo")

    def test_staged_artifact_context_detects_changed_input_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            staged_root = Path(temporary_directory)
            dockerfile = staged_root / "docker" / "Dockerfile"
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            staged_context = local_runtime.StagedArtifactContext(
                file_hashes=local_runtime.snapshot_staged_artifact_files(staged_root),
                support_lock_sha256="a" * 64,
                tenant_lock_sha256="b" * 64,
            )
            dockerfile.write_text("FROM busybox\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "staged inputs changed"):
                local_runtime.require_staged_artifact_context_unchanged(
                    staged_context_root=staged_root,
                    staged_context=staged_context,
                )

    def test_typed_odoo_instance_override_payload_from_legacy_setting_env(self) -> None:
        runtime_values = {
            "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-local.example.com",
            "ENV_OVERRIDE_AUTHENTIK__BASE_URL": "https://authentik.example.com",
            "ENV_OVERRIDE_SHOPIFY__TEST_STORE": "true",
            "ENV_OVERRIDE_DISABLE_CRON": "1",
        }

        local_runtime.apply_typed_odoo_instance_override_payload(
            runtime_values=runtime_values,
            context_name="opw",
            instance_name="local",
        )

        encoded_payload = runtime_values[local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
        payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))

        self.assertEqual(payload["context"], "opw")
        self.assertEqual(payload["instance"], "local")
        self.assertEqual(
            payload["config_parameters"],
            [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://opw-local.example.com"},
                }
            ],
        )
        self.assertIn(
            {
                "addon": "authentik_sso",
                "setting": "base_url",
                "value": {"source": "literal", "value": "https://authentik.example.com"},
            },
            payload["addon_settings"],
        )
        self.assertIn(
            {
                "addon": "shopify",
                "setting": "test_store",
                "value": {"source": "literal", "value": "true"},
            },
            payload["addon_settings"],
        )
        self.assertNotIn("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", runtime_values)
        self.assertNotIn("ENV_OVERRIDE_AUTHENTIK__BASE_URL", runtime_values)
        self.assertNotIn("ENV_OVERRIDE_SHOPIFY__TEST_STORE", runtime_values)
        self.assertEqual(runtime_values["ENV_OVERRIDE_DISABLE_CRON"], "1")

    def test_typed_odoo_instance_override_payload_from_stack_overrides(self) -> None:
        runtime_values: dict[str, str] = {"ENV_OVERRIDE_DISABLE_CRON": "1"}
        odoo_overrides = local_runtime.OdooOverrideDefinition(
            config_parameters={"web.base.url": "https://opw-local.example.com"},
            addon_settings={
                "authentik_sso": {
                    "base_url": "https://authentik.example.com",
                    "group_claim": "groups",
                }
            },
        )

        local_runtime.apply_typed_odoo_instance_override_payload(
            runtime_values=runtime_values,
            context_name="opw",
            instance_name="local",
            odoo_overrides=odoo_overrides,
        )

        encoded_payload = runtime_values[local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
        payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))

        self.assertEqual(
            payload["config_parameters"],
            [
                {
                    "key": "web.base.url",
                    "value": {"source": "literal", "value": "https://opw-local.example.com"},
                }
            ],
        )
        self.assertEqual(
            payload["addon_settings"],
            [
                {
                    "addon": "authentik_sso",
                    "setting": "base_url",
                    "value": {"source": "literal", "value": "https://authentik.example.com"},
                },
                {
                    "addon": "authentik_sso",
                    "setting": "group_claim",
                    "value": {"source": "literal", "value": "groups"},
                },
            ],
        )
        self.assertEqual(runtime_values["ENV_OVERRIDE_DISABLE_CRON"], "1")

    def test_typed_odoo_instance_override_payload_includes_website_bootstrap(self) -> None:
        runtime_values: dict[str, str] = {}
        website_bootstrap = local_runtime.WebsiteBootstrapDefinition(
            tenant="opw",
            install_modules=("opw_custom",),
            name="OPW",
            default_lang="en_US",
            homepage_url="/shop",
            primary_page_xmlid=None,
            logo_path="addons/opw_custom/static/description/icon.png",
            logo_alt="OPW",
            canonical_urls={"local": "https://opw-local.example.com"},
            pages_source={},
            routes_source={"kind": "controller", "module": "website_sale", "homepage_url": "/shop"},
            routes=(
                local_runtime.WebsiteBootstrapRouteDefinition(
                    name="Shop",
                    url="/shop",
                    module="website_sale",
                    published=True,
                    homepage=True,
                ),
            ),
        )

        local_runtime.apply_typed_odoo_instance_override_payload(
            runtime_values=runtime_values,
            context_name="opw",
            instance_name="local",
            website_bootstrap=website_bootstrap,
        )

        encoded_payload = runtime_values[local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
        payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))

        self.assertEqual(payload["config_parameters"], [])
        self.assertEqual(payload["addon_settings"], [])
        self.assertEqual(payload["website_bootstrap"]["canonical_url"], "https://opw-local.example.com")
        self.assertEqual(payload["website_bootstrap"]["homepage_url"], "/shop")
        self.assertEqual(payload["website_bootstrap"]["routes"][0]["module"], "website_sale")

    def test_data_workflow_script_environment_keeps_typed_payload(self) -> None:
        environment = {
            local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: "encoded-payload",
            local_runtime.LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY: "true",
            local_runtime.LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY: "true",
            "ODOO_DB_NAME": "opw",
            "UNRELATED": "value",
        }

        filtered_environment = local_runtime.data_workflow_script_environment(environment)

        self.assertEqual(filtered_environment[local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY], "encoded-payload")
        self.assertEqual(filtered_environment[local_runtime.LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY], "true")
        self.assertEqual(filtered_environment[local_runtime.LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY], "true")
        self.assertEqual(filtered_environment["ODOO_DB_NAME"], "opw")
        self.assertNotIn("UNRELATED", filtered_environment)

    def test_typed_odoo_instance_override_payload_rejects_stack_and_legacy_setting_mix(self) -> None:
        runtime_values = {
            "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-local.example.com",
        }
        odoo_overrides = local_runtime.OdooOverrideDefinition(
            config_parameters={"web.base.url": "https://opw-other.example.com"},
            addon_settings={},
        )

        with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "cannot be combined"):
            local_runtime.apply_typed_odoo_instance_override_payload(
                runtime_values=runtime_values,
                context_name="opw",
                instance_name="local",
                odoo_overrides=odoo_overrides,
            )

    def test_typed_odoo_instance_override_payload_rejects_mixed_authority(self) -> None:
        runtime_values = {
            local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: "already-set",
            "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-local.example.com",
        }

        with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "cannot be combined"):
            local_runtime.apply_typed_odoo_instance_override_payload(
                runtime_values=runtime_values,
                context_name="opw",
                instance_name="local",
            )

    def test_resolve_runtime_repo_path_requires_workspace_sync_for_repo_addressable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            self._initialize_git_repository(runtime_repo_path)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
url = "{runtime_repo_path}"
ref = "main"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "must be materialized by `platform workspace sync`"):
                resolve_runtime_repo_path(manifest)

    def test_synced_repo_addressable_runtime_supports_local_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            devkit_repo_path = self._create_git_repo(temp_root / "devkit-repo")
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            self._initialize_git_repository(runtime_repo_path)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                f"""
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"
workspace_root = "./assembled"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
url = "{runtime_repo_path}"
ref = "main"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)
            result = sync_workspace(manifest=manifest, devkit_repo_path=devkit_repo_path)

            materialized_runtime_repo_path = result.workspace_path / "sources" / "runtime"
            self.assertTrue(materialized_runtime_repo_path.exists())
            self.assertFalse(materialized_runtime_repo_path.is_symlink())
            self.assertEqual(resolve_runtime_repo_path(manifest), materialized_runtime_repo_path.resolve())
            self.assertEqual(
                build_runtime_platform_command(
                    manifest=manifest,
                    platform_subcommand="restore",
                ),
                (
                    "uv",
                    "--directory",
                    str(materialized_runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "restore",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                ),
            )

            payload = json.dumps(
                {
                    "context": "opw",
                    "instance": "local",
                    "environment": {
                        "ODOO_MASTER_PASSWORD": "test-master-value",
                        "ODOO_DB_USER": "odoo",
                        "ODOO_DB_PASSWORD": "test-database-value",
                    },
                }
            )
            inspect_output = io.StringIO()
            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch.dict(
                    os.environ,
                    {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: payload},
                    clear=True,
                ):
                    with contextlib.redirect_stdout(inspect_output):
                        exit_code = run_native_runtime_inspect(manifest=manifest)

            self.assertEqual(exit_code, 0)
            self.assertIn("context=opw", inspect_output.getvalue())
            self.assertIn("instance=local", inspect_output.getvalue())
            self.assertNotIn("test-master-value", inspect_output.getvalue())
            self.assertNotIn("test-database-value", inspect_output.getvalue())

    def test_resolve_runtime_repo_path_defaults_to_devkit_repo_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            shared_addons_repo_path = temp_root / "odoo-shared-addons"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.shared_addons]
name = "shared-addons-repo"
path = "../odoo-shared-addons"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons", "sources/shared-addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            self.assertEqual(resolve_runtime_repo_path(manifest), Path(__file__).resolve().parent.parent)

    def test_resolve_runtime_repo_path_requires_explicit_runtime_repo_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[runtime]
context = "opw"
instance = "dev"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, r"must declare \[repos.runtime\]"):
                resolve_runtime_repo_path(manifest)

    def test_build_runtime_platform_command_uses_manifest_context_and_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            self.assertEqual(
                build_runtime_platform_command(
                    manifest=manifest,
                    platform_subcommand="run",
                    platform_arguments=("--workflow", "update"),
                ),
                (
                    "uv",
                    "--directory",
                    str(runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "run",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                    "--workflow",
                    "update",
                ),
            )

    def test_run_runtime_platform_command_executes_from_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)

            manifest_path = tenant_repo_path / "workspace.toml"
            manifest_path.write_text(
                """
schema_version = 1
tenant = "opw"

[workspace]
name = "opw"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "../runtime-repo"

[runtime]
context = "opw"
instance = "local"
database = "opw"
addons_paths = ["sources/tenant/addons"]

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            manifest = load_workspace_manifest(manifest_path)

            completed_process = mock.Mock(returncode=17)
            with mock.patch("odoo_devkit.runtime.subprocess.run", return_value=completed_process) as run_mock:
                exit_code = run_runtime_platform_command(manifest=manifest, platform_subcommand="restore")

            self.assertEqual(exit_code, 17)
            run_mock.assert_called_once_with(
                (
                    "uv",
                    "--directory",
                    str(runtime_repo_path.resolve()),
                    "run",
                    "platform",
                    "restore",
                    "--context",
                    "opw",
                    "--instance",
                    "local",
                ),
                cwd=tenant_repo_path.resolve(),
                check=False,
            )

    def test_native_runtime_select_writes_runtime_env_and_pycharm_conf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            shared_addons_repo_path = temp_root / "shared-addons-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            (tenant_repo_path / "artifact-inputs.toml").write_text(
                """
schema_version = 1
sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "main" },
]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                shared_addons_repo_path=shared_addons_repo_path,
                addons_paths=("sources/tenant/addons", "sources/shared-addons"),
            )

            manifest = load_workspace_manifest(manifest_path)

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_native_runtime_select(manifest=manifest)

            self.assertEqual(exit_code, 0)
            runtime_env_file = runtime_repo_path / ".platform" / "env" / "opw.local.env"
            pycharm_conf_file = runtime_repo_path / ".platform" / "ide" / "opw.local.odoo.conf"
            self.assertTrue(runtime_env_file.exists())
            self.assertTrue(pycharm_conf_file.exists())
            runtime_env_text = runtime_env_file.read_text(encoding="utf-8")
            self.assertIn("PLATFORM_CONTEXT=opw", runtime_env_text)
            self.assertIn("ODOO_PROJECT_NAME=odoo-opw-local", runtime_env_text)
            self.assertIn("DOCKER_IMAGE=odoo-opw-local", runtime_env_text)
            self.assertIn("ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main", runtime_env_text)
            self.assertIn(f"ODOO_PROJECT_ADDONS_HOST_PATH={(tenant_repo_path / 'addons').resolve()}", runtime_env_text)
            addons_path_line = next(line for line in runtime_env_text.splitlines() if line.startswith("ODOO_ADDONS_PATH="))
            self.assertIn("/opt/project/addons", addons_path_line)
            self.assertIn("/opt/project/addons/shared", addons_path_line)
            self.assertIn("/opt/launchplane/addons", addons_path_line)
            pycharm_conf_text = pycharm_conf_file.read_text(encoding="utf-8")
            self.assertIn("db_port = 15432", pycharm_conf_text)
            self.assertIn(f"addons_path = {(tenant_repo_path / 'addons').resolve()}", pycharm_conf_text)
            self.assertNotIn(str(runtime_repo_path / "addons"), pycharm_conf_text)

    def test_native_runtime_select_rejects_legacy_stack_addon_source_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            (runtime_repo_path / "platform" / "stack.toml").write_text(
                """
schema_version = 1
odoo_version = "19.0"
addons_path = ["/odoo/addons", "/opt/launchplane/addons", "/opt/project/addons"]
addon_repository_selectors = ["cbusillo/disable_odoo_online@main"]
required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD"]

[contexts.opw]
database = "opw"
install_modules = ["opw_custom"]

[contexts.opw.instances.local]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "Legacy addon source keys are no longer supported"):
                run_native_runtime_select(manifest=manifest)

    def test_native_runtime_inspect_emits_key_value_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)
            output_buffer = io.StringIO()
            with contextlib.redirect_stdout(output_buffer):
                exit_code = run_native_runtime_inspect(manifest=manifest)

            self.assertEqual(exit_code, 0)
            inspect_output = output_buffer.getvalue()
            self.assertIn("context=opw", inspect_output)
            self.assertIn('"/odoo/addons"', inspect_output)
            self.assertIn(f'"{(tenant_repo_path / "addons").resolve()}"', inspect_output)
            self.assertIn("pycharm_addons_path=", inspect_output)
            self.assertIn("project_addons_host_path=", inspect_output)
            self.assertIn('"opw_custom"', inspect_output)

    def test_native_runtime_inspect_rejects_missing_required_payload_values(self) -> None:
        for environment in (
            {"ODOO_MASTER_PASSWORD": "test-master-value"},
            {
                "ODOO_MASTER_PASSWORD": "test-master-value",
                "ODOO_DB_USER": "odoo",
                "ODOO_DB_PASSWORD": "   ",
            },
        ):
            with self.subTest(environment=environment):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temp_root = Path(temporary_directory)
                    tenant_repo_path = temp_root / "tenant-repo"
                    runtime_repo_path = temp_root / "runtime-repo"
                    tenant_repo_path.mkdir(parents=True, exist_ok=True)
                    self._write_runtime_repo(runtime_repo_path)
                    manifest_path = self._write_manifest(
                        tenant_repo_path=tenant_repo_path,
                        runtime_repo_path=runtime_repo_path,
                    )
                    manifest = load_workspace_manifest(manifest_path)
                    payload = json.dumps(
                        {
                            "context": "opw",
                            "instance": "local",
                            "environment": environment,
                        }
                    )

                    with mock.patch(
                        "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                        side_effect=self.explicit_payload_loader,
                    ):
                        with mock.patch.dict(
                            os.environ,
                            {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: payload},
                            clear=True,
                        ):
                            with self.assertRaisesRegex(ValueError, "missing required non-empty values"):
                                run_native_runtime_inspect(manifest=manifest)

                    self.assertFalse((runtime_repo_path / ".platform").exists())

    def test_native_runtime_inspect_rejects_stack_override_of_required_payload_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            stack_path = runtime_repo_path / "platform" / "stack.toml"
            stack_path.write_text(
                stack_path.read_text(encoding="utf-8").replace(
                    "[contexts.opw.instances.local]\n\n[contexts.opw.instances.dev]",
                    """[contexts.opw.instances.local]

[contexts.opw.instances.local.runtime_env]
ODOO_DB_PASSWORD = ""

[contexts.opw.instances.dev]""",
                ),
                encoding="utf-8",
            )
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)
            payload = json.dumps(
                {
                    "context": "opw",
                    "instance": "local",
                    "environment": {
                        "ODOO_MASTER_PASSWORD": "test-master-value",
                        "ODOO_DB_USER": "odoo",
                        "ODOO_DB_PASSWORD": "test-database-value",
                    },
                }
            )

            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch.dict(
                    os.environ,
                    {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: payload},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "Resolved runtime environment is missing required"):
                        run_native_runtime_inspect(manifest=manifest)

            self.assertFalse((runtime_repo_path / ".platform").exists())

    def test_load_environment_uses_explicit_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            payload = json.dumps(
                {
                    "context": "opw",
                    "instance": "local",
                    "environment": {
                        "ODOO_MASTER_PASSWORD": "test-master-value",
                        "ODOO_DB_USER": "odoo",
                        "ODOO_DB_PASSWORD": "test-database-value",
                    },
                }
            )

            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch.dict(
                    os.environ,
                    {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: payload},
                    clear=True,
                ):
                    loaded_environment = local_runtime.load_environment(
                        repo_root=runtime_repo_path,
                        context_name="opw",
                        instance_name="local",
                    )

        self.assertEqual(loaded_environment.merged_values["ODOO_MASTER_PASSWORD"], "test-master-value")
        self.assertEqual(loaded_environment.merged_values["ODOO_DB_PASSWORD"], "test-database-value")

    def test_load_environment_fails_closed_when_payload_and_legacy_files_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)
            (runtime_repo_path / ".env").write_text("ODOO_MASTER_PASSWORD=legacy\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Legacy devkit-local env/secrets files are no longer supported"):
                local_runtime.load_environment(
                    repo_root=runtime_repo_path,
                    context_name="opw",
                    instance_name="local",
                )

    def test_load_environment_requires_explicit_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            runtime_repo_path = temp_root / "runtime-repo"
            self._write_runtime_repo(runtime_repo_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR):
                    local_runtime.load_environment(
                        repo_root=runtime_repo_path,
                        context_name="opw",
                        instance_name="local",
                    )

    def test_load_environment_rejects_legacy_local_env_files_without_payload(self) -> None:
        for relative_path in (Path(".env"), Path("platform/.env"), Path("platform/secrets.toml")):
            with self.subTest(relative_path=relative_path):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temp_root = Path(temporary_directory)
                    runtime_repo_path = temp_root / "runtime-repo"
                    self._write_runtime_repo(runtime_repo_path)
                    legacy_path = runtime_repo_path / relative_path
                    legacy_path.parent.mkdir(parents=True, exist_ok=True)
                    legacy_path.write_text("ODOO_MASTER_PASSWORD=legacy\n", encoding="utf-8")

                    with mock.patch.dict(os.environ, {}, clear=True):
                        with self.assertRaisesRegex(ValueError, "no longer supported"):
                            local_runtime.load_environment(
                                repo_root=runtime_repo_path,
                                context_name="opw",
                                instance_name="local",
                            )

    def test_runtime_environment_configuration_guidance_uses_explicit_payload(self) -> None:
        guidance = local_runtime.runtime_environment_configuration_guidance(noun="it")

        self.assertIn(local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, guidance)
        self.assertIn("selected context and instance", guidance)
        self.assertNotIn("runtime-environments.toml", guidance)
        self.assertNotIn("harbor", guidance)

    def test_command_execution_environment_excludes_runtime_payload(self) -> None:
        with mock.patch.dict(
            os.environ,
            {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: '{"environment":{"SECRET":"test-only"}}'},
        ):
            execution_environment = local_runtime.command_execution_env()

        self.assertNotIn(local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, execution_environment)

    def test_native_runtime_select_prefers_manifest_mounts_over_runtime_repo_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            shared_addons_repo_path = temp_root / "shared-addons-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
            shared_addons_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                shared_addons_repo_path=shared_addons_repo_path,
                addons_paths=("sources/tenant/addons", "sources/shared-addons"),
            )

            manifest = load_workspace_manifest(manifest_path)

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_native_runtime_select(manifest=manifest)

            self.assertEqual(exit_code, 0)
            runtime_env_file = runtime_repo_path / ".platform" / "env" / "opw.local.env"
            runtime_env_text = runtime_env_file.read_text(encoding="utf-8")
            self.assertIn("DOCKER_IMAGE=odoo-opw-local", runtime_env_text)
            self.assertIn(f"ODOO_PROJECT_ADDONS_HOST_PATH={(tenant_repo_path / 'addons').resolve()}", runtime_env_text)
            self.assertIn(f"ODOO_SHARED_ADDONS_HOST_PATH={shared_addons_repo_path.resolve()}", runtime_env_text)
            addons_path_line = next(line for line in runtime_env_text.splitlines() if line.startswith("ODOO_ADDONS_PATH="))
            self.assertIn("/opt/project/addons", addons_path_line)
            self.assertIn("/opt/project/addons/shared", addons_path_line)
            self.assertIn("/opt/launchplane/addons", addons_path_line)

    def test_native_runtime_select_includes_website_bootstrap_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            (tenant_repo_path / "website-bootstrap.toml").write_text(
                """
schema_version = 1
tenant = "opw"

[odoo]
install_modules = ["opw_custom", "website_sale"]

[website]
name = "OPW"
default_lang = "en_US"
homepage_url = "/shop"
logo_path = "addons/opw_custom/static/description/icon.png"
logo_alt = "OPW"

[website.routes_source]
kind = "controller"
module = "website_sale"
homepage_url = "/shop"

[website.canonical_urls]
local = "https://opw-local.example.com"
testing = "https://opw-testing.example.com"

[[website.routes]]
name = "Shop"
url = "/shop"
module = "website_sale"
published = true
homepage = true
""".strip()
                + "\n",
                encoding="utf-8",
            )
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_native_runtime_select(manifest=manifest)

            self.assertEqual(exit_code, 0)
            runtime_values = local_runtime.parse_env_file(runtime_repo_path / ".platform" / "env" / "opw.local.env")
            self.assertEqual(runtime_values["ODOO_INSTALL_MODULES"], "opw_custom,website_sale")
            encoded_payload = runtime_values[local_runtime.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))
            self.assertEqual(payload["website_bootstrap"]["canonical_url"], "https://opw-local.example.com")
            self.assertEqual(payload["website_bootstrap"]["routes_source"]["module"], "website_sale")
            self.assertEqual(payload["website_bootstrap"]["routes"][0]["url"], "/shop")

    def test_native_runtime_up_runs_compose_up_without_build_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_up(manifest=manifest, build_images=False)

            self.assertEqual(exit_code, 0)
            run_mock.assert_called_once()
            command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertEqual(command[-3:], ["up", "-d", "--no-build"])
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())

    def test_native_runtime_build_runs_compose_build_with_no_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images") as ensure_registry_auth:
                with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                    run_mock.return_value = mock.Mock(returncode=0)
                    with contextlib.redirect_stdout(io.StringIO()):
                        exit_code = run_native_runtime_build(manifest=manifest, no_cache=True)

            self.assertEqual(exit_code, 0)
            ensure_registry_auth.assert_called_once()
            command = run_mock.call_args.kwargs.get("args", run_mock.call_args.args[0])
            self.assertEqual(command[-2:], ["build", "--no-cache"])
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())

    def test_native_runtime_build_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_build(manifest=manifest, no_cache=False)

    def test_native_runtime_publish_builds_release_context_and_emits_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            shared_addons_repo_path = self._create_git_repo(temp_root / "shared-addons-repo")
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("opw_custom",))
            (tenant_repo_path / "addons" / "shared").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "shared" / "tenant_shadow.txt").write_text("ignore me\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            (shared_addons_repo_path / "shared_module").mkdir(parents=True, exist_ok=True)
            (shared_addons_repo_path / "shared_module" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=shared_addons_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "shared addon"], cwd=shared_addons_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                shared_addons_repo_path=shared_addons_repo_path,
                addons_paths=("sources/tenant/addons", "sources/shared-addons"),
                instance_name="local",
            )
            subprocess.run(["git", "add", "workspace.toml"], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload(instance="local")

            captured_build_contexts: list[Path] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = environment_overrides, allowed_return_codes
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_contexts.append(runtime_repo_path)
                    self.assertIn("--platform", command)
                    self.assertIn(",".join(local_runtime.DEFAULT_ARTIFACT_IMAGE_PLATFORMS), command)
                    self.assertIn("--metadata-file", command)
                    self.assertIn("--push", command)
                    self.assertTrue((runtime_repo_path / "addons" / "opw_custom" / "__manifest__.py").exists())
                    self.assertTrue((runtime_repo_path / "addons" / "shared" / "shared_module" / "__manifest__.py").exists())
                    self.assertFalse((runtime_repo_path / "addons" / "shared" / "tenant_shadow.txt").exists())

            output_file = temp_root / "artifact.json"
            with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images"):
                with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_image_push"):
                    with mock.patch("odoo_devkit.local_runtime.run_command", side_effect=fake_run_command):
                        with mock.patch(
                            "odoo_devkit.local_runtime.resolve_base_image_provenance",
                            side_effect=self._resolve_base_image_provenance_fixture,
                        ) as resolve_provenance_mock:
                            payload = run_native_runtime_publish(
                                manifest=manifest,
                                image_repository="ghcr.io/example/opw-runtime",
                                image_tag="opw-20260416-abcdef",
                                output_file=output_file,
                                no_cache=True,
                            )

            self.assertTrue(captured_build_contexts)
            self.assertEqual(
                [call.kwargs["role"] for call in resolve_provenance_mock.call_args_list],
                ["runtime", "devtools"],
            )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["image"]["repository"], "ghcr.io/example/opw-runtime")
            self.assertEqual(payload["image"]["digest"], self.artifact_image_digest)
            self.assertEqual(payload["enterprise_base_digest"], "sha256:" + "2" * 64)
            self.assertEqual(
                payload["dependency_provenance"]["target_platforms"],
                sorted(local_runtime.DEFAULT_ARTIFACT_IMAGE_PLATFORMS),
            )
            self.assertEqual(
                [base_image["role"] for base_image in payload["build_provenance"]["base_images"]],
                ["runtime", "devtools"],
            )
            self.assertEqual(payload["build_provenance"]["build_tools"][0]["name"], "odoo-devkit")
            self.assertEqual(payload["build_flags"]["values"]["build_target"], "production")
            self.assertEqual(payload["image"]["tags"], ["opw-20260416-abcdef"])
            self.assertEqual(
                payload["odoo_install_modules"],
                ["opw_custom"],
            )
            self.assertEqual(payload["output_file"], str(output_file.resolve()))
            written_payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(written_payload["artifact_id"], payload["artifact_id"])
            self.assertIn(
                {"repository": "example/shared-addons-repo", "ref": written_payload["addon_sources"][0]["ref"]},
                written_payload["addon_sources"],
            )

    def test_native_runtime_publish_resolves_addon_selectors_from_artifact_inputs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("opw_custom",))
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            (tenant_repo_path / "artifact-inputs.toml").write_text(
                """
schema_version = 1
sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "main" },
]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "workspace.toml", "artifact-inputs.toml"], cwd=tenant_repo_path, check=True, capture_output=True
            )
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload()

            captured_build_args: list[str] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = runtime_repo_path, environment_overrides, allowed_return_codes
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_args.extend(command)

            resolved_ref = "411f6b8e85cac72dc7aa2e2dc5540001043c327d"

            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images"):
                    with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_image_push"):
                        with mock.patch("odoo_devkit.local_runtime.run_command", side_effect=fake_run_command):
                            with mock.patch(
                                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                                return_value=resolved_ref,
                            ) as resolve_ref_mock:
                                with mock.patch(
                                    "odoo_devkit.local_runtime.resolve_base_image_provenance",
                                    side_effect=self._resolve_base_image_provenance_fixture,
                                ):
                                    payload = run_native_runtime_publish(
                                        manifest=manifest,
                                        image_repository="ghcr.io/example/opw-runtime",
                                        image_tag="opw-20260416-abcdef",
                                        output_file=None,
                                        no_cache=False,
                                    )

            resolve_ref_mock.assert_called_once_with(
                repository="cbusillo/disable_odoo_online",
                ref="main",
                github_token="gh-token",
            )
            addon_build_arg = next(argument for argument in captured_build_args if argument.startswith("ODOO_ADDON_REPOSITORIES="))
            self.assertEqual(
                addon_build_arg,
                f"ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@{resolved_ref}",
            )
            self.assertIn(
                {"repository": "cbusillo/disable_odoo_online", "ref": resolved_ref},
                payload["addon_sources"],
            )
            self.assertEqual(
                payload["addon_selectors"],
                [
                    {
                        "repository": "cbusillo/disable_odoo_online",
                        "selector": "main",
                        "resolved_ref": resolved_ref,
                    }
                ],
            )
            self.assertEqual(
                payload["odoo_install_modules"],
                ["launchplane_settings", "disable_odoo_online", "opw_custom"],
            )
            self.assertNotIn("odoo_addon_repository_selectors", payload["build_flags"]["values"])

    def test_registry_auth_splits_base_image_read_and_artifact_push_tokens(self) -> None:
        environment_values = {
            "GHCR_USERNAME": "cbusillo",
            "GHCR_READ_TOKEN": "read-token",
            "GHCR_TOKEN": "push-token",
            "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/odoo-enterprise-docker:19.0-runtime",
            "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/cbusillo/odoo-enterprise-docker:19.0-devtools",
        }
        login_inputs: list[str] = []

        def fake_subprocess_run(*args: object, **kwargs: object) -> mock.Mock:
            command = kwargs.get("args") or args[0]
            assert isinstance(command, list)
            if command[:3] == ["docker", "login", "ghcr.io"]:
                login_inputs.append(str(kwargs.get("input", "")).strip())
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:3] == ["docker", "buildx", "imagetools"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch.object(local_runtime, "_REGISTRY_LOGINS_DONE", set()):
            with mock.patch.object(local_runtime, "_VERIFIED_IMAGE_ACCESS", set()):
                with mock.patch("odoo_devkit.local_runtime.subprocess.run", side_effect=fake_subprocess_run):
                    local_runtime.ensure_registry_auth_for_base_images(environment_values)
                    local_runtime.ensure_registry_auth_for_image_push(
                        environment_values=environment_values,
                        image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
                    )

        self.assertEqual(login_inputs, ["read-token", "push-token"])

    def test_artifact_publish_runtime_values_require_odoo_version(self) -> None:
        with self.assertRaisesRegex(
            local_runtime.RuntimeCommandError,
            "environment must include ODOO_VERSION",
        ):
            local_runtime.validate_artifact_publish_runtime_values({})

    def test_artifact_publish_runtime_values_reject_skip_addons(self) -> None:
        with self.assertRaisesRegex(
            local_runtime.RuntimeCommandError,
            "does not support ODOO_PYTHON_SYNC_SKIP_ADDONS",
        ):
            local_runtime.validate_artifact_publish_runtime_values(
                {
                    "ODOO_VERSION": "19.0",
                    "ODOO_PYTHON_SYNC_SKIP_ADDONS": "legacy_addon",
                }
            )

    def test_native_runtime_publish_rejects_legacy_runtime_stack_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("opw_custom",))
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            (runtime_repo_path / "platform" / "stack.toml").write_text(
                """
schema_version = 1
odoo_version = "19.0"
addons_path = ["/odoo/addons", "/opt/launchplane/addons", "/opt/project/addons"]
addon_repository_selectors = ["cbusillo/disable_odoo_online@main"]
required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD"]

[contexts.opw]
database = "opw"
install_modules = ["opw_custom"]

[contexts.opw.instances.local]

[contexts.opw.instances.dev]

[contexts.opw.instances.testing]

[contexts.opw.instances.prod]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            subprocess.run(["git", "add", "workspace.toml"], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload()

            captured_build_args: list[str] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = runtime_repo_path, environment_overrides, allowed_return_codes
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_args.extend(command)

            with self.assertRaisesRegex(ValueError, "Legacy addon source keys are no longer supported"):
                run_native_runtime_publish(
                    manifest=manifest,
                    image_repository="ghcr.io/example/opw-runtime",
                    image_tag="opw-20260416-abcdef",
                    output_file=None,
                    no_cache=False,
                )

    def test_native_runtime_publish_prefers_artifact_inputs_manifest_over_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("opw_custom",))
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            (tenant_repo_path / "artifact-inputs.toml").write_text(
                """
schema_version = 1
sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "release-19" },
]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "workspace.toml", "artifact-inputs.toml"], cwd=tenant_repo_path, check=True, capture_output=True
            )
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload(
                environment={"ODOO_ADDON_REPOSITORIES": ("cbusillo/disable_odoo_online@ffffffffffffffffffffffffffffffffffffffff")}
            )

            captured_build_args: list[str] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = runtime_repo_path, environment_overrides, allowed_return_codes
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_args.extend(command)

            resolved_ref = "411f6b8e85cac72dc7aa2e2dc5540001043c327d"
            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images"):
                    with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_image_push"):
                        with mock.patch("odoo_devkit.local_runtime.run_command", side_effect=fake_run_command):
                            with mock.patch(
                                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                                return_value=resolved_ref,
                            ) as resolve_ref_mock:
                                with mock.patch(
                                    "odoo_devkit.local_runtime.resolve_base_image_provenance",
                                    side_effect=self._resolve_base_image_provenance_fixture,
                                ):
                                    payload = run_native_runtime_publish(
                                        manifest=manifest,
                                        image_repository="ghcr.io/example/opw-runtime",
                                        image_tag="opw-20260416-abcdef",
                                        output_file=None,
                                        no_cache=False,
                                    )

            resolve_ref_mock.assert_called_once_with(
                repository="cbusillo/disable_odoo_online",
                ref="release-19",
                github_token="gh-token",
            )
            addon_build_arg = next(argument for argument in captured_build_args if argument.startswith("ODOO_ADDON_REPOSITORIES="))
            self.assertEqual(
                addon_build_arg,
                f"ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@{resolved_ref}",
            )
            self.assertEqual(
                payload["addon_selectors"],
                [
                    {
                        "repository": "cbusillo/disable_odoo_online",
                        "selector": "release-19",
                        "resolved_ref": resolved_ref,
                    }
                ],
            )
            self.assertEqual(
                payload["odoo_install_modules"],
                ["launchplane_settings", "disable_odoo_online", "opw_custom"],
            )

    def test_native_runtime_publish_revalidates_required_values_after_artifact_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            stack_path = runtime_repo_path / "platform" / "stack.toml"
            stack_path.write_text(
                stack_path.read_text(encoding="utf-8").replace(
                    'required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD"]',
                    'required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD", "ODOO_ADDON_REPOSITORIES"]',
                ),
                encoding="utf-8",
            )
            (tenant_repo_path / "artifact-inputs.toml").write_text(
                "schema_version = 1\nsources = []\n",
                encoding="utf-8",
            )
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
                artifact_inputs_file="artifact-inputs.toml",
            )
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload(
                environment={"ODOO_ADDON_REPOSITORIES": "example/addon@411f6b8e85cac72dc7aa2e2dc5540001043c327d"}
            )

            with self.assertRaisesRegex(
                ValueError,
                "Resolved publish runtime environment is missing required non-empty values: ODOO_ADDON_REPOSITORIES",
            ):
                run_native_runtime_publish(
                    manifest=manifest,
                    image_repository="ghcr.io/example/opw-runtime",
                    image_tag="opw-20260416-abcdef",
                    output_file=None,
                    no_cache=False,
                )

    def test_native_runtime_publish_rejects_invalid_artifact_inputs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "opw_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "opw_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("opw_custom",))
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
                artifact_inputs_file="config/publish-inputs.toml",
            )
            (tenant_repo_path / "config").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "config" / "publish-inputs.toml").write_text(
                """
schema_version = 1
sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "main", exact_ref = "411f6b8e85cac72dc7aa2e2dc5540001043c327d" },
]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "workspace.toml", "config/publish-inputs.toml"], cwd=tenant_repo_path, check=True, capture_output=True
            )
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload()

            with self.assertRaisesRegex(ValueError, "exactly one of exact_ref or selector"):
                run_native_runtime_publish(
                    manifest=manifest,
                    image_repository="ghcr.io/example/opw-runtime",
                    image_tag="opw-20260416-abcdef",
                    output_file=None,
                    no_cache=False,
                )

    def test_artifact_inputs_manifest_merges_context_and_instance_sources(self) -> None:
        definition = artifact_inputs.parse_artifact_inputs_definition(
            payload={
                "schema_version": 1,
                "sources": [
                    {"repository": "cbusillo/disable_odoo_online", "selector": "main"},
                    {"repository": "example/global", "selector": "stable"},
                ],
                "contexts": {
                    "opw": {
                        "sources_add": [
                            {
                                "repository": "cbusillo/disable_odoo_online",
                                "exact_ref": "411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                            }
                        ],
                        "instances": {"testing": {"sources_add": [{"repository": "example/testing", "selector": "release-19"}]}},
                    }
                },
            },
            source_file_path=Path("/tmp/artifact-inputs.toml"),
        )

        effective_sources = artifact_inputs.effective_artifact_input_sources(
            artifact_inputs_definition=definition,
            context_name="opw",
            instance_name="testing",
        )

        self.assertEqual(
            tuple(source.repository_spec() for source in effective_sources),
            (
                "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                "example/global@stable",
                "example/testing@release-19",
            ),
        )

    def test_resolve_runtime_selection_tracks_effective_source_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            stack_definition = local_runtime.parse_stack_definition(
                {
                    "schema_version": 1,
                    "odoo_version": "19.0",
                    "addons_path": ["/odoo/addons", "/opt/project/addons"],
                    "contexts": {
                        "opw": {
                            "database": "opw",
                            "install_modules": ["opw_custom"],
                            "instances": {
                                "testing": {},
                            },
                        }
                    },
                },
                stack_file_path=temp_root / "platform" / "stack.toml",
            )
            artifact_inputs_definition = artifact_inputs.parse_artifact_inputs_definition(
                payload={
                    "schema_version": 1,
                    "sources": [
                        {"repository": "cbusillo/disable_odoo_online", "selector": "main"},
                        {"repository": "example/retained_selector", "selector": "stable"},
                    ],
                    "contexts": {
                        "opw": {
                            "sources_add": [
                                {
                                    "repository": "cbusillo/disable_odoo_online",
                                    "exact_ref": "411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                                }
                            ],
                            "instances": {
                                "testing": {"sources_add": [{"repository": "example/testing_selector", "selector": "release-19"}]}
                            },
                        }
                    },
                },
                source_file_path=temp_root / "artifact-inputs.toml",
            )

            selection = local_runtime.resolve_runtime_selection(
                stack_definition=stack_definition,
                artifact_inputs_definition=artifact_inputs_definition,
                context_name="opw",
                instance_name="testing",
                repo_root=temp_root,
            )

            self.assertEqual(
                selection.effective_source_repositories,
                (
                    "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                    "example/retained_selector@stable",
                    "example/testing_selector@release-19",
                ),
            )
            self.assertEqual(
                selection.effective_source_selectors,
                (
                    "example/retained_selector@stable",
                    "example/testing_selector@release-19",
                ),
            )
            self.assertEqual(
                selection.effective_install_modules,
                ("launchplane_settings", "disable_odoo_online", "opw_custom"),
            )

    def test_resolve_runtime_selection_keeps_local_modules_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            stack_definition = local_runtime.parse_stack_definition(
                {
                    "schema_version": 1,
                    "odoo_version": "19.0",
                    "addons_path": ["/odoo/addons", "/opt/project/addons"],
                    "contexts": {
                        "opw": {
                            "database": "opw",
                            "install_modules": ["opw_custom"],
                            "instances": {
                                "local": {},
                            },
                        }
                    },
                },
                stack_file_path=temp_root / "platform" / "stack.toml",
            )

            selection = local_runtime.resolve_runtime_selection(
                stack_definition=stack_definition,
                artifact_inputs_definition=None,
                context_name="opw",
                instance_name="local",
                repo_root=temp_root,
            )

            self.assertEqual(selection.effective_install_modules, ("opw_custom",))

    def test_resolve_runtime_selection_orders_managed_and_bootstrap_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            stack_definition = local_runtime.parse_stack_definition(
                {
                    "schema_version": 1,
                    "odoo_version": "19.0",
                    "addons_path": ["/odoo/addons", "/opt/project/addons"],
                    "contexts": {
                        "opw": {
                            "database": "opw",
                            "install_modules": ["opw_custom"],
                            "instances": {
                                "testing": {},
                            },
                        }
                    },
                },
                stack_file_path=temp_root / "platform" / "stack.toml",
            )
            website_bootstrap = local_runtime.parse_website_bootstrap_definition(
                {
                    "schema_version": 1,
                    "tenant": "opw",
                    "odoo": {"install_modules": ["opw_custom", "website_sale"]},
                    "website": {"name": "OPW"},
                },
                bootstrap_path=temp_root / "website-bootstrap.toml",
                context_name="opw",
            )

            selection = local_runtime.resolve_runtime_selection(
                stack_definition=stack_definition,
                artifact_inputs_definition=None,
                context_name="opw",
                instance_name="testing",
                repo_root=temp_root,
                website_bootstrap=website_bootstrap,
            )

            self.assertEqual(
                selection.effective_install_modules,
                ("launchplane_settings", "disable_odoo_online", "opw_custom", "website_sale"),
            )

    def test_checked_in_stack_keeps_hosted_runtime_authority_out_of_devkit(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        stack = local_runtime.load_stack(repo_root / "platform" / "stack.toml").stack_definition

        cm_context = stack.contexts["cm"]
        self.assertEqual(set(cm_context.instances), {"local", "dev", "testing"})
        self.assertEqual(cm_context.runtime_env, {})
        self.assertEqual(cm_context.odoo_overrides, local_runtime.empty_odoo_override_definition())
        for instance_name in ("dev", "testing"):
            instance = cm_context.instances[instance_name]
            self.assertEqual(instance.runtime_env, {})
            self.assertEqual(
                instance.odoo_overrides,
                local_runtime.empty_odoo_override_definition(),
            )
        self.assertNotIn("prod", cm_context.instances)
        self.assertEqual(
            cm_context.instances["local"].odoo_overrides.addon_settings["authentik_sso"]["base_url"],
            "https://authentik.cellmechanic.com",
        )

        opw_context = stack.contexts["opw"]
        self.assertEqual(set(opw_context.instances), {"local"})
        self.assertEqual(opw_context.runtime_env, {})
        self.assertEqual(opw_context.odoo_overrides, local_runtime.empty_odoo_override_definition())
        self.assertEqual(
            opw_context.instances["local"].runtime_env["OPENUPGRADE_ENABLED"],
            True,
        )

    def test_runtime_payload_synthesizes_missing_instance_in_existing_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir()
            runtime_repo_path.mkdir()
            manifest = load_workspace_manifest(
                self._write_manifest(
                    tenant_repo_path=tenant_repo_path,
                    runtime_repo_path=runtime_repo_path,
                    instance_name="testing",
                )
            )
            stack = local_runtime.parse_stack_definition(
                {
                    "schema_version": 1,
                    "odoo_version": "19.0",
                    "addons_path": ["/odoo/addons", "/opt/project/addons"],
                    "contexts": {
                        "opw": {
                            "database": "opw",
                            "install_modules": ["opw_custom"],
                            "instances": {"local": {}},
                        }
                    },
                },
                stack_file_path=runtime_repo_path / "platform" / "stack.toml",
            )

            synthesized = local_runtime.synthesize_runtime_payload_context(
                manifest=manifest,
                stack_definition=stack,
            )

            self.assertEqual(set(synthesized.contexts["opw"].instances), {"local", "testing"})
            self.assertEqual(synthesized.contexts["opw"].install_modules, ("opw_custom",))
            self.assertEqual(synthesized.contexts["opw"].instances["testing"].database, "opw")

    def test_native_runtime_publish_accepts_build_only_payload_and_prefers_exact_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "cm_custom").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "cm_custom" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("cm_custom",))
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant addons"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            stack_file = runtime_repo_path / "platform" / "stack.toml"
            stack_file.write_text(
                stack_file.read_text(encoding="utf-8").replace(
                    'install_modules = ["opw_custom"]',
                    """install_modules = ["opw_custom"]
runtime_env = { ODOO_VERSION = "18.0", ODOO_BASE_RUNTIME_IMAGE = "ghcr.io/example/runtime:18.0-runtime", ODOO_BASE_DEVTOOLS_IMAGE = "ghcr.io/example/devtools:18.0-devtools", ODOO_ADDON_REPOSITORIES = "example/stale-addon@main", OPENUPGRADE_ADDON_REPOSITORY = "example/stale-openupgrade@main", OPENUPGRADELIB_INSTALL_SPEC = "example-stale-spec", ODOO_PYTHON_SYNC_SKIP_ADDONS = "stale_skip" }""",
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            subprocess.run(["git", "add", "workspace.toml"], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload(
                include_deployment_secrets=False,
                environment={
                    "ODOO_VERSION": "20.0",
                    "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/example/runtime:20.0-runtime",
                    "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/example/devtools:20.0-devtools",
                    "ODOO_ADDON_REPOSITORIES": ("cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"),
                    "OPENUPGRADE_ADDON_REPOSITORY": ("OCA/OpenUpgrade@411f6b8e85cac72dc7aa2e2dc5540001043c327d"),
                    "OPENUPGRADELIB_INSTALL_SPEC": (
                        "git+https://github.com/OCA/openupgradelib.git@89e649728027a8ab656b3aa4be18f4bd364db417"
                    ),
                    "ODOO_PYTHON_SYNC_SKIP_ADDONS": "",
                },
            )

            captured_build_args: list[str] = []
            captured_commands: list[list[str]] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = runtime_repo_path, environment_overrides, allowed_return_codes
                captured_commands.append(command)
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_args.extend(command)

            exact_ref = "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"
            with mock.patch(
                "odoo_devkit.local_runtime.load_environment_from_explicit_payload",
                side_effect=self.explicit_payload_loader,
            ):
                with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images"):
                    with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_image_push"):
                        with mock.patch("odoo_devkit.local_runtime.run_command", side_effect=fake_run_command):
                            with mock.patch(
                                "odoo_devkit.local_runtime.resolve_base_image_provenance",
                                side_effect=self._resolve_base_image_provenance_fixture,
                            ):
                                payload = run_native_runtime_publish(
                                    manifest=manifest,
                                    image_repository="ghcr.io/example/cm-runtime",
                                    image_tag="cm-20260416-abcdef",
                                    output_file=None,
                                    no_cache=False,
                                )

            addon_build_arg = next(argument for argument in captured_build_args if argument.startswith("ODOO_ADDON_REPOSITORIES="))
            self.assertEqual(addon_build_arg, f"ODOO_ADDON_REPOSITORIES={exact_ref}")
            expected_build_args = {
                "ODOO_VERSION=20.0",
                "ODOO_BASE_RUNTIME_IMAGE=ghcr.io/example/runtime@sha256:" + "2" * 64,
                "ODOO_BASE_DEVTOOLS_IMAGE=ghcr.io/example/devtools@sha256:" + "3" * 64,
                "OPENUPGRADE_ADDON_REPOSITORY=OCA/OpenUpgrade@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
            }
            self.assertTrue(expected_build_args.issubset(set(captured_build_args)))
            self.assertFalse(any(argument.startswith("OPENUPGRADELIB_INSTALL_SPEC=") for argument in captured_build_args))
            self.assertFalse(any(argument.startswith("ODOO_PYTHON_SYNC_SKIP_ADDONS=") for argument in captured_build_args))
            self.assertIn(
                {"repository": "cbusillo/disable_odoo_online", "ref": exact_ref.rsplit("@", 1)[1]},
                payload["addon_sources"],
            )
            self.assertEqual(
                payload["odoo_install_modules"],
                ["launchplane_settings", "disable_odoo_online", "opw_custom"],
            )
            self.assertEqual(payload["build_flags"]["values"]["odoo_version"], "20.0")
            self.assertEqual(payload["build_flags"]["addon_skip_flags"], [])
            preflight_command = next(command for command in captured_commands if command[:2] == ["docker", "run"])
            build_command = next(command for command in captured_commands if command[:3] == ["docker", "buildx", "build"])
            self.assertLess(captured_commands.index(preflight_command), captured_commands.index(build_command))
            self.assertIn("ghcr.io/example/runtime@sha256:" + "2" * 64, preflight_command)
            self.assertIn("linux/amd64", preflight_command)
            self.assertTrue(any(value.endswith(":/opt/runtime:ro") for value in preflight_command))
            self.assertTrue(any(value.endswith(":/opt/project:ro") for value in preflight_command))

    def test_base_runtime_dependency_preflight_wraps_platform_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            support_root = temporary_root / "runtime"
            tenant_root = temporary_root / "project"
            support_root.mkdir()
            tenant_root.mkdir()

            with mock.patch(
                "odoo_devkit.local_runtime.run_command",
                side_effect=local_runtime.RuntimeCommandError("Command failed (1): docker run"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "Base runtime dependency preflight failed for linux/amd64; see resolver diagnostics above",
                ):
                    local_runtime.require_base_runtime_dependency_compatibility(
                        base_runtime_image="ghcr.io/example/runtime@sha256:" + "2" * 64,
                        staged_support_root=support_root,
                        staged_tenant_root=tenant_root,
                        platforms=("linux/amd64",),
                        build_environment={},
                    )

    def test_native_runtime_publish_requires_explicit_payload_for_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "cm_website").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "cm_website" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant website"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                addons_paths=("addons",),
                context_name="cm_website",
                database_name="cm_website_testing",
                instance_name="testing",
            )
            subprocess.run(["git", "add", "workspace.toml"], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch.dict(os.environ, {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: ""}):
                with self.assertRaisesRegex(
                    ValueError,
                    "Non-local artifact publish requires Launchplane runtime environment payload",
                ):
                    run_native_runtime_publish(
                        manifest=manifest,
                        image_repository="ghcr.io/example/cm-website-runtime",
                        image_tag="cm_website-20260606-abcdef",
                        output_file=None,
                        no_cache=False,
                        platforms=("linux/amd64",),
                    )

    def test_native_runtime_publish_rejects_payload_without_odoo_version_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)
            self._configure_publish_runtime_payload(odoo_version=None)

            with mock.patch("odoo_devkit.local_runtime.run_command") as run_command_mock:
                with self.assertRaisesRegex(ValueError, "environment must include ODOO_VERSION"):
                    run_native_runtime_publish(
                        manifest=manifest,
                        image_repository="ghcr.io/example/opw-runtime",
                        image_tag="opw-20260416-abcdef",
                        output_file=None,
                        no_cache=False,
                    )

            run_command_mock.assert_not_called()

    def test_native_runtime_publish_synthesizes_context_from_explicit_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = self._create_git_repo(temp_root / "tenant-repo")
            runtime_repo_path = self._create_git_repo(temp_root / "runtime-repo")
            (tenant_repo_path / "addons" / "cm_website").mkdir(parents=True, exist_ok=True)
            (tenant_repo_path / "addons" / "cm_website" / "__manifest__.py").write_text("{}\n", encoding="utf-8")
            self._write_tenant_dependency_workspace(tenant_repo_path, addon_names=("cm_website",))
            (tenant_repo_path / "website-bootstrap.toml").write_text(
                """
schema_version = 1
tenant = "cm_website"

[odoo]
install_modules = ["cm_website"]

[website]
name = "Cell Mechanic"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=tenant_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "tenant website"], cwd=tenant_repo_path, check=True, capture_output=True)
            self._write_runtime_repo(runtime_repo_path)
            subprocess.run(["git", "add", "."], cwd=runtime_repo_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "runtime files"], cwd=runtime_repo_path, check=True, capture_output=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                addons_paths=("addons",),
                context_name="cm_website",
                database_name="cm_website_testing",
                instance_name="testing",
            )
            (tenant_repo_path / "artifact-inputs.toml").write_text(
                """
schema_version = 1
sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "main" },
]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "workspace.toml", "artifact-inputs.toml"],
                cwd=tenant_repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "commit", "-m", "workspace manifest"], cwd=tenant_repo_path, check=True, capture_output=True)
            manifest = load_workspace_manifest(manifest_path)

            captured_build_args: list[str] = []

            def fake_run_command(
                *,
                runtime_repo_path: Path,
                command: list[str],
                environment_overrides: object | None = None,
                allowed_return_codes: object | None = None,
            ) -> None:
                _ = runtime_repo_path, environment_overrides, allowed_return_codes
                if command[:3] == ["docker", "buildx", "build"]:
                    self._write_artifact_build_outputs_for_command(command)
                if "--metadata-file" in command:
                    captured_build_args.extend(command)

            explicit_payload = {
                "context": "cm_website",
                "instance": "testing",
                "environment": {
                    "ODOO_VERSION": "19.0",
                    "ODOO_MASTER_PASSWORD": "runtime-payload-master",
                    "ODOO_DB_USER": "odoo",
                    "ODOO_DB_PASSWORD": "runtime-payload-database",
                    "GITHUB_TOKEN": "gh-token",
                    "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/example/runtime:19.0-runtime",
                    "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/example/devtools:19.0-devtools",
                },
            }
            with mock.patch.dict(
                os.environ,
                {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: json.dumps(explicit_payload)},
            ):
                with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_base_images"):
                    with mock.patch("odoo_devkit.local_runtime.ensure_registry_auth_for_image_push"):
                        with mock.patch("odoo_devkit.local_runtime.run_command", side_effect=fake_run_command):
                            with mock.patch(
                                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                                return_value="411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                            ):
                                with mock.patch(
                                    "odoo_devkit.local_runtime.resolve_base_image_provenance",
                                    side_effect=self._resolve_base_image_provenance_fixture,
                                ):
                                    payload = run_native_runtime_publish(
                                        manifest=manifest,
                                        image_repository="ghcr.io/example/cm-website-runtime",
                                        image_tag="cm_website-20260606-abcdef",
                                        output_file=None,
                                        no_cache=False,
                                        platforms=("linux/amd64",),
                                    )

            self.assertTrue(payload["artifact_id"].startswith("artifact-cm_website-"))
            self.assertEqual(payload["image"]["repository"], "ghcr.io/example/cm-website-runtime")
            self.assertEqual(payload["image"]["tags"], ["cm_website-20260606-abcdef"])
            self.assertEqual(
                payload["odoo_install_modules"],
                ["launchplane_settings", "disable_odoo_online", "cm_website"],
            )
            self.assertIn(
                "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                captured_build_args,
            )

    def test_resolve_source_repository_ref_to_git_sha_rejects_missing_remote_ref(self) -> None:
        with mock.patch(
            "odoo_devkit.local_runtime.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ):
            with self.assertRaisesRegex(ValueError, "No remote ref matched"):
                local_runtime.resolve_source_repository_ref_to_git_sha(
                    repository="cbusillo/disable_odoo_online",
                    ref="main",
                )

    def test_resolve_source_repository_ref_to_git_sha_uses_credential_helper(self) -> None:
        with mock.patch(
            "odoo_devkit.local_runtime.subprocess.run",
            return_value=mock.Mock(
                returncode=0,
                stdout="411f6b8e85cac72dc7aa2e2dc5540001043c327d\trefs/heads/main\n",
                stderr="",
            ),
        ) as run_mock:
            resolved_ref = local_runtime.resolve_source_repository_ref_to_git_sha(
                repository="cbusillo/disable_odoo_online",
                ref="main",
                github_token="source-token",
            )

        self.assertEqual(resolved_ref, "411f6b8e85cac72dc7aa2e2dc5540001043c327d")
        execution_env = run_mock.call_args.kwargs["env"]
        self.assertEqual(execution_env["ODOO_DEVKIT_GITHUB_TOKEN"], "source-token")
        self.assertEqual(execution_env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(execution_env["GIT_CONFIG_KEY_0"], "credential.https://github.com.helper")
        self.assertEqual(
            execution_env["GIT_CONFIG_VALUE_0"],
            "!f() { echo username=x-access-token; echo password=$ODOO_DEVKIT_GITHUB_TOKEN; }; f",
        )
        self.assertEqual(execution_env["GIT_CONFIG_KEY_1"], "credential.useHttpPath")
        self.assertEqual(execution_env["GIT_CONFIG_VALUE_1"], "true")

    def test_resolve_artifact_runtime_source_refs_uses_environment_token_fallback(self) -> None:
        runtime_values = {
            "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@main",
        }
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}):
            with mock.patch(
                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                return_value="411f6b8e85cac72dc7aa2e2dc5540001043c327d",
            ) as resolve_ref_mock:
                resolved_values, selector_metadata = local_runtime.resolve_artifact_runtime_source_repository_refs(
                    runtime_values=runtime_values
                )

        resolve_ref_mock.assert_called_once_with(
            repository="cbusillo/disable_odoo_online",
            ref="main",
            github_token="env-token",
        )
        self.assertEqual(
            resolved_values["ODOO_ADDON_REPOSITORIES"],
            "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
        )
        self.assertEqual(
            selector_metadata,
            (
                {
                    "repository": "cbusillo/disable_odoo_online",
                    "selector": "main",
                    "resolved_ref": "411f6b8e85cac72dc7aa2e2dc5540001043c327d",
                },
            ),
        )

    def test_resolve_artifact_runtime_source_refs_uses_dedicated_source_token_env(self) -> None:
        runtime_values = {
            "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@main",
        }
        with mock.patch.dict(
            os.environ,
            {
                "ODOO_DEVKIT_SOURCE_GITHUB_TOKEN": "source-env-token",
                "GITHUB_TOKEN": "github-env-token",
            },
        ):
            with mock.patch(
                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                return_value="411f6b8e85cac72dc7aa2e2dc5540001043c327d",
            ) as resolve_ref_mock:
                local_runtime.resolve_artifact_runtime_source_repository_refs(runtime_values=runtime_values)

        resolve_ref_mock.assert_called_once_with(
            repository="cbusillo/disable_odoo_online",
            ref="main",
            github_token="source-env-token",
        )

    def test_resolve_artifact_runtime_source_refs_supports_ci_source_token_env(self) -> None:
        runtime_values = {
            "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@main",
        }
        with mock.patch.dict(
            os.environ,
            {
                "ODOO_SOURCE_GITHUB_TOKEN": "ci-source-token",
                "GITHUB_TOKEN": "github-env-token",
            },
        ):
            with mock.patch(
                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                return_value="411f6b8e85cac72dc7aa2e2dc5540001043c327d",
            ) as resolve_ref_mock:
                local_runtime.resolve_artifact_runtime_source_repository_refs(runtime_values=runtime_values)

        resolve_ref_mock.assert_called_once_with(
            repository="cbusillo/disable_odoo_online",
            ref="main",
            github_token="ci-source-token",
        )

    def test_resolve_artifact_runtime_source_refs_prefers_runtime_github_token(self) -> None:
        runtime_values = {
            "GITHUB_TOKEN": "source-token",
            "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@main",
        }
        with mock.patch.dict(os.environ, {"GHCR_TOKEN": "package-token", "GITHUB_TOKEN": "env-token"}):
            with mock.patch(
                "odoo_devkit.local_runtime.resolve_source_repository_ref_to_git_sha",
                return_value="411f6b8e85cac72dc7aa2e2dc5540001043c327d",
            ) as resolve_ref_mock:
                local_runtime.resolve_artifact_runtime_source_repository_refs(runtime_values=runtime_values)

        resolve_ref_mock.assert_called_once_with(
            repository="cbusillo/disable_odoo_online",
            ref="main",
            github_token="source-token",
        )

    def test_resolve_source_repository_ref_to_git_sha_rejects_ambiguous_matches(self) -> None:
        ambiguous_stdout = (
            "1111111111111111111111111111111111111111\trefs/heads/main\n2222222222222222222222222222222222222222\trefs/tags/main\n"
        )
        with mock.patch(
            "odoo_devkit.local_runtime.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=ambiguous_stdout, stderr=""),
        ):
            with self.assertRaisesRegex(ValueError, "resolve unambiguously"):
                local_runtime.resolve_source_repository_ref_to_git_sha(
                    repository="cbusillo/disable_odoo_online",
                    ref="main",
                )

    def test_native_runtime_down_runs_compose_down_with_optional_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)

            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_down(manifest=manifest, volumes=True)

            self.assertEqual(exit_code, 0)
            command = run_mock.call_args.kwargs.get("args", run_mock.call_args.args[0])
            self.assertEqual(command[-2:], ["down", "--volumes"])
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())

    def test_native_runtime_down_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_down(manifest=manifest, volumes=False)

    def test_native_runtime_workflow_runs_init_natively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="init")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertIn([*commands[0][:-2], "stop", "web"], commands)
            self.assertTrue(any(command[-3:] == ["up", "-d", "script-runner"] for command in commands))
            self.assertTrue(any("/odoo/odoo-bin" in command for command in commands))
            self.assertTrue(any(command[-3:] == ["up", "-d", "web"] for command in commands))

    def test_native_runtime_workflow_runs_openupgrade_natively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="openupgrade")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertTrue(any(command[-3:] == ["up", "-d", "script-runner"] for command in commands))
            self.assertTrue(any("/volumes/scripts/run_openupgrade.py" in command for command in commands))
            self.assertTrue(any(command[-3:] == ["up", "-d", "web"] for command in commands))

    def test_native_runtime_workflow_runs_update_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="update")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            self.assertTrue(any(command[-2:] == ["build", "web"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "database"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "script-runner"] for command in commands))
            update_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertIn("UPDATE_ONLY=1", update_command)
            self.assertIn("--update-only", update_command)

    def test_native_runtime_workflow_runs_bootstrap_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_workflow(manifest=manifest, workflow="bootstrap")

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            bootstrap_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertIn("BOOTSTRAP=1", bootstrap_command)
            self.assertIn("--bootstrap", bootstrap_command)

    def test_native_runtime_restore_runs_natively_for_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = self._runtime_data_workflow_side_effect()
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_restore(manifest=manifest)

            self.assertEqual(exit_code, 0)
            commands = [call.kwargs.get("args", call.args[0]) for call in run_mock.call_args_list]
            restore_command = next(command for command in commands if "/volumes/scripts/run_odoo_data_workflows.py" in command)
            self.assertNotIn("UPDATE_ONLY=1", restore_command)
            self.assertNotIn("BOOTSTRAP=1", restore_command)
            self.assertTrue(any(command[-2:] == ["stop", "web"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--remove-orphans", "web"] for command in commands))

    def test_native_runtime_odoo_shell_executes_script_runner_with_script_and_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            script_path = tenant_repo_path / "tmp" / "shell-script.py"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("print('hello from shell')\n", encoding="utf-8")
            log_file_path = tenant_repo_path / "tmp" / "odoo-shell.log"

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = run_native_runtime_odoo_shell(
                        manifest=manifest,
                        service="script-runner",
                        database_name="opw-alt",
                        script_path=script_path,
                        log_file=log_file_path,
                        dry_run=False,
                    )

            self.assertEqual(exit_code, 0)
            command = run_mock.call_args.kwargs.get("args", run_mock.call_args.args[0])
            self.assertIn("exec", command)
            self.assertIn("-T", command)
            self.assertIn("script-runner", command)
            self.assertIn("shell", command)
            self.assertIn("opw-alt", command)
            self.assertEqual(run_mock.call_args.kwargs["cwd"], runtime_repo_path.resolve())
            self.assertEqual(run_mock.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(run_mock.call_args.kwargs["input"], b"print('hello from shell')\n")
            self.assertTrue(log_file_path.parent.exists())

    def test_native_runtime_odoo_shell_dry_run_prints_redirects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            self._write_runtime_repo(runtime_repo_path)
            manifest_path = self._write_manifest(tenant_repo_path=tenant_repo_path, runtime_repo_path=runtime_repo_path)
            manifest = load_workspace_manifest(manifest_path)
            with contextlib.redirect_stdout(io.StringIO()):
                run_native_runtime_select(manifest=manifest)

            script_path = tenant_repo_path / "tmp" / "shell-script.py"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("print('hello from shell')\n", encoding="utf-8")
            log_file_path = tenant_repo_path / "tmp" / "odoo-shell.log"

            with mock.patch("odoo_devkit.local_runtime.subprocess.run") as run_mock:
                output_buffer = io.StringIO()
                with contextlib.redirect_stdout(output_buffer):
                    exit_code = run_native_runtime_odoo_shell(
                        manifest=manifest,
                        service="script-runner",
                        database_name=None,
                        script_path=script_path,
                        log_file=log_file_path,
                        dry_run=True,
                    )

            self.assertEqual(exit_code, 0)
            run_mock.assert_not_called()
            output = output_buffer.getvalue()
            self.assertIn("<", output)
            self.assertIn(">", output)
            self.assertIn("odoo-shell.log", output)

    def test_native_runtime_restore_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "belongs in Launchplane"):
                run_native_runtime_restore(manifest=manifest)

    def test_native_runtime_workflow_rejects_non_local_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "belongs in Launchplane"):
                run_native_runtime_workflow(manifest=manifest, workflow="update")

    def test_native_runtime_workflow_rejects_non_local_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="prod",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "belongs in Launchplane"):
                run_native_runtime_workflow(manifest=manifest, workflow="bootstrap")

    def test_native_runtime_logs_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.stream_runtime_logs") as stream_runtime_logs:
                exit_code = run_native_runtime_logs(manifest=manifest, service="script-runner", tail_lines=25, follow=False)

            self.assertEqual(exit_code, 0)
            stream_runtime_logs.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                service="script-runner",
                tail_lines=25,
                follow=False,
            )

    def test_native_runtime_logs_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_logs(manifest=manifest, service="web", tail_lines=200, follow=True)

    def test_native_runtime_psql_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_psql_command") as run_psql_command:
                exit_code = run_native_runtime_psql(manifest=manifest, psql_arguments=("-c", "select 1"))

            self.assertEqual(exit_code, 0)
            run_psql_command.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                psql_arguments=("-c", "select 1"),
            )

    def test_native_runtime_psql_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="prod",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_psql(manifest=manifest, psql_arguments=())

    def test_native_runtime_odoo_shell_runs_local_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            manifest = load_workspace_manifest(manifest_path)

            with mock.patch("odoo_devkit.runtime.run_odoo_shell_command") as run_odoo_shell_command:
                exit_code = run_native_runtime_odoo_shell(
                    manifest=manifest,
                    service="script-runner",
                    database_name="opw-alt",
                    script_path=Path("tmp/script.py"),
                    log_file=Path("tmp/odoo-shell.log"),
                    dry_run=True,
                )

            self.assertEqual(exit_code, 0)
            run_odoo_shell_command.assert_called_once_with(
                manifest=manifest,
                runtime_repo_path=runtime_repo_path.resolve(),
                service="script-runner",
                database_name="opw-alt",
                script_path=Path("tmp/script.py"),
                log_file=Path("tmp/odoo-shell.log"),
                dry_run=True,
            )

    def test_native_runtime_odoo_shell_rejects_non_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_odoo_shell(
                    manifest=manifest,
                    service="script-runner",
                    database_name=None,
                    script_path=None,
                    log_file=None,
                    dry_run=False,
                )

    def test_cli_runtime_restore_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(manifest=manifest_path, runtime_instance="testing")

            with mock.patch("odoo_devkit.cli.run_native_runtime_restore", return_value=0) as runtime_restore:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_restore(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            overridden_manifest = runtime_restore.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.context, "opw")
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_logs_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                service="web",
                lines=50,
                follow=False,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_logs", return_value=0) as runtime_logs:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_logs(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_logs.call_args.kwargs["service"], "web")
            self.assertEqual(runtime_logs.call_args.kwargs["tail_lines"], 50)
            self.assertFalse(runtime_logs.call_args.kwargs["follow"])
            overridden_manifest = runtime_logs.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_down_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                volumes=True,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_down", return_value=0) as runtime_down:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_down(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertTrue(runtime_down.call_args.kwargs["volumes"])
            overridden_manifest = runtime_down.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_build_supports_instance_override_against_local_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                no_cache=True,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_build", return_value=0) as runtime_build:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_build(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertTrue(runtime_build.call_args.kwargs["no_cache"])
            overridden_manifest = runtime_build.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_psql_strips_leading_separator_and_supports_instance_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                psql_arguments=["--", "-c", "select 1"],
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_psql", return_value=0) as runtime_psql:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_psql(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_psql.call_args.kwargs["psql_arguments"], ("-c", "select 1"))
            overridden_manifest = runtime_psql.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_cli_runtime_odoo_shell_supports_instance_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="local",
            )
            arguments = argparse.Namespace(
                manifest=manifest_path,
                runtime_instance="testing",
                script_path=Path("tmp/script.py"),
                service="script-runner",
                database_name="opw-alt",
                log_file=Path("tmp/odoo-shell.log"),
                dry_run=True,
            )

            with mock.patch("odoo_devkit.cli.run_native_runtime_odoo_shell", return_value=0) as runtime_odoo_shell:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_odoo_shell(arguments)

            self.assertEqual(captured_exit.exception.code, 0)
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["service"], "script-runner")
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["database_name"], "opw-alt")
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["script_path"], Path("tmp/script.py"))
            self.assertEqual(runtime_odoo_shell.call_args.kwargs["log_file"], Path("tmp/odoo-shell.log"))
            self.assertTrue(runtime_odoo_shell.call_args.kwargs["dry_run"])
            overridden_manifest = runtime_odoo_shell.call_args.kwargs["manifest"]
            self.assertEqual(overridden_manifest.runtime.instance, "testing")

    def test_native_runtime_workflow_rejects_non_local_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="dev",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_workflow(manifest=manifest, workflow="init")

    def test_native_runtime_workflow_rejects_non_local_openupgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            manifest = load_workspace_manifest(manifest_path)

            with self.assertRaisesRegex(ValueError, "requires --instance local"):
                run_native_runtime_workflow(manifest=manifest, workflow="openupgrade")

    def test_cli_runtime_workflow_rejects_non_local_mutations_without_platform_fallback(self) -> None:
        workflow_cases = (
            ("init", "dev"),
            ("openupgrade", "testing"),
            ("bootstrap", "prod"),
            ("update", "testing"),
        )
        for workflow_name, instance_name in workflow_cases:
            with self.subTest(workflow=workflow_name, instance=instance_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temp_root = Path(temporary_directory)
                    tenant_repo_path = temp_root / "tenant-repo"
                    runtime_repo_path = temp_root / "runtime-repo"
                    tenant_repo_path.mkdir(parents=True, exist_ok=True)
                    runtime_repo_path.mkdir(parents=True, exist_ok=True)
                    manifest_path = self._write_manifest(
                        tenant_repo_path=tenant_repo_path,
                        runtime_repo_path=runtime_repo_path,
                        instance_name=instance_name,
                    )
                    arguments = argparse.Namespace(manifest=manifest_path, workflow=workflow_name)

                    with mock.patch("odoo_devkit.cli.run_runtime_platform_command") as platform_command:
                        with self.assertRaises(SystemExit) as captured_exit:
                            _handle_runtime_workflow(arguments)

                    self.assertIsInstance(captured_exit.exception.code, str)
                    self.assertTrue(
                        "requires --instance local" in str(captured_exit.exception.code)
                        or "belongs in Launchplane" in str(captured_exit.exception.code)
                    )
                    platform_command.assert_not_called()

    def test_cli_runtime_workflow_rejects_unknown_workflows_without_platform_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
            )
            arguments = argparse.Namespace(manifest=manifest_path, workflow="custom-remote-flow")

            with mock.patch("odoo_devkit.cli.run_runtime_platform_command") as platform_command:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_workflow(arguments)

            self.assertIsInstance(captured_exit.exception.code, str)
            self.assertIn("Unsupported runtime workflow", str(captured_exit.exception.code))
            platform_command.assert_not_called()

    def test_cli_runtime_restore_rejects_non_local_without_platform_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            tenant_repo_path = temp_root / "tenant-repo"
            runtime_repo_path = temp_root / "runtime-repo"
            tenant_repo_path.mkdir(parents=True, exist_ok=True)
            runtime_repo_path.mkdir(parents=True, exist_ok=True)
            manifest_path = self._write_manifest(
                tenant_repo_path=tenant_repo_path,
                runtime_repo_path=runtime_repo_path,
                instance_name="testing",
            )
            arguments = argparse.Namespace(manifest=manifest_path, runtime_instance=None)

            with mock.patch("odoo_devkit.cli.run_runtime_platform_command") as platform_command:
                with self.assertRaises(SystemExit) as captured_exit:
                    _handle_runtime_restore(arguments)

            self.assertIsInstance(captured_exit.exception.code, str)
            self.assertIn("belongs in Launchplane", str(captured_exit.exception.code))
            platform_command.assert_not_called()

    @staticmethod
    def _runtime_data_workflow_side_effect() -> mock.Mock:
        def run_side_effect(*args: object, **kwargs: object) -> mock.Mock:
            command = kwargs.get("args") or args[0]
            assert isinstance(command, list)
            if command[-3:] == ["ps", "-q", "database"]:
                return mock.Mock(returncode=0, stdout="database-container\n")
            if command[-3:] == ["ps", "-q", "script-runner"]:
                return mock.Mock(returncode=0, stdout="script-runner-container\n")
            if command[:3] == ["docker", "inspect", "-f"]:
                return mock.Mock(returncode=0, stdout="running\n")
            return mock.Mock(returncode=0, stdout="")

        return run_side_effect

    def _load_environment_from_explicit_payload(
        self,
        *,
        raw_payload: str,
        context_name: str,
        instance_name: str,
    ) -> local_runtime.LoadedEnvironment:
        if raw_payload != "{}":
            return self.explicit_payload_loader(
                raw_payload=raw_payload,
                context_name=context_name,
                instance_name=instance_name,
            )
        return local_runtime.LoadedEnvironment(
            env_file_path=Path(".generated") / "runtime-env" / f"{context_name}.{instance_name}.env",
            merged_values={
                "ODOO_MASTER_PASSWORD": "runtime-payload-master",
                "ODOO_DB_USER": "odoo",
                "ODOO_DB_PASSWORD": "runtime-payload-database",
                "ODOO_UPSTREAM_HOST": "example.internal",
                "ODOO_UPSTREAM_USER": "odoo",
                "ODOO_UPSTREAM_DB_NAME": "opw-source",
                "ODOO_UPSTREAM_DB_USER": "odoo",
                "ODOO_UPSTREAM_FILESTORE_PATH": "/srv/odoo/filestore/opw-source",
                "GITHUB_TOKEN": "gh-token",
                "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/example/runtime:19.0-runtime",
                "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/example/devtools:19.0-devtools",
            },
            collisions=(),
        )

    @staticmethod
    def _write_manifest(
        *,
        tenant_repo_path: Path,
        runtime_repo_path: Path,
        shared_addons_repo_path: Path | None = None,
        addons_paths: tuple[str, ...] = ("sources/tenant/addons",),
        context_name: str = "opw",
        database_name: str = "opw",
        instance_name: str = "local",
        artifact_inputs_file: str | None = None,
    ) -> Path:
        manifest_path = tenant_repo_path / "workspace.toml"
        (tenant_repo_path / "addons").mkdir(parents=True, exist_ok=True)
        shared_addons_block = ""
        artifacts_block = ""
        if shared_addons_repo_path is not None:
            shared_addons_block = f"""

[repos.shared_addons]
name = "shared-addons-repo"
path = "{shared_addons_repo_path}"
"""
        if artifact_inputs_file is not None:
            artifacts_block = f"""

[artifacts]
inputs_file = "{artifact_inputs_file}"
"""
        rendered_addons_paths = ", ".join(f'"{addons_path}"' for addons_path in addons_paths)
        manifest_path.write_text(
            f"""
schema_version = 1
tenant = "{context_name}"

[workspace]
name = "{context_name}"
python = "3.13"

[repos.tenant]
name = "tenant-repo"
path = "."

[repos.runtime]
name = "runtime-repo"
path = "{runtime_repo_path}"
{shared_addons_block}

[runtime]
context = "{context_name}"
instance = "{instance_name}"
database = "{database_name}"
addons_paths = [{rendered_addons_paths}]
{artifacts_block}

[ide]
mode = "tenant_repo"
focus_paths = ["addons/opw_custom"]
attached_paths = ["sources/devkit"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return manifest_path

    @staticmethod
    def _write_runtime_repo(runtime_repo_path: Path) -> None:
        (runtime_repo_path / "platform" / "compose").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "platform" / "config").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "docker" / "runtime-python").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "addons" / "shared").mkdir(parents=True, exist_ok=True)
        (runtime_repo_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (runtime_repo_path / "platform" / "compose" / "base.yaml").write_text("services: {}\n", encoding="utf-8")
        (runtime_repo_path / "docker" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (runtime_repo_path / "docker" / "artifact.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (runtime_repo_path / "docker" / "dependency-evidence.Dockerfile").write_text(
            "ARG ARTIFACT_IMAGE\nFROM ${ARTIFACT_IMAGE} AS artifact\nFROM scratch\n",
            encoding="utf-8",
        )
        (runtime_repo_path / "docker" / "runtime-python" / "pyproject.toml").write_text(
            "[project]\n"
            'name = "runtime-support"\n'
            'version = "0.0.0"\n'
            'requires-python = ">=3.13"\n'
            'dependencies = ["hatchling==1.27.0"]\n\n'
            "[tool.uv]\n"
            "package = false\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["uv", "lock", "--project", str(runtime_repo_path / "docker" / "runtime-python")],
            check=True,
            capture_output=True,
        )
        (runtime_repo_path / "platform" / "config" / "odoo.conf").write_text("[options]\n", encoding="utf-8")
        (runtime_repo_path / "pyproject.toml").write_text("[project]\nname='runtime-repo'\nversion='0.1.0'\n", encoding="utf-8")
        (runtime_repo_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (runtime_repo_path / "platform" / "stack.toml").write_text(
            """
schema_version = 1
odoo_version = "19.0"
addons_path = ["/odoo/addons", "/opt/launchplane/addons", "/opt/project/addons"]
required_env_keys = ["ODOO_MASTER_PASSWORD", "ODOO_DB_USER", "ODOO_DB_PASSWORD"]

[contexts.opw]
database = "opw"
install_modules = ["opw_custom"]

[contexts.opw.instances.local]

[contexts.opw.instances.dev]

[contexts.opw.instances.testing]

[contexts.opw.instances.prod]
""".strip()
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_tenant_dependency_workspace(tenant_repo_path: Path, *, addon_names: tuple[str, ...]) -> None:
        for addon_name in addon_names:
            addon_root = tenant_repo_path / "addons" / addon_name
            addon_root.mkdir(parents=True, exist_ok=True)
            (addon_root / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["hatchling==1.27.0"]\n'
                'build-backend = "hatchling.build"\n\n'
                "[project]\n"
                f'name = "{addon_name}"\n'
                'version = "0.0.0"\n'
                "dependencies = []\n\n"
                "[tool.uv]\n"
                "package = false\n",
                encoding="utf-8",
            )
        members = ", ".join(json.dumps(f"addons/{addon_name}") for addon_name in addon_names)
        (tenant_repo_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "tenant-dependencies"\n'
            'version = "0.0.0"\n'
            "dependencies = []\n\n"
            "[tool.uv]\n"
            "package = false\n\n"
            "[tool.uv.workspace]\n"
            f"members = [{members}]\n",
            encoding="utf-8",
        )
        subprocess.run(["uv", "lock", "--project", str(tenant_repo_path)], check=True, capture_output=True)

    @staticmethod
    def _initialize_git_repository(repo_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Code"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "code@example.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", f"git@github.com:example/{repo_path.name}.git"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, check=True, capture_output=True)
        return repo_path

    def _create_git_repo(self, repo_path: Path) -> Path:
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / "README.md").write_text(f"# {repo_path.name}\n", encoding="utf-8")
        return self._initialize_git_repository(repo_path)


if __name__ == "__main__":
    unittest.main()
