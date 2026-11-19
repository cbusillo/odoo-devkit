"""Inventory the assembled image without importing Odoo or loading a registry."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from collections.abc import Iterable
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree


def xml_database_references(elements: Any) -> list[str | None]:
    references = []
    for element in elements:
        if element.get("file"):
            references.append(element.get("file"))
        icon = element.get("web_icon", "")
        if element.tag == "field" and element.get("name") == "web_icon":
            icon = element.text or ""
            if element.get("eval"):
                try:
                    value = ast.literal_eval(element.get("eval"))
                    icon = value if isinstance(value, str) else ""
                except (ValueError, SyntaxError):
                    references.append(None)  # Computed resource paths cannot be inferred safely.
        name, separator, path = icon.strip().partition(",")
        if separator:
            references.append(name.strip() + "/" + path.strip())
    return references


def git_inventory(repository: Path, commit: str, module_name: str = "", *, product_odoo_version: str = "") -> list[dict[str, Any]]:
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
    if product_odoo_version:
        major = int(product_odoo_version.split(".")[0])
        verify_introduced_migrations(repository, commit, major)
        verify_product_source_migrations(content, major)
    return classify_files(content, {Path("."): module_name or repository.name})[0]


def classify_files(
    content: dict[str, bytes], module_aliases: dict[Path, str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    modules: dict[str, set[str]] = {}
    module_roots = {}
    database_files: set[Path] = set()
    for raw_path, value in content.items():
        path = Path(raw_path)
        if path.name != "__manifest__.py" or "tests" in path.parts:
            continue
        data = ast.literal_eval(value.decode())
        if data.get("installable", True):
            name = (module_aliases or {}).get(path.parent, path.parent.name)
            modules[name] = set(data.get("depends", []))
            module_roots[path.parent] = name
            database_files.update(path.parent / item for item in (*data.get("data", ()), *data.get("demo", ())))
    roots_by_name = {name: root for root, name in module_roots.items()}
    for path in tuple(database_files):
        if path.suffix != ".xml" or path.as_posix() not in content:
            continue
        try:
            references = xml_database_references(ElementTree.fromstring(content[path.as_posix()]).iter())
        except ElementTree.ParseError:
            continue  # The assembled-image scan marks this declaration incomplete.
        for reference in references:
            if reference is None:
                continue
            module, separator, relative = reference.partition("/")
            if separator and module in roots_by_name:
                database_files.add(roots_by_name[module] / relative)
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
        elif path in database_files and module:
            kind = "database_data"
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


class ModuleSources(NamedTuple):
    sha256: str
    version: str
    migrations: frozenset[str]


def module_sources(path: Path) -> ModuleSources:
    files, _ = inventory([path], root_module=path.name)
    prefix = path.as_posix().lstrip("/") + "/"
    hashes = {file["path"].removeprefix(prefix): file["sha256"] for file in files}
    sha256 = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = ast.literal_eval((path / "__manifest__.py").read_text())
    migrations = migration_folders(hashes)
    return ModuleSources(
        sha256, manifest.get("version", "1.0"), frozenset(path.as_posix() for path in migrations if len(path.parts) == 2)
    )


def migration_folders(paths: Iterable[str]) -> set[Path]:
    return {
        path.parent
        for item in paths
        if (path := Path(item)).parent.parent.name in {"migrations", "upgrades"}
        and path.parent.name != "tests"
        and path.name.startswith(("pre-", "post-", "end-"))
        and path.suffix == ".py"
    }


def version_key(version: str) -> tuple[int, ...]:
    parts = version.removeprefix("saas~").replace(".saas~", ".").split(".")
    while parts and int(parts[-1]) == 0:
        parts.pop()
    return tuple(map(int, parts))


def manifest_migration_version(version: str, major: int) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+){0,3}", version, re.ASCII):
        raise ValueError(f"Invalid Odoo manifest version {version!r}")
    series = f"{major}.0"
    if version.count(".") <= 2 and not version.startswith(series):
        version = f"{series}.{version}"
    if not version.startswith(f"{series}."):
        raise ValueError(f"Odoo manifest version {version!r} must start with {series}.")
    return _migration_order(version, major)


def migration_version(version: str, major: int) -> tuple[int, ...]:
    # https://github.com/odoo/odoo/blob/20.0/odoo/modules/migration.py
    prefix = r"(?:6\.1|(?:[6-9]|[1-9]\d+)\.0|(?:[7-9]|10)\.saas~[1-9]\d*|saas~(?:1[1-9]|[2-9]\d+)\.[1-9])"
    if not re.fullmatch(rf"(?:{prefix}\.)?\d+\.\d+(?:\.\d+)?", version, re.ASCII):
        raise ValueError(f"Invalid Odoo migration folder {version!r}")
    return _migration_order(version, major)


def _migration_order(version: str, major: int) -> tuple[int, ...]:
    if version.count(".") <= 2 and version != "0.0.0":
        version = f"{major}.0.{version}"
    return version_key(version)


def _product_migration_folders(paths: Iterable[str]) -> set[Path]:
    return {
        folder
        for folder in migration_folders(paths)
        if len(folder.parts) == 4
        and folder.is_relative_to("addons")
        and not folder.is_relative_to("addons/shared")
        and folder.name != "0.0.0"
    }


def _check_migration_folder(folder: Path, manifest: bytes, major: int, *, introduced: bool) -> None:
    version = ast.literal_eval(manifest.decode()).get("version", "1.0")
    candidate = manifest_migration_version(version, major)
    target = migration_version(folder.name, major)
    if target > candidate or (introduced and target != candidate):
        raise ValueError(
            f"Addon {folder.parent.parent.name} migration folder {folder.name} does not match manifest version {version}."
        )


def verify_product_source_migrations(content: dict[str, bytes], major: int) -> None:
    for folder in sorted(_product_migration_folders(content)):
        manifest = content.get((folder.parent.parent / "__manifest__.py").as_posix())
        if manifest is not None:
            _check_migration_folder(folder, manifest, major, introduced=False)


def verify_introduced_migrations(repository: Path, commit: str, major: int) -> None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    pull_request = event.get("pull_request", {})
    if (pull_request.get("head", {}).get("sha") if pull_request else event.get("after")) != commit:
        return
    baseline = pull_request.get("base", {}).get("sha") or event.get("before")
    if not isinstance(baseline, str) or not re.fullmatch(r"[0-9a-f]{40}", baseline):
        return
    comparison = Path(__file__).resolve().parents[2] / ".github/actions/addon-tests/comparison-base.sh"
    baseline = subprocess.check_output(["bash", str(comparison), str(repository), baseline], text=True).strip()
    git = partial(_migration_git, repository)
    for entry in git("rev-list", "--reverse", "--topo-order", "--parents", f"{baseline}..{commit}").splitlines():
        revision, *parents = entry.split()
        base = parents[:1] or ["--root"]
        added = git(
            "diff-tree",
            "--no-commit-id",
            "--no-renames",
            "--diff-filter=A",
            "--name-only",
            "-r",
            "-z",
            *base,
            revision,
            "--",
            "addons",
        )
        folders = _product_migration_folders(added.split("\0"))
        if not folders:
            continue
        for parent in parents:
            if not folders:
                break
            earlier = git("ls-tree", "-r", "-z", "--name-only", parent, "--", *map(str, sorted(folders)))
            folders.difference_update(migration_folders(earlier.split("\0")))
        for folder in sorted(folders):
            path = (folder.parent.parent / "__manifest__.py").as_posix()
            manifest = git("ls-tree", "--name-only", revision, "--", path)
            if manifest:
                _check_migration_folder(folder, git("show", f"{revision}:{path}").encode(), major, introduced=True)


def _migration_git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True)


def module_paths(roots: list[Path]) -> dict[str, Path]:
    """Match Odoo's exposed addon namespaces and first-path precedence."""
    paths: dict[str, Path] = {}
    expand_addon_paths = runpy.run_path(str(Path(__file__).with_name("odoo_addon_paths.py")))["expand_addon_paths"]
    for root in expand_addon_paths(roots):
        for manifest in sorted(root.glob("*/__manifest__.py")):
            name = manifest.parent.name
            if name in paths:
                continue
            data = ast.literal_eval(manifest.read_text())
            if data.get("installable", True):
                paths[name] = manifest.parent
    return paths


