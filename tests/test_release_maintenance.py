from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from odoo_devkit.artifact_provenance import ArtifactProvenanceError, aggregate_release_inventories, load_examined_input_plan


def load(name: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "docker" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load("odoo_release_plan")
inventory = load("odoo_release_inventory")


class ReleaseMaintenanceTests(unittest.TestCase):
    def test_xml_binary_references_across_sources_require_database_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tenant, shared = root / "tenant", root / "shared"
            (tenant / "module/data").mkdir(parents=True)
            (shared / "other/static").mkdir(parents=True)
            (tenant / "module/__manifest__.py").write_text(repr({"depends": ["other"], "data": ("data/logo.xml",)}))
            (shared / "other/__manifest__.py").write_text(repr({"depends": []}))
            (tenant / "module/data/logo.xml").write_text(
                '<odoo><field name="logo" type="base64" file="other/static/logo.png"/>'
                '<menuitem web_icon="other,static/icon.png"/>'
                '<field name="web_icon">other,static/alternate.png</field>'
                '<field name="web_icon" eval="\'other,static/evaluated.png\'"/></odoo>'
            )
            (shared / "other/static/logo.png").write_bytes(b"first")
            (shared / "other/static/icon.png").write_bytes(b"icon")
            (shared / "other/static/alternate.png").write_bytes(b"alternate")
            (shared / "other/static/evaluated.png").write_bytes(b"evaluated")
            metadata = {
                "sources": [
                    {"input_name": name, "repository": f"fixture/{name}", "commit": "a" * 40, "roots": [str(path)]}
                    for name, path in (("tenant", tenant), ("shared", shared))
                ],
                "addon_paths": [str(tenant), str(shared)],
            }
            before = inventory.build_inventory(metadata)
            (shared / "other/static/logo.png").write_bytes(b"second")
            after = inventory.build_inventory(metadata)
            left = next(item for item in before["sources"][1]["files"] if item["path"].endswith("logo.png"))
            right = next(item for item in after["sources"][1]["files"] if item["path"].endswith("logo.png"))
            self.assertNotEqual(left["sha256"], right["sha256"])
            self.assertEqual(right["kind"], "database_data")
            self.assertEqual(right["module"], "other")
            for source in metadata["sources"]:
                source_root = Path(source["roots"][0])
                prefix = source_root.as_posix().lstrip("/") + "/"
                source["files"] = [
                    {**item, "path": item["path"].removeprefix(prefix)} for item in inventory.inventory([source_root])[0]
                ]
            git_paths = inventory.build_inventory(metadata)
            for name in ("icon.png", "alternate.png", "evaluated.png"):
                self.assertEqual(
                    next(item for item in git_paths["sources"][1]["files"] if item["path"].endswith(name))["kind"], "database_data"
                )
            (tenant / "module/data/logo.xml").unlink()
            self.assertFalse(inventory.build_inventory(metadata)["complete"])
            (tenant / "module/data/logo.xml").write_text("<broken")
            self.assertFalse(inventory.build_inventory(metadata)["complete"])
            inventory.inventory([tenant])  # Malformed data does not prevent legacy artifact publication.

    def test_exposed_graph_and_examined_plan_support_real_dependency_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, depends in (("tenant/module", []), ("shared/module", ["missing"]), ("tenant/docs/example", ["missing"])):
                addon = root / path
                addon.mkdir(parents=True)
                (addon / "__manifest__.py").write_text(repr({"depends": depends}))
            metadata = {
                "sources": [
                    {"input_name": "tenant", "repository": "fixture/tenant", "commit": "a" * 40, "roots": [str(root / "tenant")]}
                ],
                "addon_paths": [str(root / "tenant"), str(root / "shared")],
                "examined_input_plan": {"examined_inputs_sha256": "b" * 64, "database_update_modules": ["module"]},
            }
            declaration = inventory.build_inventory(metadata)
            self.assertTrue(declaration["complete"])
            self.assertEqual(declaration["modules"], [{"name": "module", "depends": []}])
            self.assertEqual(declaration["database_update_modules"], ["module"])
            file = root / "examined.json"
            file.write_text(json.dumps(metadata["examined_input_plan"]))
            self.assertEqual(load_examined_input_plan(file), metadata["examined_input_plan"])
            metadata["examined_input_plan"]["database_update_modules"] = ["missing"]
            with self.assertRaises(ValueError):
                inventory.build_inventory(metadata)
            file.write_text(json.dumps({**metadata["examined_input_plan"], "token": "inert"}))
            with self.assertRaises(ArtifactProvenanceError):
                load_examined_input_plan(file)

    def test_platform_native_inputs_are_retained_and_graph_disagreement_refuses(self) -> None:
        platforms = ("linux/amd64", "linux/arm64")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declarations = {}
            for platform in platforms:
                output = root / platform.replace("/", "_")
                output.mkdir()
                (output / "dependency-provenance.json").write_text(json.dumps({"target_platform": platform}))
                declarations[platform] = {
                    "schema_version": 1,
                    "complete": True,
                    "read_write_compatible": True,
                    "modules": [],
                    "sources": [
                        {
                            "input_name": "base:runtime",
                            "repository": "fixture/base",
                            "commit": "a" * 40,
                            "files": [
                                {
                                    "path": "usr/local/bin/uv",
                                    "sha256": hashlib.sha256(platform.encode()).hexdigest(),
                                    "kind": "dependency",
                                    "module": "",
                                },
                            ],
                        }
                    ],
                }
                (output / "release-compatibility.json").write_text(json.dumps(declarations[platform]))
            merged = aggregate_release_inventories(evidence_root=root, expected_platforms=platforms)
            files = merged["sources"][0]["files"]
            self.assertEqual(
                {file["sha256"] for file in files}, {hashlib.sha256(platform.encode()).hexdigest() for platform in platforms}
            )
            self.assertEqual(len({file["path"] for file in files}), len(platforms))
            plan.verify_image_declaration(declarations[platforms[0]], merged, platform=platforms[0])
            with self.assertRaises(ValueError):
                plan.verify_image_declaration(declarations[platforms[1]], merged, platform=platforms[0])
            declarations[platforms[1]]["modules"] = [{"name": "unexpected", "depends": []}]
            (root / platforms[1].replace("/", "_") / "release-compatibility.json").write_text(json.dumps(declarations[platforms[1]]))
            with self.assertRaises(ArtifactProvenanceError):
                aggregate_release_inventories(evidence_root=root, expected_platforms=platforms)

    def setUp(self) -> None:
        self.graph = {"base": set(), "changed": {"base"}, "optional": {"changed"}, "unrelated": {"base"}, "new": {"changed"}}
        self.states = {
            "base": "installed",
            "changed": "installed",
            "optional": "installed",
            "unrelated": "installed",
            "new": "uninstalled",
        }
        self.image = "fixture/image@sha256:" + "a" * 64
        self.candidate = {
            "artifact_id": "candidate",
            "image": {"repository": "fixture/image", "digest": "sha256:" + "a" * 64},
            "odoo_install_modules": ["new"],
            "release_compatibility": {
                "complete": True,
                "modules": [{"name": name, "depends": sorted(deps)} for name, deps in self.graph.items()],
            },
        }
        self.payload = {
            "database": "fixture",
            "candidate_manifest": self.candidate,
            "release": {
                "candidate_artifact_id": "candidate",
                "candidate_image": self.image,
                "candidate_manifest_sha256": plan.digest(self.candidate),
                "module_plan_complete": True,
                "classification": "database_changing",
                "install_modules": ["new"],
                "update_modules": ["changed"],
                "changed_modules": ["changed"],
                "changes": [{"module": "changed", "kind": "model"}],
            },
        }

    def resolve(self) -> dict[str, Any]:
        return plan.resolve_plan(self.payload, database="fixture", image=self.image, states=self.states, graph=self.graph)

    def test_database_reconciliation_updates_installed_dependents_and_keeps_installs_separate(self) -> None:
        result = self.resolve()
        self.assertEqual(result["install_modules"], ["new"])
        self.assertEqual(result["update_modules"], ["changed", "optional"])
        self.assertNotIn("unrelated", result["update_modules"])
        self.states["new"] = "installed"
        self.assertEqual(self.resolve()["install_modules"], [])
        self.assertIn("new", self.resolve()["update_modules"])
        self.payload["release"].update(update_modules=[], changed_modules=[], changes=[])
        self.assertEqual(self.resolve()["update_modules"], [])

    def test_already_installed_new_requirement_still_applies_its_model_changes(self) -> None:
        self.graph["new"] = {"base"}
        self.states["new"] = "installed"
        self.candidate["release_compatibility"]["modules"] = [
            {"name": name, "depends": sorted(deps)} for name, deps in self.graph.items()
        ]
        self.payload["release"].update(update_modules=[], changed_modules=["new"], changes=[{"module": "new", "kind": "model"}])
        self.payload["release"]["candidate_manifest_sha256"] = plan.digest(self.candidate)
        self.assertEqual(self.resolve()["install_modules"], [])
        self.assertEqual(self.resolve()["update_modules"], ["new"])

    def test_manifest_database_semantics_require_coverage_but_assets_only_do_not(self) -> None:
        self.candidate["odoo_install_modules"] = []
        file = {
            "path": "changed/__manifest__.py",
            "module": "changed",
            "kind": "manifest_assets",
            "manifest_database_sha256": "a" * 64,
        }
        self.candidate["release_compatibility"]["sources"] = [{"input_name": "tenant", "files": [file]}]
        production = copy.deepcopy(self.candidate)
        self.payload["production_manifest"] = production
        file["manifest_database_sha256"] = "b" * 64
        self.payload["release"].update(
            install_modules=[],
            update_modules=[],
            production_manifest_sha256=plan.digest(production),
            changes=[
                {
                    "module": "changed",
                    "kind": "manifest_assets",
                    "input_name": "tenant",
                    "path": file["path"],
                }
            ],
            candidate_manifest_sha256=plan.digest(self.candidate),
        )
        with self.assertRaises(ValueError):
            self.resolve()
        file["manifest_database_sha256"] = "a" * 64
        self.payload["release"]["candidate_manifest_sha256"] = plan.digest(self.candidate)
        self.assertEqual(self.resolve()["update_modules"], [])

    def test_verified_multi_module_and_single_addon_checkout_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "_checkouts" / "repo-checksum"
            for name, dependencies in (("one", []), ("two", ["one"])):
                addon = checkout / name
                addon.mkdir(parents=True)
                (addon / "__manifest__.py").write_text(repr({"depends": dependencies}))
            marker = Path(str(checkout) + ".odoo-source")
            marker.write_text("fixture/repo@" + "a" * 40)
            metadata = {
                "sources": [
                    {
                        "input_name": "addon:fixture/repo",
                        "repository": "fixture/repo",
                        "commit": "a" * 40,
                        "roots": [],
                        "checkout_root": str(root / "_checkouts"),
                        "root_module": "repo",
                    }
                ]
            }
            result = inventory.build_inventory(metadata)
            self.assertEqual({module["name"] for module in result["modules"]}, {"one", "two"})
            self.assertTrue(result["complete"])
            marker.write_text("fixture/repo@" + "b" * 40)
            with self.assertRaises(ValueError):
                inventory.build_inventory(metadata)
            marker.write_text("fixture/repo@" + "a" * 40)
            (checkout / "__manifest__.py").write_text(repr({"depends": []}))
            result = inventory.build_inventory(metadata)
            self.assertIn("repo", {module["name"] for module in result["modules"]})

    def test_compatible_static_release_has_no_blanket_updates(self) -> None:
        self.candidate["odoo_install_modules"] = []
        self.payload["release"].update(
            classification="compatible", install_modules=[], update_modules=[], changes=[{"module": "changed", "kind": "static"}]
        )
        self.payload["release"]["candidate_manifest_sha256"] = plan.digest(self.candidate)
        self.assertEqual(self.resolve()["update_modules"], [])
        self.assertEqual(self.resolve()["install_modules"], [])

    def test_missing_changed_modules_incomplete_identity_and_pending_work_refuse(self) -> None:
        original = copy.deepcopy(self.payload)
        for fault in ("changed", "updates", "incomplete", "identity", "graph", "pending"):
            with self.subTest(fault=fault):
                self.payload = copy.deepcopy(original)
                if fault == "changed":
                    self.payload["release"]["changed_modules"] = []
                elif fault == "updates":
                    self.payload["release"]["update_modules"] = []
                elif fault == "incomplete":
                    self.payload["release"]["module_plan_complete"] = False
                elif fault == "identity":
                    self.payload["candidate_manifest"]["artifact_id"] = "stale"
                elif fault == "graph":
                    self.payload["candidate_manifest"]["release_compatibility"]["modules"] = []
                    self.payload["release"]["candidate_manifest_sha256"] = plan.digest(self.payload["candidate_manifest"])
                else:
                    self.states["optional"] = "to upgrade"
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_inventory_preserves_files_hashes_and_manifest_database_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "addon"
            (module / "static").mkdir(parents=True)
            manifest = module / "__manifest__.py"
            manifest.write_text(repr({"depends": [], "data": ["view.xml"], "assets": {"web.assets_frontend": ["a.js"]}}))
            (module / "static" / "a.js").write_text("old")
            (module / "static" / "view.xml").write_text("<odoo/>")
            manifest.write_text(repr({"depends": [], "data": ["static/view.xml"], "assets": {"web.assets_frontend": ["a.js"]}}))
            first, graph = inventory.inventory([root])
            self.assertEqual(graph, {"addon": set()})
            self.assertEqual(next(item for item in first if item["path"].endswith("static/view.xml"))["kind"], "database_data")
            manifest.write_text(repr({"depends": [], "data": ["static/view.xml"], "assets": {"web.assets_frontend": ["b.js"]}}))
            second, _ = inventory.inventory([root])
            left = next(item for item in first if item["kind"] == "manifest_assets")
            right = next(item for item in second if item["kind"] == "manifest_assets")
            self.assertNotEqual(left["sha256"], right["sha256"])
            self.assertEqual(left["manifest_database_sha256"], right["manifest_database_sha256"])
            manifest.write_text(repr({"depends": [], "data": ["other.xml"]}))
            third, _ = inventory.inventory([root])
            self.assertNotEqual(
                right["manifest_database_sha256"],
                next(item for item in third if item["kind"] == "manifest_assets")["manifest_database_sha256"],
            )
