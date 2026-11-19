from __future__ import annotations

import ast
import json
import os
import re
import runpy
import subprocess
import tempfile
from collections.abc import Mapping
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_INVENTORY = _REPO / "docker/scripts/odoo_release_inventory.py"
_COMPARISON = _REPO / ".github/actions/addon-tests/comparison-base.sh"
if not _INVENTORY.is_file():
    _INVENTORY = Path(__file__).with_name("release_inventory.py")
    _COMPARISON = Path(__file__).with_name("comparison-base.sh")

_PRODUCT_PATHS = runpy.run_path(str(_INVENTORY))["product_module_paths"]
_MIGRATION_SCRIPT = re.compile(r"(?:pre|post|end)-.*[.]py")
_FOLDER_VERSION = re.compile(r"(?:(?:6[.]1|[6-9][.]0|[1-9][0-9]+[.]0)[.])?[0-9]+[.][0-9]+(?:[.][0-9]+)?")
_SENTINEL = "0.0.0"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=repository, text=True, capture_output=True, check=True).stdout.strip()


def _comparison_base(repository: Path, candidate: str, source_token: str | None, comparison: str = "") -> str:
    environment = os.environ.copy()
    if source_token:
        environment["STYLE_CHECK_TOKEN"] = source_token
    event_path = environment.get("GITHUB_EVENT_PATH")
    if event_path and not comparison:
        event = json.loads(Path(event_path).read_text())
        pull_request = event.get("pull_request", {})
        if environment.get("GITHUB_EVENT_NAME") == "pull_request" and candidate in {
            pull_request.get("head", {}).get("sha"),
            environment.get("GITHUB_SHA"),
        }:
            comparison = pull_request["base"]["sha"]
        elif environment.get("GITHUB_EVENT_NAME") == "push" and candidate == event.get("after"):
            comparison = event.get("before", "")
    environment.pop("GITHUB_EVENT_PATH", None)
    try:
        return subprocess.run(
            ["bash", str(_COMPARISON), str(repository), comparison],
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Cannot resolve migration comparison history for {candidate}: {error.stderr.strip()}") from error


def _version(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in value.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _manifest_version(raw: str, series: str) -> tuple[int, ...]:
    parts = raw.split(".")
    if not 2 <= len(parts) <= 5 or not all(re.fullmatch(r"[0-9]+", part) for part in parts):
        raise ValueError(f"Invalid addon manifest version {raw!r}")
    value = f"{series}.{raw}" if len(parts) <= 3 and not raw.startswith(series) else raw
    if not value.startswith(series + "."):
        raise ValueError(f"Invalid addon manifest version {raw!r} for Odoo {series}")
    return _version(value)


def _folder_version(folder: str, series: str) -> tuple[int, ...]:
    if not _FOLDER_VERSION.fullmatch(folder):
        raise ValueError("invalid native folder version")
    return _version(folder if folder.count(".") > 2 else f"{series}.{folder}")


def _migration_folder(path: PurePosixPath) -> bool:
    return path.parent.parent.name in {"migrations", "upgrades"} and _MIGRATION_SCRIPT.fullmatch(path.name) is not None


def _git_addon_tree(repository: Path, commit: str) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[tuple[str, str]]]]:
    paths = [PurePosixPath(name) for name in _git(repository, "ls-tree", "-rz", "--name-only", commit, "--", "addons").split("\0")]
    manifests = {}
    folders = {}
    with tempfile.TemporaryDirectory(prefix="devkit-migration-tree-") as directory:
        checkout = Path(directory)
        for path in paths:
            if path.name in {"__manifest__.py", "__init__.py", "__openerp__.py"} and len(path.parts) <= 4:
                target = checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_git(repository, "show", f"{commit}:{path}") if path.name == "__manifest__.py" else "")
        root = checkout / "addons"
        groups = runpy.run_path(
            str(
                _INVENTORY.with_name("odoo_addon_paths.py")
                if _INVENTORY.name == "odoo_release_inventory.py"
                else _INVENTORY.with_name("addon_paths.py")
            )
        )["addon_group_dirs"](root)
        scripts = {path.parent for path in paths if _migration_folder(path)}
        for name, module in _PRODUCT_PATHS([root, *groups], root).items():
            manifests[name] = ast.literal_eval((module / "__manifest__.py").read_text())
            relative = PurePosixPath(module.relative_to(checkout))
            folders[name] = {(script.parent.name, script.name) for script in scripts if script.parent.parent == relative}
    return manifests, folders


def _older_version(raw: str, series: str) -> tuple[int, ...]:
    return _version(raw) if raw.count(".") > 2 else _manifest_version(raw, series)


def _validate_folder(name: str, folder: str, version: str, series: str, *, introduced: bool, previous: list[str]) -> None:
    if folder == _SENTINEL:
        return
    try:
        expected = _manifest_version(version, series)
        actual = _folder_version(folder, series)
        if actual > expected or (introduced and actual != expected):
            raise ValueError("folder must match the introducing manifest version")
        scope = slice(2, None) if folder.count(".") <= 2 else slice(None)
        if introduced and any(actual[scope] <= _older_version(old, series)[scope] for old in previous):
            raise ValueError("version must advance beyond the previously deliverable manifest")
    except ValueError as error:
        raise ValueError(f"Addon {name} migration {folder} with manifest {version}: {error}") from error


def validate_product_migrations(
    repository: Path,
    candidate: str,
    series: str,
    *,
    base: str | None = None,
    source_token: str | None = None,
) -> None:
    if not re.fullmatch(r"[0-9]+[.]0", series):
        raise ValueError(f"Unsupported Odoo migration series: {series}")
    head = _git(repository, "rev-parse", "--verify", "--end-of-options", f"{candidate}^{{commit}}")

    @cache
    def tree(commit: str) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[tuple[str, str]]]]:
        return _git_addon_tree(repository, commit)

    manifests, folders = tree(head)
    for name, module_folders in folders.items():
        for _directory, folder in sorted(module_folders):
            _validate_folder(name, folder, manifests[name].get("version", "1.0"), series, introduced=False, previous=[])
    if not any(folders.values()):
        return
    base = base if base is not None else _comparison_base(repository, head, source_token)
    if _git(repository, "rev-parse", "--is-shallow-repository") == "true":
        base = _comparison_base(repository, head, source_token, base) if base != head else head
        if base != head and _git(repository, "rev-parse", "--is-shallow-repository") == "true":
            raise ValueError(f"Cannot obtain complete migration comparison history for {head}")
    try:
        base = _git(repository, "merge-base", base, head)
        changes = _git(repository, "rev-list", "--reverse", f"{base}..{head}", "--").splitlines()
        base_manifests, _ = tree(base)
        for commit in changes:
            manifests, folders = tree(commit)
            parents = [tree(parent) for parent in _git(repository, "show", "-s", "--format=%P", commit).split()]
            for name, module_folders in folders.items():
                inherited = set().union(*(parent_folders.get(name, set()) for _, parent_folders in parents))
                previous = [
                    modules[name].get("version", "1.0")
                    for modules in (base_manifests, *(parent_manifests for parent_manifests, _ in parents))
                    if name in modules
                ]
                for _directory, folder in sorted(module_folders - inherited):
                    _validate_folder(name, folder, manifests[name].get("version", "1.0"), series, introduced=True, previous=previous)
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Cannot compare migration history for {head}: {error.stderr.strip()}") from error