def module_graph(roots: list[Path]) -> dict[str, set[str]]:
    return {
        name: set(ast.literal_eval((path / "__manifest__.py").read_text()).get("depends", []))
        for name, path in module_paths(roots).items()
    }


def database_loaded_files(paths: dict[str, Path]) -> tuple[set[Path], bool]:
    """Include binary attachments loaded through XML across source boundaries."""
    loaded: set[Path] = set()
    complete = True
    for root in paths.values():
        data = ast.literal_eval((root / "__manifest__.py").read_text())
        for relative in (*data.get("data", ()), *data.get("demo", ())):
            file = root / relative
            loaded.add(file.resolve())
            if file.suffix != ".xml":
                continue
            try:
                references = xml_database_references(ElementTree.parse(file).iter())
            except (OSError, ElementTree.ParseError):
                complete = False
                print(f"Incomplete release inventory: {root.name}/{relative} XML is missing or unparsable", file=sys.stderr)
                continue
            for reference in references:
                if reference is None:
                    complete = False
                    print(f"Incomplete release inventory: {root.name}/{relative} has a computed icon path", file=sys.stderr)
                    continue
                name, separator, remainder = reference.partition("/")
                if separator and name in paths:
                    loaded.add((paths[name] / remainder).resolve())
    return loaded, complete


