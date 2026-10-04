from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from odoo_devkit import local_runtime


class RuntimeEnvSerializationTests(unittest.TestCase):
    values = {
        "DOLLARS": "synthetic-$ODK_REVIEW_EXPANSION ${ODK_REVIEW_EXPANSION} $$ #suffix",
        "QUOTES": "synthetic-'single' and \"double\"",
        "BACKSLASHES": "synthetic-\\n\\t\\'\\\"\\\\end\\",
        "WHITESPACE": "  synthetic value  ",
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


if __name__ == "__main__":
    unittest.main()
