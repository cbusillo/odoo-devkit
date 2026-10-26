from __future__ import annotations

import subprocess
from pathlib import Path
from xml.etree import ElementTree

from .artifact_inputs import load_artifact_inputs_definition
from .artifact_provenance import normalize_git_commit, normalize_repository_identity
from .dependency_workspace import check_source_dependency_workspace
from .local_runtime import manifest_build_values, resolve_manifest_shared_addons_repo_path
from .manifest import WorkspaceManifest


def check_workspace_source(*, manifest: WorkspaceManifest) -> dict[str, object]:
    manifest_build_values(manifest, required=True)
    declared_sources = _check_source_declarations(manifest)
    inspection = check_source_dependency_workspace(manifest=manifest)
    tenant_root = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    assert tenant_root is not None
    roots = {tenant_root}
    shared_root = resolve_manifest_shared_addons_repo_path(manifest=manifest)
    if shared_root is not None:
        roots.add(shared_root)
    syntax_files = sum(_check_source_syntax(root) for root in sorted(roots))
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
        if source.exact_ref:
            normalize_git_commit(source.exact_ref)
            continue
        result = subprocess.run(["git", "check-ref-format", f"refs/heads/{source.selector}"], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("Artifact input selector is not a valid Git ref")
    return len(sources)


def _check_source_syntax(root: Path) -> int:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True)
    count = 0
    for name in result.stdout.split("\0"):
        path = root / name
        if path.suffix not in {".py", ".xml"}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"Source syntax requires a regular repository file: {name}")
        try:
            if path.suffix == ".py":
                compile(path.read_bytes(), name, "exec")
            else:
                ElementTree.parse(path)
        except (SyntaxError, ElementTree.ParseError, ValueError) as error:
            raise ValueError(f"Invalid source syntax: {name}") from error
        count += 1
    return count