def build_inventory(metadata: dict[str, Any]) -> dict[str, Any]:
    sources = []
    source_roots: dict[str, list[Path]] = {}
    data_complete = True
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
        source_roots[source["input_name"]] = roots
        if any(not root.is_dir() for root in roots):
            raise ValueError("Missing release source root")
        files, modules = (
            ([], {})
            if "addon_paths" in metadata and ("files" in source or source.get("files_from"))
            else inventory(roots, [Path(path) for path in source.get("exclude", [])], source.get("root_module", ""))
        )
        source_files = source.get("files", files)
        if source.get("files_from"):
            source_files = json.loads(Path(source["files_from"]).read_text())
        if source["input_name"] == "base:devtools":
            source_files = [{**item, "module": "", "kind": "dependency"} for item in source_files]
        if source["input_name"] == "tenant":
            source_files = [item for item in source_files if not item["path"].startswith("addons/shared/")]
        sources.append({k: source[k] for k in ("input_name", "repository", "commit")} | {"files": source_files})
        for name, dependencies in ({} if "addon_paths" in metadata else modules).items():
            if name in graph and graph[name] != dependencies:
                raise ValueError("Conflicting release module graph")
            graph[name] = dependencies
    if len({source["input_name"] for source in sources}) != len(sources):
        raise ValueError("Duplicate release source input names")
    if "addon_paths" in metadata:
        roots = [Path(path) for path in metadata["addon_paths"]]
        roots += [Path(path) for path in ("/odoo/addons", "/odoo/odoo/addons") if Path(path) not in roots]
        graph = module_graph(roots)
        loaded, data_complete = database_loaded_files(module_paths(roots))
        for source in sources:
            source["files"] = [
                {**item, "module": "", "kind": "dependency", "manifest_database_sha256": ""}
                if item.get("module") and item["module"] not in graph
                else item
                for item in source["files"]
            ]
            for item in source["files"]:
                if not item.get("module"):
                    continue
                for root in source_roots[source["input_name"]]:
                    prefix = root.as_posix().lstrip("/") + "/"
                    physical = Path("/" + item["path"]) if item["path"].startswith(prefix) else root / item["path"]
                    if physical.resolve() in loaded:
                        item["kind"] = "database_data"
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
                    "files": [item for item in origin["files"] if item["path"].endswith(external["dependency_file_path"])],
                }
            )
    complete = data_complete and all(not dependencies - graph.keys() for dependencies in graph.values())
    examined = metadata.get("examined_input_plan") or {}
    if examined:
        fingerprints = [examined.get(field) for field in ("examined_inputs_sha256", "examined_baseline_inputs_sha256")]
        modules = examined.get("database_update_modules")
        if (
            set(examined) != {"examined_inputs_sha256", "examined_baseline_inputs_sha256", "database_update_modules"}
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in fingerprints)
            or not isinstance(modules, list)
            or any(not isinstance(name, str) or name not in graph for name in modules)
            or len(modules) != len(set(modules))
        ):
            raise ValueError(
                "Examined input plan requires canonical candidate/baseline fingerprints and explicit known module names"
            )
    for source in sources:
        if source["input_name"] in {"base:runtime", "base:devtools"}:
            files = sorted(source["files"], key=lambda item: item["path"])
            if len({item["path"] for item in files}) != len(files):
                raise ValueError("Duplicate base inventory paths")
            # Base changes already require Launchplane's examined-input plan.
            # Bind every file and its classification without publishing the
            # entire framework/Enterprise tree as individual manifest records.
            source["files"] = [
                {
                    "path": "source-inventory.sha256",
                    "sha256": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "kind": "dependency",
                    "module": "",
                    "manifest_database_sha256": "",
                }
            ]
        else:
            # Git permits spaces and Unicode in names; Launchplane's inventory
            # paths do not. Keep safe components and suffixes for classification,
            # and bind each unsupported component to its exact original name.
            source["files"] = [
                {
                    **item,
                    "path": "/".join(
                        component
                        if re.fullmatch(r"[A-Za-z0-9._-]+", component)
                        else "name-"
                        + hashlib.sha256(component.encode()).hexdigest()
                        + (Path(component).suffix if re.fullmatch(r"[.][A-Za-z0-9_-]+", Path(component).suffix) else "")
                        for component in item["path"].split("/")
                    ),
                }
                for item in source["files"]
            ]
    return {
        "schema_version": 1,
        "complete": complete,
        "read_write_compatible": True,
        "sources": sources,
        "modules": [{"name": name, "depends": sorted(dependencies)} for name, dependencies in sorted(graph.items())],
        "examined_inputs_sha256": examined.get("examined_inputs_sha256", ""),
        "examined_baseline_inputs_sha256": examined.get("examined_baseline_inputs_sha256", ""),
        "database_update_modules": examined.get("database_update_modules"),
    }


if __name__ == "__main__" and sys.argv[1] in {"--base", "--base-tools"}:
    base_paths = (
        ("/usr/local/bin",)
        if sys.argv[1] == "--base-tools"
        else ("/odoo", "/usr/local/bin", "/opt/launchplane/addons", "/opt/enterprise")
    )
    base_roots = [Path(path) for path in base_paths if Path(path).is_dir()]
    Path(sys.argv[2]).write_text(json.dumps(inventory(base_roots)[0], sort_keys=True))
elif __name__ == "__main__" and sys.argv[1] == "--git":
    source_name = sys.argv[4] if len(sys.argv) > 4 else ""
    product_series = sys.argv[5] if len(sys.argv) > 5 else ""
    print(
        json.dumps(git_inventory(Path(sys.argv[2]), sys.argv[3], source_name, product_odoo_version=product_series), sort_keys=True)
    )
elif __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(build_inventory(json.loads(Path(sys.argv[1]).read_text())), sort_keys=True))
