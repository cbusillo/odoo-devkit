from __future__ import annotations

import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit.base_python import BasePythonCatalog, parse_base_python_catalog, read_base_python_catalogs
from odoo_devkit.dependency_workspace import DependencyWorkspaceError, derive_base_python_lock


class BasePythonTests(unittest.TestCase):
    def test_reader_keeps_platform_catalogs_distinct_and_refuses_mutable_images(self) -> None:
        image = "registry.invalid/base@sha256:" + "c" * 64
        versions = {"linux/amd64": "3.10", "linux/arm64": "3.11"}

        def extract(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            platform = args[args.index("--platform") + 1]
            output = Path(args[args.index("--output") + 1].split("dest=", 1)[1])
            output.mkdir()
            (output / "catalog.txt").write_text(f"idna=={versions[platform]}\n")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("odoo_devkit.base_python.subprocess.run", side_effect=extract) as run:
            catalogs = read_base_python_catalogs(image=image, platforms=tuple(versions))
            self.assertEqual({c.platform: c.requirements for c in catalogs}, {p: (f"idna=={v}",) for p, v in versions.items()})
            self.assertEqual({c.image for c in catalogs}, {image})
            with self.assertRaisesRegex(ValueError, "unresolved"):
                read_base_python_catalogs(image="registry.invalid/base:mutable", platforms=tuple(versions))
            self.assertEqual(run.call_count, len(versions))

    def test_platform_specific_versions_fork_the_effective_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.13"\n'
                'dependencies=["idna>=3"]\n[tool.uv]\npackage=false\n'
            )
            catalogs = tuple(
                BasePythonCatalog("registry.invalid/base@sha256:" + "d" * 64, platform, (f"idna=={version}",))
                for platform, version in (("linux/amd64", "3.10"), ("linux/arm64", "3.11"))
            )
            derive_base_python_lock(staged_root=root, catalogs=catalogs, python_version="3.13")
            subprocess.run(
                ["uv", "export", "--frozen", "--no-config", "--no-emit-workspace", "--no-hashes", "-o", "requirements.txt"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            for platform, python_platform in (
                ("linux/amd64", "x86_64-unknown-linux-gnu"),
                ("linux/arm64", "aarch64-unknown-linux-gnu"),
            ):
                installed = subprocess.run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--dry-run",
                        "--target",
                        str(root / "target"),
                        "--no-deps",
                        "--python-platform",
                        python_platform,
                        "--python-version",
                        "3.13",
                        "-r",
                        "requirements.txt",
                    ],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                expected = next(c.requirements[0] for c in catalogs if c.platform == platform)
                self.assertIn(expected, installed.stderr)

    def test_base_move_relocks_overlap_retains_other_pins_and_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                '[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.13"\n'
                'dependencies=["idna>=3", "humanfriendly>=9"]\n[tool.uv]\npackage=false\n'
                'constraint-dependencies=["humanfriendly<11"]\n'
            )
            subprocess.run(
                ["uv", "lock", "--no-config", "--upgrade-package", "humanfriendly==9.2"], cwd=source, check=True, capture_output=True
            )
            original = {p.name: p.read_bytes() for p in source.iterdir()}
            locks = []
            for index, version in enumerate(("3.10", "3.11", "3.11")):
                stage = root / f"stage-{index}"
                shutil.copytree(source, stage)
                catalog = BasePythonCatalog(
                    image=f"registry.invalid/base@sha256:{str(int(index > 0)) * 64}",
                    platform="linux/arm64",
                    requirements=(f"idna=={version}",),
                )
                derive_base_python_lock(staged_root=stage, catalogs=(catalog,), python_version="3.13")
                packages = {p["name"]: p["version"] for p in tomllib.loads((stage / "uv.lock").read_text())["package"]}
                self.assertEqual(packages["idna"], version)
                self.assertEqual(packages["humanfriendly"], "9.2")
                subprocess.run(["uv", "lock", "--check", "--offline", "--no-config"], cwd=stage, check=True, capture_output=True)
                locks.append((stage / "uv.lock").read_bytes())
            self.assertEqual(locks[1], locks[2])
            self.assertNotEqual(locks[0], locks[1])
            self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, original)

    def test_explicit_root_and_member_conflicts_keep_package_and_base_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for member in (False, True):
                stage = root / str(member)
                stage.mkdir()
                (stage / "pyproject.toml").write_text(
                    '[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.13"\n'
                    f"dependencies={[] if member else ['idna<3']!r}\n[tool.uv]\npackage=false\n"
                    + ('[tool.uv.workspace]\nmembers=["addon"]\n' if member else "")
                )
                if member:
                    (stage / "addon").mkdir()
                    (stage / "addon" / "pyproject.toml").write_text(
                        '[project]\nname="addon"\nversion="0"\nrequires-python=">=3.13"\n'
                        'dependencies=["idna<3"]\n[tool.uv]\npackage=false\n'
                    )
                catalog = BasePythonCatalog("registry.invalid/base@sha256:" + "a" * 64, "linux/amd64", ("idna==3.11",))
                with self.subTest(member=member), self.assertRaises(DependencyWorkspaceError) as caught:
                    derive_base_python_lock(staged_root=stage, catalogs=(catalog,), python_version="3.13")
                self.assertIn("idna", str(caught.exception))
                self.assertIn(catalog.image, str(caught.exception))

    def test_catalog_rejects_empty_unpinned_duplicate_and_invalid_entries(self) -> None:
        image = "registry.invalid/base@sha256:" + "b" * 64
        for text in ("", "idna>=3", "idna==3.10\nidna==3.11", "--index-url https://invalid", "idna==not-a-version"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, image):
                parse_base_python_catalog(text, image=image, platform="linux/amd64")
        catalog = parse_base_python_catalog("# producer\nSome_Package==1.0\nidna==3.11\n", image=image, platform="linux/amd64")
        self.assertEqual(set(catalog.requirements), {"some-package==1.0", "idna==3.11"})
