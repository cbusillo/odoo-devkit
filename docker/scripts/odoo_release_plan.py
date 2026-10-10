"""Reconcile Launchplane's immutable release plan with the selected database.

This does not classify releases or authorize traffic/writers. Launchplane owns
those decisions; this boundary refuses incomplete or mismatched execution input.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def names(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name) for name in value):
        raise ValueError("Release module names must be an explicit array of addon names")
    if len(value) != len(set(value)):
        raise ValueError("Duplicate release module names")
    return set(value)


def resolve_plan(
    payload: dict[str, Any], *, database: str, image: str, states: dict[str, str], graph: dict[str, set[str]]
) -> dict[str, Any]:
    release = payload["release"]
    candidate = payload["candidate_manifest"]
    if payload.get("database") != database or not image or release.get("candidate_image") != image:
        raise ValueError("Release plan database/image identity mismatch")
    if release.get("candidate_artifact_id") != candidate.get("artifact_id"):
        raise ValueError("Release plan artifact identity mismatch")
    if release.get("candidate_manifest_sha256") != digest(candidate):
        raise ValueError("Release plan manifest identity mismatch")
    if image != f"{candidate['image']['repository']}@{candidate['image']['digest']}":
        raise ValueError("Release plan candidate image mismatch")
    if release.get("module_plan_complete") is not True or release.get("classification") not in {"compatible", "database_changing"}:
        raise ValueError("Release module plan is incomplete")
    declaration = candidate["release_compatibility"]
    declared_graph = {module["name"]: names(module["depends"]) for module in declaration["modules"]}
    if declaration.get("complete") is not True or declared_graph != graph or len(declared_graph) != len(declaration["modules"]):
        raise ValueError("Release module graph differs from the actual image")
    if any(dependencies - graph.keys() for dependencies in graph.values()):
        raise ValueError("Release image graph has missing dependencies")
    installs = names(release["install_modules"])
    updates = names(release["update_modules"])
    changed = names(release["changed_modules"])
    if (installs | updates | changed) - graph.keys() or installs & updates:
        raise ValueError("Missing or conflicting release modules")
    roots = {
        change["module"]
        for change in release["changes"]
        if change["kind"] in {"database_data", "model", "migration", "dependency"} and change.get("module")
    }
    allowed_kinds = {"static", "code", "manifest_assets", "database_data", "model", "migration", "dependency", "docs_ci"}
    for change in release["changes"]:
        if change["kind"] not in allowed_kinds:
            raise ValueError("Release plan contains unexamined changes")
        if change["kind"] == "manifest_assets":
            production = payload.get("production_manifest")
            if not isinstance(production, dict) or digest(production) != release.get("production_manifest_sha256"):
                raise ValueError("Manifest changes require the exact production declaration")

            def manifest_semantics(manifest: dict[str, Any], current_change: dict[str, Any] = change) -> str | None:
                for source in manifest["release_compatibility"]["sources"]:
                    if source["input_name"] == current_change["input_name"]:
                        for file in source["files"]:
                            if file["path"] == current_change["path"]:
                                return file.get("manifest_database_sha256")
                return None

            before, after = manifest_semantics(production), manifest_semantics(candidate)
            if not before or not after or before != after:
                roots.add(change["module"])
        if change["kind"] == "dependency" and not change.get("module"):
            roots |= names(declaration.get("database_update_modules"))
    if not roots <= graph.keys():
        raise ValueError("Release plan has unresolved database roots")
    if any(change.get("module") and change["module"] not in changed for change in release["changes"]):
        raise ValueError("Release plan omits changed modules")
    if roots - (updates | installs):
        raise ValueError("Release plan omits required database changes")
    if release["classification"] == "compatible" and (installs or updates or roots):
        raise ValueError("Compatible plan requires database changes")
    pending = {name for name, state in states.items() if state in {"to install", "to upgrade", "to remove"}}
    if pending:
        raise ValueError("Database has pending module work; refusing to guess release coverage")
    installed = {name for name, state in states.items() if state == "installed"}
    if installed - graph.keys():
        raise ValueError("Installed modules are missing from the candidate image")
    required = names(candidate["odoo_install_modules"]) | installs | (updates & installed)
    if required - graph.keys():
        raise ValueError("Required installs are missing from the image")
    # Optional installed addons are database state, not artifact requirements.
    # Odoo updates their reverse dependents too; capture that effect explicitly.
    actual_updates = (updates | (installs & roots)) & installed
    while True:
        while True:
            expanded = required | {dep for name in required for dep in graph[name]}
            if expanded == required:
                break
            required = expanded
        actual_installs = required - installed
        expanded = actual_updates | {name for name in installed if graph[name] & (actual_updates | actual_installs)}
        if expanded == actual_updates:
            break
        actual_updates = expanded
        required |= actual_updates
    if release["classification"] == "compatible" and actual_installs:
        raise ValueError("Compatible release database is missing required modules")
    return {
        "candidate_artifact_id": release["candidate_artifact_id"],
        "candidate_image": image,
        "candidate_manifest_sha256": release["candidate_manifest_sha256"],
        "database": database,
        "changed_modules": sorted(changed),
        "install_modules": sorted(actual_installs),
        "update_modules": sorted(actual_updates),
        "already_installed": sorted(installed),
        "plan_sha256": digest(payload),
    }


def load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Release plan must be an object")
    return payload


def verify_image_declaration(actual: dict[str, Any], expected: dict[str, Any], *, platform: str) -> None:
    """Bind execution to the declaration baked into this container's image."""
    for field, default in (
        ("schema_version", 1),
        ("complete", False),
        ("read_write_compatible", False),
        ("modules", []),
        ("examined_inputs_sha256", ""),
        ("database_update_modules", None),
    ):
        left, right = actual.get(field, default), expected.get(field, default)
        if field == "modules":
            left, right = sorted(left, key=lambda item: item["name"]), sorted(right, key=lambda item: item["name"])
        if left != right:
            raise ValueError("Running image declaration does not match the release artifact")
    current = {source["input_name"]: source for source in actual.get("sources", [])}
    candidate = {source["input_name"]: source for source in expected.get("sources", [])}
    if current.keys() != candidate.keys():
        raise ValueError("Running image source inventory differs from the release artifact")
    prefix = f"platforms/{platform.replace('/', '_')}/"
    for input_name, source in candidate.items():
        present = current[input_name]
        if (present["repository"], present["commit"]) != (source["repository"], source["commit"]):
            raise ValueError("Running image source identity differs from the release artifact")

        def files(items: list[dict[str, Any]], *, select: bool = False) -> dict[str, dict[str, Any]]:
            result = {}
            for item in items:
                path = item["path"]
                if select and path.startswith("platforms/"):
                    if not path.startswith(prefix):
                        continue
                    path = path.removeprefix(prefix)
                result[path] = {
                    **item,
                    "path": path,
                    "module": item.get("module", ""),
                    "manifest_database_sha256": item.get("manifest_database_sha256", ""),
                }
            return result

        if files(present["files"]) != files(source["files"], select=True):
            raise ValueError("Running image file inventory differs from the release artifact")
