from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from odoo_devkit import remote_runtime


def _sample_remote_target_definition() -> remote_runtime.DokployTargetDefinition:
    return remote_runtime.DokployTargetDefinition(
        context="opw",
        instance="testing",
        target_id="compose-1",
        target_name="opw-testing",
        deploy_timeout_seconds=7200,
    )


def _sample_runtime_context(*, repo_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=repo_root,
        selection=SimpleNamespace(context_name="opw", instance_name="testing"),
    )


class RemoteRuntimeTests(unittest.TestCase):
    def test_load_dokploy_source_of_truth_reads_control_plane_catalog_with_target_id_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "runtime-repo"
            control_plane_root = temp_root / "odoo-control-plane"
            (control_plane_root / "config").mkdir(parents=True, exist_ok=True)
            (control_plane_root / "config" / "dokploy.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "testing"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (control_plane_root / "config" / "dokploy-targets.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "testing"
target_id = "control-plane-compose"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ODOO_CONTROL_PLANE_ROOT": str(control_plane_root)}):
                source_of_truth = remote_runtime.load_dokploy_source_of_truth(repo_root)

        assert source_of_truth is not None
        self.assertEqual(source_of_truth.targets[0].target_id, "control-plane-compose")

    def test_load_dokploy_source_of_truth_requires_control_plane_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory)
            platform_directory = repo_root / "platform"
            platform_directory.mkdir(parents=True, exist_ok=True)
            (platform_directory / "dokploy.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "testing"
target_id = "legacy-compose"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                source_of_truth = remote_runtime.load_dokploy_source_of_truth(repo_root)

        self.assertIsNone(source_of_truth)

    def test_load_dokploy_source_of_truth_applies_profile_and_project_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "runtime-repo"
            control_plane_root = temp_root / "odoo-control-plane"
            control_plane_config_directory = control_plane_root / "config"
            control_plane_config_directory.mkdir(parents=True, exist_ok=True)
            (control_plane_config_directory / "dokploy.toml").write_text(
                """
schema_version = 1

[defaults]
target_type = "compose"
deploy_timeout_seconds = 7200

[projects]
shared = "shared-project"

[profiles.testing]
project = "shared"
healthcheck_enabled = false

[[targets]]
context = "opw"
instance = "testing"
profile = "testing"
target_name = "opw-testing"
domains = ["testing.example.com"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (control_plane_config_directory / "dokploy-targets.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ODOO_CONTROL_PLANE_ROOT": str(control_plane_root)}):
                source_of_truth = remote_runtime.load_dokploy_source_of_truth(repo_root)

        assert source_of_truth is not None
        self.assertEqual(source_of_truth.schema_version, 1)
        self.assertEqual(len(source_of_truth.targets), 1)
        target_definition = source_of_truth.targets[0]
        self.assertEqual(target_definition.project_name, "shared-project")
        self.assertEqual(target_definition.target_type, "compose")
        self.assertEqual(target_definition.deploy_timeout_seconds, 7200)
        self.assertFalse(target_definition.healthcheck_enabled)
        self.assertEqual(target_definition.domains, ("testing.example.com",))

    def test_load_dokploy_source_of_truth_rejects_unknown_target_id_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            repo_root = temp_root / "runtime-repo"
            control_plane_root = temp_root / "odoo-control-plane"
            control_plane_config_directory = control_plane_root / "config"
            control_plane_config_directory.mkdir(parents=True, exist_ok=True)
            (control_plane_config_directory / "dokploy.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "testing"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (control_plane_config_directory / "dokploy-targets.toml").write_text(
                """
schema_version = 1

[[targets]]
context = "cm"
instance = "prod"
target_id = "compose-456"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ODOO_CONTROL_PLANE_ROOT": str(control_plane_root)}):
                with self.assertRaisesRegex(
                    remote_runtime.RuntimeCommandError,
                    r"route\(s\) that are not present in the control-plane route catalog: cm/prod",
                ):
                    remote_runtime.load_dokploy_source_of_truth(repo_root)

    def test_build_dokploy_data_workflow_script_includes_project_labels_and_flags(self) -> None:
        schedule_script = remote_runtime._build_dokploy_data_workflow_script(
            compose_app_name="compose-opw-testing-abc123",
            database_name="opw-testing",
            bootstrap=True,
            no_sanitize=True,
            update_only=False,
            clear_stale_lock=True,
            data_workflow_lock_path="/volumes/data/.data_workflow_in_progress",
        )

        self.assertIn("com.docker.compose.project=${compose_project}", schedule_script)
        self.assertIn('script_runner_container_id=$(resolve_container_id "script-runner")', schedule_script)
        self.assertIn("--bootstrap", schedule_script)
        self.assertIn("--no-sanitize", schedule_script)
        self.assertIn("Clearing stale data workflow lock ${data_workflow_lock_path}", schedule_script)
        self.assertIn("Normalizing filestore ownership for ${database_name}", schedule_script)
        self.assertIn("workflow_ssh_dir=/tmp/platform-data-workflow-ssh", schedule_script)

    def test_run_dokploy_managed_remote_data_workflow_upserts_and_runs_schedule(self) -> None:
        runtime_context = _sample_runtime_context(repo_root=Path("/tmp/repo"))
        target_definition = _sample_remote_target_definition()
        dokploy_request_calls: list[dict[str, object]] = []
        updated_target_env_calls: list[dict[str, object]] = []

        def record_dokploy_request(**kwargs: object) -> object:
            dokploy_request_calls.append(dict(kwargs))
            return True

        def record_target_env_update(**kwargs: object) -> None:
            updated_target_env_calls.append(dict(kwargs))

        with (
            patch.object(
                remote_runtime,
                "_resolve_required_dokploy_compose_target_definition",
                return_value=target_definition,
            ),
            patch.object(
                remote_runtime,
                "_resolve_dokploy_schedule_runtime",
                return_value=("dokploy-server", "user-123", "compose-opw-testing-abc123", None),
            ),
            patch.object(
                remote_runtime,
                "find_matching_dokploy_schedule",
                return_value={"deployments": [{"status": "cancelled"}]},
            ),
            patch.object(
                remote_runtime,
                "fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main,OCA/OpenUpgrade@19.0\n"
                    "OPENUPGRADE_ADDON_REPOSITORY=OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417\n"
                    "OPENUPGRADELIB_INSTALL_SPEC=git+https://github.com/OCA/openupgradelib.git@46d66ff5ed6a99481f84d3c79fc6e50835da7286",
                },
            ),
            patch.object(remote_runtime, "update_dokploy_target_env", side_effect=record_target_env_update),
            patch.object(
                remote_runtime,
                "upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ) as upsert_schedule,
            patch.object(
                remote_runtime,
                "latest_deployment_for_compose",
                return_value={"deploymentId": "compose-before-1", "status": "done"},
            ),
            patch.object(
                remote_runtime,
                "wait_for_dokploy_compose_deployment",
                return_value="deployment=compose-after-1 status=done",
            ),
            patch.object(
                remote_runtime,
                "latest_deployment_for_schedule",
                side_effect=[{"deploymentId": "before-1", "status": "done"}, {"deploymentId": "after-1", "status": "done"}],
            ),
            patch.object(remote_runtime, "wait_for_dokploy_schedule_deployment", return_value="deployment=after-1 status=done"),
            patch.object(remote_runtime, "dokploy_request", side_effect=record_dokploy_request),
        ):
            exit_code = remote_runtime._run_dokploy_managed_remote_data_workflow(
                runtime_context=runtime_context,
                env_values={
                    "DOKPLOY_HOST": "https://dokploy.example",
                    "DOKPLOY_TOKEN": "token",
                    "ODOO_DB_NAME": "opw",
                    "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@main,"
                    "OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417",
                    "OPENUPGRADE_ADDON_REPOSITORY": "OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417",
                    "OPENUPGRADELIB_INSTALL_SPEC": "git+https://github.com/OCA/openupgradelib.git@"
                    "46d66ff5ed6a99481f84d3c79fc6e50835da7286",
                },
                bootstrap=True,
                no_sanitize=True,
                update_only=False,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(updated_target_env_calls), 1)
        rendered_env_text = str(updated_target_env_calls[0]["env_text"])
        self.assertIn(
            "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main,OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417",
            rendered_env_text,
        )
        upsert_payload = upsert_schedule.call_args.kwargs["schedule_payload"]
        self.assertEqual(upsert_payload["scheduleType"], "dokploy-server")
        self.assertEqual(upsert_payload["userId"], "user-123")
        self.assertEqual(upsert_payload["enabled"], False)
        self.assertEqual(upsert_payload["timezone"], "UTC")
        self.assertIn("Clearing stale data workflow lock ${data_workflow_lock_path}", str(upsert_payload["script"]))
        self.assertIn("Normalizing filestore ownership for ${database_name}", str(upsert_payload["script"]))
        self.assertIn("--bootstrap", str(upsert_payload["script"]))
        self.assertIn("--no-sanitize", str(upsert_payload["script"]))
        self.assertEqual(
            dokploy_request_calls,
            [
                {
                    "host": "https://dokploy.example",
                    "token": "token",
                    "path": "/api/compose.deploy",
                    "method": "POST",
                    "payload": {"composeId": "compose-1"},
                    "timeout_seconds": 7200,
                },
                {
                    "host": "https://dokploy.example",
                    "token": "token",
                    "path": "/api/schedule.runManually",
                    "method": "POST",
                    "payload": {"scheduleId": "schedule-123"},
                    "timeout_seconds": 7200,
                },
            ],
        )

    def test_run_dokploy_managed_remote_data_workflow_requires_database_name(self) -> None:
        runtime_context = _sample_runtime_context(repo_root=Path("/tmp/repo"))
        target_definition = _sample_remote_target_definition()

        with (
            patch.object(
                remote_runtime,
                "_resolve_required_dokploy_compose_target_definition",
                return_value=target_definition,
            ),
            patch.object(
                remote_runtime,
                "_resolve_dokploy_schedule_runtime",
                return_value=("dokploy-server", "user-123", "compose-opw-testing-abc123", None),
            ),
            patch.object(remote_runtime, "_sync_dokploy_target_environment_and_deploy") as sync_target,
            patch.object(remote_runtime, "find_matching_dokploy_schedule", return_value=None),
        ):
            with self.assertRaisesRegex(remote_runtime.RuntimeCommandError, "requires ODOO_DB_NAME"):
                remote_runtime._run_dokploy_managed_remote_data_workflow(
                    runtime_context=runtime_context,
                    env_values={
                        "DOKPLOY_HOST": "https://dokploy.example",
                        "DOKPLOY_TOKEN": "token",
                    },
                    bootstrap=False,
                    no_sanitize=False,
                    update_only=False,
                )
        sync_target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
