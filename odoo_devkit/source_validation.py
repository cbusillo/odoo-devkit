from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from .artifact_inputs import load_artifact_inputs_definition
from .artifact_provenance import normalize_git_commit, normalize_repository_identity
from .dependency_workspace import check_source_dependency_workspace
from .local_runtime import GIT_SHA_PATTERN, manifest_build_values, resolve_manifest_shared_addons_repo_path
from .manifest import WorkspaceManifest


def check_workspace_source(*, manifest: WorkspaceManifest) -> dict[str, object]:
    build = manifest_build_values(manifest, required=True)
    declared_sources = _check_source_declarations(manifest)
    inspection = check_source_dependency_workspace(manifest=manifest)
    tenant_root = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    assert tenant_root is not None
    shared_root = resolve_manifest_shared_addons_repo_path(manifest=manifest)
    syntax_files = _check_source_syntax(tenant_root, odoo_series=build["ODOO_VERSION"])
    if shared_root is not None and shared_root != tenant_root:
        syntax_files += _check_source_syntax(shared_root)
    return {
        "validation": "source-only",
        "tenant": manifest.tenant,
        "full_build_required": True,
        "declared_sources": declared_sources,
        "syntax_files": syntax_files,
        "dependencies": inspection.to_dict(),
    }


def _check_source_declarations(manifest: WorkspaceManifest) -> int:
    definition = load_artifact_inputs_definition(manifest=manifest)
    if definition is None:
        if manifest.artifacts.inputs_file:
            raise ValueError("Configured artifact input file is missing")
        return 0
    sources = list(definition.sources)
    for context in definition.contexts.values():
        sources.extend(context.sources_add)
        for instance in context.instances.values():
            sources.extend(instance.sources_add)
    for source in sources:
        normalize_repository_identity(source.repository)
        exact_ref = source.exact_ref or (source.selector if GIT_SHA_PATTERN.fullmatch(source.selector or "") else None)
        if exact_ref:
            normalize_git_commit(exact_ref)
            continue
        result = subprocess.run(["git", "check-ref-format", f"refs/heads/{source.selector}"], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("Artifact input selector is not a valid Git ref")
    return len(sources)


def _check_source_syntax(root: Path, *, odoo_series: str | None = None) -> int:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True)
    sources = {Path(name) for name in result.stdout.split("\0") if name.endswith((".py", ".xml"))}
    resolved_root = root.resolve()
    for name in sorted(sources):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(resolved_root) or not path.is_file():
            raise ValueError(f"Source syntax requires a regular repository file: {name}")
        try:
            if path.suffix == ".py":
                compile(path.read_bytes(), str(name), "exec")
            else:
                ElementTree.parse(path)
        except (SyntaxError, ElementTree.ParseError, ValueError) as error:
            raise ValueError(f"Invalid source syntax: {name}") from error
    if odoo_series is not None:
        _check_migration_versions(root, sources, odoo_series)
    return len(sources)


# https://github.com/odoo/odoo/blob/20.0/odoo/modules/migration.py
_MIGRATION_VERSION = re.compile(
    r"(?:(?:6\.1|[6-9]\.0|[1-9][0-9]+\.0|"
    r"(?:[7-9]|10)\.saas~[1-9][0-9]*|saas~(?:1[1-9]|[2-9][0-9])\.[1-9])\.)?"
    r"[0-9]+\.[0-9]+(?:\.[0-9]+)?"
)


def _check_migration_versions(root: Path, sources: set[Path], series: str) -> None:
    folders = {
        path.parent
        for path in sources
        if path.parts[0] == "addons"
        and path.parts[1] != "shared"
        and path.parent.parent.name in {"migrations", "upgrades"}
        and path.suffix == ".py"
        and path.name.startswith(("pre-", "post-", "end-"))
        and path.parent.name != "0.0.0"
        and _MIGRATION_VERSION.fullmatch(path.parent.name)
    }
    for folder in sorted(folders):
        manifest = folder.parent.parent / "__manifest__.py"
        if manifest not in sources:
            continue
        try:
            values = ast.literal_eval((root / manifest).read_text(encoding="utf-8"))
        except (SyntaxError, ValueError) as error:
            raise ValueError(f"Invalid addon manifest: {manifest}") from error
        if not isinstance(values, dict):
            raise ValueError(f"Invalid addon manifest: {manifest}")
        version = str(values.get("version", "1.0"))
        # https://github.com/odoo/odoo/blob/20.0/odoo/modules/module.py
        adapted = f"{series}.{version}" if version.count(".") <= 2 and not version.startswith(series) else version
        if _odoo_migration_version_key(folder.name, series) > _odoo_migration_version_key(adapted, series):
            raise ValueError(f"Migration folder {folder} is newer than manifest version {version} in {manifest}")


def _odoo_migration_version_key(version: str, series: str) -> tuple[str, ...]:
    full_version = f"{series}.{version}" if version.count(".") <= 2 else version
    # https://github.com/odoo/odoo/blob/20.0/odoo/tools/parse_version.py
    parts = [part.zfill(8) for part in full_version.replace("saas~", "").split(".")]
    while parts and parts[-1] == "00000000":
        parts.pop()
    return tuple(parts)
