from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any


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
            first, graph = inventory.inventory([root])
            self.assertEqual(graph, {"addon": set()})
            manifest.write_text(repr({"depends": [], "data": ["view.xml"], "assets": {"web.assets_frontend": ["b.js"]}}))
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
