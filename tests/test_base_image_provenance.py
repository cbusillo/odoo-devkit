from __future__ import annotations

import copy
import json
import subprocess
import unittest
from unittest import mock

from odoo_devkit import local_runtime


class BaseImageProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = "ghcr.io/example/runtime"
        self.digest = "sha256:" + "a" * 64
        self.source_repository = "example/odoo-docker"
        self.source_ref = "d" * 40
        self.annotations = {
            "org.opencontainers.image.source": f"https://github.com/{self.source_repository}",
            "org.opencontainers.image.revision": self.source_ref,
        }
        self.platforms = ("linux/amd64", "linux/arm64")
        self.descriptors = [
            {"digest": "sha256:" + character * 64, "platform": {"os": "linux", "architecture": platform.split("/")[1]}}
            for platform, character in zip(self.platforms, ("b", "c"), strict=True)
        ]
        self.manifests = {
            descriptor["digest"]: {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + "e" * 64},
                "layers": [],
                "annotations": copy.deepcopy(self.annotations),
            }
            for descriptor in self.descriptors
        }
        self.index = {"schemaVersion": 2, "annotations": copy.deepcopy(self.annotations), "manifests": self.descriptors}
        self.configs = {
            platform: {
                "os": "linux",
                "architecture": platform.split("/")[1],
                "config": {"Labels": {"org.opencontainers.image.version": "inherited-operating-system"}},
            }
            for platform in self.platforms
        }
        self.inspected_references: list[str] = []

    def _resolve(
        self, required_platforms: tuple[str, ...] | None = None, *, single: bool = False
    ) -> local_runtime.BaseImageProvenance:
        def inspect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            reference = command[4]
            self.inspected_references.append(reference)
            digest = reference.rsplit("@", 1)[1]
            if "--format" in command:
                payload = self.configs[self.platforms[0]] if single else self.configs
            elif digest == self.digest:
                payload = self.manifests[self.descriptors[0]["digest"]] if single else self.index
            else:
                payload = self.manifests[digest]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        with (
            mock.patch.object(local_runtime, "resolve_image_digest", return_value=self.digest),
            mock.patch.object(local_runtime.subprocess, "run", side_effect=inspect),
        ):
            return local_runtime.resolve_base_image_provenance(
                image_reference=f"{self.repository}:stable",
                role="runtime",
                required_platforms=self.platforms if required_platforms is None else required_platforms,
            )

    def test_annotations_bind_the_resolved_index_and_required_children(self) -> None:
        provenance = self._resolve()
        self.assertEqual(provenance.source_repository, self.source_repository)
        self.assertEqual(provenance.source_ref, self.source_ref)
        self.assertEqual(provenance.digest_reference, f"{self.repository}@{self.digest}")
        self.assertEqual(provenance.tags, ("stable",))
        self.assertEqual(
            set(self.inspected_references),
            {f"{self.repository}@{self.digest}", *(f"{self.repository}@{descriptor['digest']}" for descriptor in self.descriptors)},
        )

    def test_annotations_override_complete_inherited_config_identity(self) -> None:
        inherited = {
            "org.opencontainers.image.source": "https://github.com/example/inherited-base",
            "org.opencontainers.image.revision": "f" * 40,
        }
        for config in self.configs.values():
            config["config"]["Labels"] = inherited
        provenance = self._resolve()
        self.assertEqual(provenance.source_repository, self.source_repository)
        self.assertEqual(provenance.source_ref, self.source_ref)

    def test_generic_arm64_request_accepts_a_variant_descriptor(self) -> None:
        self.descriptors[1]["platform"]["variant"] = "v8"
        provenance = self._resolve()
        self.assertEqual(provenance.source_ref, self.source_ref)

    def test_single_annotated_manifest_requires_its_actual_platform(self) -> None:
        provenance = self._resolve((self.platforms[0],), single=True)
        self.assertEqual(provenance.source_ref, self.source_ref)
        with self.assertRaises(local_runtime.RuntimeCommandError):
            self._resolve(self.platforms, single=True)

    def test_legacy_label_only_images_keep_index_and_single_platform_support(self) -> None:
        self.index.pop("annotations")
        for manifest in self.manifests.values():
            manifest.pop("annotations")
        for config in self.configs.values():
            config["config"]["Labels"] = copy.deepcopy(self.annotations)
        for single in (False, True):
            with self.subTest(single=single):
                provenance = self._resolve((self.platforms[0],) if single else self.platforms, single=single)
                self.assertEqual(provenance.source_repository, self.source_repository)
                self.assertEqual(provenance.source_ref, self.source_ref)

    def test_missing_and_ambiguous_target_platforms_are_rejected(self) -> None:
        with self.assertRaises(local_runtime.RuntimeCommandError):
            self._resolve(("linux/s390x",))
        self.descriptors.append(copy.deepcopy(self.descriptors[0]))
        with self.assertRaises(local_runtime.RuntimeCommandError):
            self._resolve()

    def test_partial_annotation_identity_never_falls_back_to_labels(self) -> None:
        for location in (self.index, self.manifests[self.descriptors[0]["digest"]]):
            with self.subTest(location=location):
                partial = copy.deepcopy(location["annotations"])
                partial.pop("org.opencontainers.image.revision")
                for config in self.configs.values():
                    config["config"]["Labels"] = copy.deepcopy(self.annotations)
                with mock.patch.dict(location, {"annotations": partial}), self.assertRaises(local_runtime.RuntimeCommandError):
                    self._resolve()

    def test_index_and_platform_source_disagreement_is_rejected(self) -> None:
        for location in (self.index, self.manifests[self.descriptors[0]["digest"]]):
            for field, value in (
                ("org.opencontainers.image.revision", "f" * 40),
                ("org.opencontainers.image.source", "https://github.com/example/other"),
            ):
                with self.subTest(field=field, location=location):
                    with (
                        mock.patch.dict(location["annotations"], {field: value}),
                        self.assertRaises(local_runtime.RuntimeCommandError),
                    ):
                        self._resolve()

    def test_annotated_index_cannot_substitute_for_unidentified_child(self) -> None:
        self.manifests[self.descriptors[0]["digest"]].pop("annotations")
        with self.assertRaises(local_runtime.RuntimeCommandError):
            self._resolve()

    def test_malformed_identity_manifest_and_digest_are_rejected(self) -> None:
        for field, value in (
            ("org.opencontainers.image.revision", "main"),
            ("org.opencontainers.image.source", "https://user:password@github.com/example/runtime"),
        ):
            with self.subTest(field=field):
                with (
                    mock.patch.dict(self.index["annotations"], {field: value}),
                    self.assertRaises(local_runtime.RuntimeCommandError),
                ):
                    self._resolve()
        self.descriptors[0]["digest"] = "mutable-tag"
        with self.assertRaises(local_runtime.RuntimeCommandError):
            self._resolve()
