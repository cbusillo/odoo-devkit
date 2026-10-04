from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import local_runtime


class RuntimeEnvSerializationTests(unittest.TestCase):
    values = {
        "DOLLARS": "synthetic-$ODK_REVIEW_EXPANSION ${ODK_REVIEW_EXPANSION} $$ #suffix",
        "QUOTES": "synthetic-'single' and \"double\"",
        "BACKSLASHES": "synthetic-\\n\\t\\'\\\"\\\\end\\",
        "WHITESPACE": " \tsynthetic value\t ",
        "MULTILINE": "synthetic\nnext\r\tline",
        "UNICODE": "synthetic-café-雪",
        "EMPTY": "",
        "PLAIN": "synthetic-value",
    }

    def test_generated_files_round_trip_without_expansion_or_value_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "runtime.env"
            runtime_file.write_text(local_runtime.render_runtime_env(self.values), encoding="utf-8")
            self.assertEqual(local_runtime.parse_env_file(runtime_file), self.values)
            compose_file = local_runtime.compose_runtime_env_file(runtime_file)
            expected = {**self.values, "PLATFORM_RUNTIME_ENV_FILE": str(compose_file)}
            self.assertEqual(local_runtime.parse_env_file(compose_file), expected)
            self.assertEqual(
                local_runtime.build_registry_auth_environment(source_environment={}, runtime_env_file=runtime_file), self.values
            )

    def test_invalid_input_diagnostics_do_not_include_values(self) -> None:
        for values in ({"INVALID\nKEY": "synthetic-private"}, {"VALID": "synthetic-private\0value"}):
            with self.subTest(values=values), self.assertRaises(ValueError) as error:
                local_runtime.render_runtime_env(values)
            self.assertNotIn("synthetic-private", str(error.exception))

    def test_real_compose_preserves_interpolation_and_service_env_values(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("Docker CLI unavailable; pure serialization regressions still run")
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
        environment["ODK_REVIEW_EXPANSION"] = "expanded"
        version = subprocess.run([docker, "compose", "version"], env=environment, capture_output=True, text=True, check=False)
        if version.returncode:
            self.skipTest("Docker Compose unavailable; pure serialization regressions still run")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_file = root / "runtime.env"
            runtime_file.write_text(local_runtime.render_runtime_env(self.values), encoding="utf-8")
            compose_file = local_runtime.compose_runtime_env_file(runtime_file)
            # Both consumption paths used by the shared Compose contract:
            # --env-file interpolation and the service's env_file.
            model = {
                "name": "synthetic-env-probe",
                "services": {
                    "probe": {
                        "image": "synthetic",
                        "env_file": str(compose_file),
                        "environment": {f"COPY_{key}": "${" + key + "}" for key in self.values},
                    }
                },
            }
            model_file = root / "compose.json"
            model_file.write_text(json.dumps(model), encoding="utf-8")
            command = [docker, "compose", "--env-file", str(compose_file), "-f", str(model_file), "config"]
            result = subprocess.run([*command, "--format", "json"], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, "Offline Compose config failed")
            resolved = json.loads(result.stdout)["services"]["probe"]["environment"]
            for key, value in self.values.items():
                # Compose escapes dollars when serializing its reusable config.
                self.assertEqual(resolved[key].replace("$$", "$"), value)
                self.assertEqual(resolved[f"COPY_{key}"].replace("$$", "$"), value)
            result = subprocess.run([*command, "--environment"], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, "Offline Compose interpolation probe failed")
            interpolation_values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            self.assertEqual(interpolation_values["DOLLARS"], self.values["DOLLARS"])

    def test_shared_compose_ssh_mount_agrees_with_workflow_forwarding(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("Docker CLI unavailable")
        if subprocess.run([docker, "compose", "version"], capture_output=True, check=False).returncode:
            self.skipTest("Docker Compose unavailable")
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                str(root / "keys with spaces"),
                "~/.ssh",
                "$SYNTHETIC_SSH_ROOT/.ssh",
                "${SYNTHETIC_SSH_ROOT}/.ssh",
                "${SYNTHETIC_ABSENT:-~}/.ssh",
                "${DATA_WORKFLOW_SSH_KEY}/keys",
            )
            for path in paths:
                with self.subTest(path=path), mock.patch.dict(os.environ, {"SYNTHETIC_SSH_ROOT": str(root)}, clear=True):
                    values = {
                        "DATA_WORKFLOW_SSH_DIR": path,
                        "DATA_WORKFLOW_SSH_KEY": "$SYNTHETIC_SSH_ROOT",
                        "ODOO_DB_NAME": "synthetic",
                        "ODOO_DB_USER": "synthetic",
                        "ODOO_DB_PASSWORD": "synthetic-$SYNTHETIC_SSH_ROOT #suffix",
                        "ODOO_DATA_VOLUME": "synthetic-data",
                        "ODOO_LOG_VOLUME": "synthetic-logs",
                        "ODOO_DB_VOLUME": "synthetic-db",
                    }
                    runtime_file = root / "runtime.env"
                    runtime_file.write_text(local_runtime.render_runtime_env(values), encoding="utf-8")
                    forwarded = local_runtime.data_workflow_script_environment(
                        local_runtime.resolve_data_workflow_environment(local_runtime.parse_env_file(runtime_file))
                    )
                    command = local_runtime.compose_base_command(runtime_repo_path=repo, runtime_env_file=runtime_file)
                    command[0] = docker
                    result = subprocess.run(
                        [*command, "config", "--format", "json"],
                        env=local_runtime.command_execution_env(),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, "Offline shared Compose config failed")
                    services = json.loads(result.stdout)["services"]
                    mounts = [mount for mount in services["script-runner"]["volumes"] if mount["target"].endswith("/.ssh")]
                    self.assertEqual(len(mounts), 2)
                    for mount in mounts:
                        self.assertEqual(mount["type"], "bind")
                        self.assertTrue(mount["read_only"])
                        self.assertEqual(mount["source"].replace("$$", "$"), forwarded["DATA_WORKFLOW_SSH_DIR"])
                    for service in services.values():
                        self.assertEqual(service["environment"]["ODOO_DB_PASSWORD"].replace("$$", "$"), values["ODOO_DB_PASSWORD"])
                    self.assertEqual(local_runtime.parse_env_file(runtime_file), values)


if __name__ == "__main__":
    unittest.main()
