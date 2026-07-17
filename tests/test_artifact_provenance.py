from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from odoo_devkit.artifact_provenance import (
    ArtifactProvenanceError,
    aggregate_dependency_evidence,
    normalize_repository_identity,
)


class ArtifactProvenanceTests(unittest.TestCase):
    def test_aggregate_dependency_evidence_emits_launchplane_v2_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence_root = Path(temporary_directory)
            locks = self._locks()
            external_inputs = self._external_inputs()
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/arm64",
                locks=locks,
                external_inputs=external_inputs,
            )
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/amd64",
                locks=locks,
                external_inputs=external_inputs,
            )

            provenance = aggregate_dependency_evidence(
                evidence_root=evidence_root,
                expected_platforms=("linux/arm64", "linux/amd64"),
                expected_uv_locks=tuple(locks),
            )

            self.assertEqual(provenance["target_platforms"], ["linux/amd64", "linux/arm64"])
            self.assertEqual([lock["scope"] for lock in provenance["uv_locks"]], ["support_runtime", "tenant"])
            self.assertEqual(
                provenance["external_compatibility_inputs"][0]["source_repository"],
                "example/external-addon",
            )
            self.assertEqual(
                provenance["python_environments"]["linux/amd64"]["packages"][1]["source"],
                {
                    "kind": "vcs",
                    "repository": "example/openupgradelib",
                    "commit": "d" * 40,
                },
            )

    def test_aggregate_dependency_evidence_rejects_lock_bytes_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence_root = Path(temporary_directory)
            locks = self._locks()
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/amd64",
                locks=locks,
                external_inputs=self._external_inputs(),
            )
            expected_locks = copy.deepcopy(locks)
            expected_locks[1]["sha256"] = "f" * 64

            with self.assertRaisesRegex(ArtifactProvenanceError, "exact staged lock inputs"):
                aggregate_dependency_evidence(
                    evidence_root=evidence_root,
                    expected_platforms=("linux/amd64",),
                    expected_uv_locks=tuple(expected_locks),
                )

    def test_aggregate_dependency_evidence_rejects_cross_platform_external_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence_root = Path(temporary_directory)
            locks = self._locks()
            external_inputs = self._external_inputs()
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/amd64",
                locks=locks,
                external_inputs=external_inputs,
            )
            changed_inputs = copy.deepcopy(external_inputs)
            changed_inputs[0]["dependency_file_sha256"] = "e" * 64
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/arm64",
                locks=locks,
                external_inputs=changed_inputs,
            )

            with self.assertRaisesRegex(ArtifactProvenanceError, "differs across target platforms"):
                aggregate_dependency_evidence(
                    evidence_root=evidence_root,
                    expected_platforms=("linux/amd64", "linux/arm64"),
                    expected_uv_locks=tuple(locks),
                )

    def test_repository_identity_rejects_local_and_authenticated_values(self) -> None:
        for value in (
            "/Users/operator/private",
            "file:///tmp/private",
            "https://operator:secret@example.invalid/repo.git",
            "git@example.invalid:owner/../repo",
            "https://example.invalid/owner/../repo",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ArtifactProvenanceError):
                    normalize_repository_identity(value)

    def test_aggregate_dependency_evidence_rejects_unsafe_relative_path_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence_root = Path(temporary_directory)
            locks = self._locks()
            external_inputs = self._external_inputs()
            external_inputs[0]["dependency_file_path"] = "dependencies/private requirements.txt"
            self._write_sidecar(
                evidence_root=evidence_root,
                platform="linux/amd64",
                locks=locks,
                external_inputs=external_inputs,
            )

            with self.assertRaisesRegex(ArtifactProvenanceError, "safe repository-relative path"):
                aggregate_dependency_evidence(
                    evidence_root=evidence_root,
                    expected_platforms=("linux/amd64",),
                    expected_uv_locks=tuple(locks),
                )

    @staticmethod
    def _locks() -> list[dict[str, str]]:
        return [
            {
                "scope": "support_runtime",
                "source_repository": "example/odoo-devkit",
                "source_ref": "a" * 40,
                "path": "docker/runtime-python/uv.lock",
                "sha256": "b" * 64,
            },
            {
                "scope": "tenant",
                "source_repository": "example/tenant",
                "source_ref": "c" * 40,
                "path": "uv.lock",
                "sha256": "d" * 64,
            },
        ]

    @staticmethod
    def _external_inputs() -> list[dict[str, str]]:
        return [
            {
                "source_repository": "https://github.com/example/external-addon.git",
                "source_ref": "e" * 40,
                "dependency_file_path": "requirements.txt",
                "dependency_file_sha256": "f" * 64,
                "format": "requirements_txt",
                "resolution_posture": "exact_source_unlocked",
            }
        ]

    @staticmethod
    def _write_sidecar(
        *,
        evidence_root: Path,
        platform: str,
        locks: list[dict[str, str]],
        external_inputs: list[dict[str, str]],
    ) -> None:
        packages = [
            {
                "name": "httpx",
                "version": "0.28.1",
                "source": {"kind": "registry", "repository": "", "commit": ""},
            },
            {
                "name": "openupgradelib",
                "version": "3.12.0",
                "source": {
                    "kind": "vcs",
                    "repository": "example/openupgradelib",
                    "commit": "d" * 40,
                },
            },
        ]
        packages_sha256 = hashlib.sha256(
            json.dumps(packages, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        output_directory = evidence_root / platform.replace("/", "_")
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / "dependency-provenance.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "layout": "two_lock",
                    "publishable": True,
                    "target_platform": platform,
                    "uv_locks": locks,
                    "python_environment": {
                        "python_version": "3.13.7",
                        "packages": packages,
                        "package_count": len(packages),
                        "packages_sha256": packages_sha256,
                    },
                    "external_compatibility_inputs": external_inputs,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
