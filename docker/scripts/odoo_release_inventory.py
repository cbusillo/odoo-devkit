"""Inventory the assembled image without importing Odoo or loading a registry."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def git_inventory(repository: Path, commit: str, module_name: str = "") -> list[dict[str, Any]]:
    """Hash the exact committed tree, including build files and symlink blobs."""
    tree = subprocess.run(["git", "-C", str(repository), "ls-tree", "-rz", commit], capture_output=True, check=True).stdout
    entries = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if kind != b"blob":
            raise ValueError("Release inventories cannot omit git submodule inputs")
        entries.append((mode, oid, path.decode()))
    blobs = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input=b"\n".join(oid for _, oid, _ in entries) + b"\n",
        capture_output=True,
        check=True,
    ).stdout
    content = {}
    offset = 0
    for _mode, _oid, path in entries:
        end = blobs.index(b"\n", offset)
        size = int(blobs[offset:end].split()[-1])
        content[path] = blobs[end + 1 : end + 1 + size]
        offset = end + size + 2
    return classify_files(content, {Path("."): module_name or repository.name})[0]


def classify_files(
    content: dict[str, bytes], module_aliases: dict[Path, str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    modules: dict[str, set[str]] = {}
    module_roots = {}
    for raw_path, value in content.items():
        path = Path(raw_path)
        if path.name != "__manifest__.py" or "tests" in path.parts:
            continue
        data = ast.literal_eval(value.decode())
        if data.get("installable", True):
            name = (module_aliases or {}).get(path.parent, path.parent.name)
            modules[name] = set(data.get("depends", []))
            module_roots[path.parent] = name
    files = []
    for raw_path, value in sorted(content.items()):
        path = Path(raw_path)
        module = next((module_roots[parent] for parent in path.parents if parent in module_roots), "")
        semantic = ""
        if path.name == "__manifest__.py" and module:
            data = ast.literal_eval(value.decode())
            semantic = hashlib.sha256(
                json.dumps({k: v for k, v in data.items() if k != "assets"}, sort_keys=True).encode()
            ).hexdigest()
            kind = "manifest_assets"
        elif "static" in path.parts and module:
            kind = "static"
        elif path.suffix in {".md", ".rst", ".adoc"} or {"doc", "docs", ".github", "tests"} & set(path.parts):
            kind = "docs_ci"
        elif module:
            kind = "migration" if "migrations" in path.parts else "model" if path.suffix == ".py" else "database_data"
        else:
            kind = "dependency"
        files.append(
            {
                "path": raw_path,
                "sha256": hashlib.sha256(value).hexdigest(),
                "kind": kind,
                "module": module,
                "manifest_database_sha256": semantic,
            }
        )
    return files, modules


def inventory(
    roots: list[Path], exclude: list[Path] | None = None, root_module: str = ""
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    content = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if (
                {".git", "__pycache__"} & set(path.parts)
                or path.suffix == ".pyc"
                or any(path.is_relative_to(item) for item in exclude or [])
            ):
                continue
            if path.is_symlink():
                value = os.readlink(path).encode()
            elif path.is_file():
                value = path.read_bytes()
            else:
                continue
            content[root.as_posix().lstrip("/") + "/" + path.relative_to(root).as_posix()] = value
    aliases = {Path(root.as_posix().lstrip("/")): root_module for root in roots} if root_module else {}
    return classify_files(content, aliases)


def build_inventory(metadata: dict[str, Any]) -> dict[str, Any]:
    sources = []
    graph: dict[str, set[str]] = {}
    for source in metadata["sources"]:
        roots = [Path(path) for path in source["roots"]]
        if source.get("checkout_root"):
            expected = f"{source['repository']}@{source['commit']}"
            matches = [
                Path(str(marker).removesuffix(".odoo-source"))
                for marker in Path(source["checkout_root"]).glob("*.odoo-source")
                if marker.read_text().strip() == expected
            ]
            if len(matches) != 1:
                raise ValueError("Missing or ambiguous exact fetched addon checkout")
            roots += matches
        roots += [Path(path) for path in source.get("optional_roots", []) if Path(path).is_dir()]
        if any(not root.is_dir() for root in roots):
            raise ValueError("Missing release source root")
        files, modules = inventory(roots, [Path(path) for path in source.get("exclude", [])], source.get("root_module", ""))
        source_files = source.get("files", files)
        if source.get("files_from"):
            source_files = json.loads(Path(source["files_from"]).read_text())
        if source["input_name"] == "base:devtools":
            source_files = [{**item, "module": "", "kind": "dependency"} for item in source_files]
        sources.append({k: source[k] for k in ("input_name", "repository", "commit")} | {"files": source_files})
        for name, dependencies in modules.items():
            if name in graph and graph[name] != dependencies:
                raise ValueError("Conflicting release module graph")
            graph[name] = dependencies
    dependency_evidence = metadata.get("dependency_evidence")
    if dependency_evidence:
        for external in json.loads(Path(dependency_evidence).read_text())["external_compatibility_inputs"]:
            repository = external["source_repository"]
            origin = next(
                (item for item in sources if item["repository"] == repository and item["commit"] == external["source_ref"]), None
            )
            if origin is None:
                raise ValueError("External dependency source is not inventoried")
            sources.append(
                {
                    **origin,
                    "input_name": f"external:{repository}:{external['dependency_file_path']}",
                }
            )
    complete = all(not dependencies - graph.keys() for dependencies in graph.values())
    return {
        "schema_version": 1,
        "complete": complete,
        "read_write_compatible": True,
        "sources": sources,
        "modules": [{"name": name, "depends": sorted(dependencies)} for name, dependencies in sorted(graph.items())],
        "examined_inputs_sha256": "",
        "database_update_modules": None,
    }


if __name__ == "__main__" and sys.argv[1] == "--base":
    roots = [Path(path) for path in ("/odoo", "/usr/local/bin", "/opt/launchplane/addons", "/opt/enterprise") if Path(path).is_dir()]
    Path(sys.argv[2]).write_text(json.dumps(inventory(roots)[0], sort_keys=True))
elif __name__ == "__main__" and sys.argv[1] == "--git":
    print(json.dumps(git_inventory(Path(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""), sort_keys=True))
elif __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(build_inventory(json.loads(Path(sys.argv[1]).read_text())), sort_keys=True))
