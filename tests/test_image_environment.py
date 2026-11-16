from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from odoo_devkit.local_runtime import RuntimeCommandError, write_artifact_image_environment


@contextmanager
def built_image(dockerfile: Path) -> Iterator[str]:
    tag = f"devkit-environment-test:{uuid.uuid4().hex}"
    subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--file",
            str(dockerfile),
            "--target",
            "production",
            "--tag",
            tag,
            str(dockerfile.parent),
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield tag
    finally:
        subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True)


class ImageEnvironmentTests(unittest.TestCase):
    def test_image_stores_literal_defaults_and_container_overrides_them(self) -> None:
        environment = {"SELECTED": "true", "EMPTY": "", "LITERAL": ' $HOME ${SELECTED} "quoted" \\path # literal '}
        container = None
        with tempfile.TemporaryDirectory() as directory:
            dockerfile = Path(directory) / "Dockerfile"
            dockerfile.write_text('FROM scratch AS production\nENV SELECTED="base"\nUSER 1000')
            write_artifact_image_environment(dockerfile, tuple(environment.items()))
            with built_image(dockerfile) as tag:
                try:
                    config = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]["Config"]
                    actual = dict(item.split("=", 1) for item in config["Env"])
                    self.assertEqual({name: actual.get(name) for name in environment}, environment)
                    container = subprocess.check_output(
                        ["docker", "create", "--env", "SELECTED=lane", tag, "/unused"], text=True
                    ).strip()
                    config = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]["Config"]
                    self.assertEqual(dict(item.split("=", 1) for item in config["Env"]), {**actual, "SELECTED": "lane"})
                finally:
                    if container:
                        subprocess.run(["docker", "rm", container], check=True, capture_output=True)

    def test_defaults_refuse_a_later_stage_but_omission_leaves_older_runtimes_unchanged(self) -> None:
        for last_stage in ("FROM scratch", "FROM scratch AS development"):
            with self.subTest(last_stage=last_stage), tempfile.TemporaryDirectory() as directory:
                dockerfile = Path(directory) / "Dockerfile"
                dockerfile.write_text(f"FROM scratch AS production\n{last_stage}\n")
                original = dockerfile.read_bytes()
                write_artifact_image_environment(dockerfile, ())
                self.assertEqual(dockerfile.read_bytes(), original)
                with self.assertRaises(RuntimeCommandError):
                    write_artifact_image_environment(dockerfile, (("SELECTED", "true"),))
                self.assertEqual(dockerfile.read_bytes(), original)
