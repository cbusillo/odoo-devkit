from __future__ import annotations

import ast
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


def _comparison_base(repository: Path, candidate: str, source_token: str | None, comparison: str, local: bool) -> str:
    environment = os.environ.copy()
    if source_token:
        environment["STYLE_CHECK_TOKEN"] = source_token
    try:
        return subprocess.run(
            ["bash", str(_COMPARISON), str(repository), comparison, candidate, "local" if local else "fetch"],
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


def _git_addon_tree(repository: Path, revision: str | None) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[tuple[str, str]]]]:
    listing = ("ls-tree", "-rz", "--name-only", revision) if revision else ("ls-files", "-z")
    paths = [PurePosixPath(name) for name in _git(repository, *listing, "--", "addons").split("\0") if name]
    if revision is None:
        paths = [path for path in paths if (repository / path).is_file()]
    manifests = {}
    folders = {}
    with tempfile.TemporaryDirectory(prefix="devkit-migration-tree-") as directory:
        checkout = Path(directory)
        for path in paths:
            if path.name in {"__manifest__.py", "__init__.py", "__openerp__.py"} and len(path.parts) <= 4:
                target = checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                content = ""
                if path.name == "__manifest__.py":
                    content = _git(repository, "show", f"{revision}:{path}") if revision else (repository / path).read_text()
                target.write_text(content)
        root = checkout / "addons"
        groups = runpy.run_path(
            str(
                _INVENTORY.with_name("odoo_addon_paths.py")
                if _INVENTORY.name == "odoo_release_inventory.py"
                else _INVENTORY.with_name("addon_paths.py")
            )
        )["addon_group_dirs"](root)
        scripts = {path.parent for path in paths if _migration_folder(path)}
        try:
            modules = _PRODUCT_PATHS([root, *groups], root)
        except ValueError as error:
            raise ValueError(f"Cannot validate migrations at {revision or 'working tree'}: {error}") from error
        for name, module in modules.items():
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
        start = 2 if folder.count(".") <= 2 else 0
        if introduced and any(actual[start:] <= _older_version(old, series)[start:] for old in previous):
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
    local: bool = False,
) -> None:
    if not re.fullmatch(r"[0-9]+[.]0", series):
        raise ValueError(f"Unsupported Odoo migration series: {series}")
    head = _git(repository, "rev-parse", "--verify", "--end-of-options", f"{candidate}^{{commit}}")

    @cache
    def tree(revision: str | None) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[tuple[str, str]]]]:
        return _git_addon_tree(repository, revision)

    manifests, folders = tree(head)
    if local and _git(repository, "diff", "--name-only", "HEAD", "--", "addons"):
        working_manifests, working_folders = tree(None)
        for name, entries in working_folders.items():
            for directory, folder in entries:
                _validate_folder(
                    name,
                    folder,
                    working_manifests[name].get("version", "1.0"),
                    series,
                    introduced=(directory, folder) not in folders.get(name, set()),
                    previous=[manifests[name].get("version", "1.0")] if name in manifests else [],
                )
    for name, module_folders in folders.items():
        for _directory, folder in sorted(module_folders):
            _validate_folder(name, folder, manifests[name].get("version", "1.0"), series, introduced=False, previous=[])
    if not any(folders.values()):
        return
    if base is None and local and not os.environ.get("GITHUB_EVENT_PATH"):
        return
    comparison = base or _comparison_base(repository, head, source_token, "", local)
    if comparison == head:
        return
    if _git(repository, "rev-parse", "--is-shallow-repository") == "true":
        if not local:
            comparison = _comparison_base(repository, head, source_token, comparison, local)
        if _git(repository, "rev-parse", "--is-shallow-repository") == "true":
            raise ValueError(f"Cannot obtain complete migration comparison history for {head}")
    try:
        base = _git(repository, "merge-base", comparison, head)
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
