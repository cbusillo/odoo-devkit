from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .manifest import WorkspaceManifest

DependencyProjectOwner = Literal["tenant", "shared_addons"]

_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_EXACT_BUILD_REQUIREMENT_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
_VCS_REFERENCE_PATTERN = re.compile(r"git\+(?:https|ssh)://[^\s]+@([^#\s]+)", re.IGNORECASE)
_DIRECT_REFERENCE_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._,-]+\])?\s*@\s*(?P<url>\S+)",
    re.IGNORECASE,
)
_UNSAFE_DIRECT_REFERENCE_PATTERN = re.compile(
    r"(?:^|\s)(?:-e\s+|--editable\s+|file:|\.\.?/|/)",
    re.IGNORECASE,
)
_IGNORED_PATH_PARTS = frozenset({"__pycache__", "build", "dist"})
_FORBIDDEN_UV_SOURCE_KEYS = frozenset(
    {
        "allow-insecure-host",
        "default-index",
        "dependency-metadata",
        "extra-index-url",
        "find-links",
        "index",
        "index-url",
        "keyring-provider",
        "no-index",
    }
)
_UV_DEPENDENCY_LIST_KEYS = (
    "build-constraint-dependencies",
    "constraint-dependencies",
    "dev-dependencies",
    "override-dependencies",
)


class DependencyWorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class DependencyProjectInspection:
    owner: DependencyProjectOwner
    path: str
    name: str
    runtime_dependencies: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "path": self.path,
            "name": self.name,
            "runtime_dependencies": list(self.runtime_dependencies),
        }


@dataclass(frozen=True)
class DependencyWorkspaceInspection:
    tenant: str
    current: bool
    publishable: bool
    requires_tenant_lock: bool
    tenant_root_pyproject_present: bool
    tenant_lock_present: bool
    tenant_lock_current: bool | None
    tenant_lock_sha256: str
    workspace_members: tuple[str, ...]
    projects: tuple[DependencyProjectInspection, ...]
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant": self.tenant,
            "current": self.current,
            "publishable": self.publishable,
            "requires_tenant_lock": self.requires_tenant_lock,
            "tenant_root_pyproject_present": self.tenant_root_pyproject_present,
            "tenant_lock_present": self.tenant_lock_present,
            "tenant_lock_current": self.tenant_lock_current,
            "tenant_lock_sha256": self.tenant_lock_sha256,
            "workspace_members": list(self.workspace_members),
            "projects": [project.to_dict() for project in self.projects],
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class _ProjectInput:
    owner: DependencyProjectOwner
    source_repo_path: Path
    source_pyproject_path: Path
    staged_pyproject_path: Path


def inspect_dependency_workspace(*, manifest: WorkspaceManifest) -> DependencyWorkspaceInspection:
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if tenant_repo_path is None or not tenant_repo_path.is_dir():
        raise DependencyWorkspaceError("Tenant repo path must exist before dependency inspection.")
    shared_addons_repo_path = _resolve_shared_addons_repo_path(manifest)
    if manifest.shared_addons_repo is not None and (shared_addons_repo_path is None or not shared_addons_repo_path.is_dir()):
        raise DependencyWorkspaceError(
            "Shared addons repo must be materialized before dependency inspection. Run `platform workspace sync` first."
        )
    devkit_repo_path = _resolve_devkit_repo_path(manifest)
    if manifest.devkit_repo is not None and (devkit_repo_path is None or not devkit_repo_path.is_dir()):
        raise DependencyWorkspaceError(
            "Devkit repo must be materialized before dependency inspection. Run `platform workspace sync` first."
        )

    tenant_repo_path = tenant_repo_path.resolve()
    shared_addons_repo_path = shared_addons_repo_path.resolve() if shared_addons_repo_path is not None else None
    devkit_repo_path = devkit_repo_path.resolve() if devkit_repo_path is not None else None
    project_inputs = _discover_project_inputs(
        tenant_repo_path=tenant_repo_path,
        shared_addons_repo_path=shared_addons_repo_path,
    )
    findings: list[str] = []
    findings.extend(
        _reserved_dependency_namespace_findings(
            tenant_repo_path=tenant_repo_path,
            shared_addons_repo_path=shared_addons_repo_path,
        )
    )
    projects: list[DependencyProjectInspection] = []
    staged_project_paths: set[str] = set()
    requires_tenant_lock = False

    for project_input in project_inputs:
        project_path = project_input.staged_pyproject_path.as_posix()
        if project_path in staged_project_paths:
            findings.append(f"Duplicate staged dependency project path: {project_path}")
            continue
        staged_project_paths.add(project_path)
        try:
            payload = _load_pyproject(project_input.source_pyproject_path)
            runtime_dependencies = _runtime_dependencies(payload=payload, display_path=project_path)
            requires_tenant_lock = requires_tenant_lock or bool(runtime_dependencies)
            project_name = _project_name(payload=payload, display_path=project_path)
        except DependencyWorkspaceError as error:
            findings.append(str(error))
            continue
        projects.append(
            DependencyProjectInspection(
                owner=project_input.owner,
                path=project_path,
                name=project_name,
                runtime_dependencies=_sanitized_dependency_names(runtime_dependencies),
            )
        )
        findings.extend(_member_pyproject_findings(payload=payload, display_path=project_path))

    findings.extend(_owned_requirements_findings(tenant_repo_path=tenant_repo_path, shared_addons_repo_path=shared_addons_repo_path))

    root_pyproject_path = tenant_repo_path / "pyproject.toml"
    tenant_lock_path = tenant_repo_path / "uv.lock"
    root_pyproject_present = root_pyproject_path.is_file()
    tenant_lock_present = tenant_lock_path.is_file()
    tenant_lock_sha256 = _sha256_file(tenant_lock_path) if tenant_lock_present else ""
    tenant_lock_current: bool | None = None
    workspace_members: tuple[str, ...] = ()

    if root_pyproject_present != tenant_lock_present:
        findings.append("Tenant dependency workspace requires pyproject.toml and uv.lock as a complete pair.")
    if requires_tenant_lock and not (root_pyproject_present and tenant_lock_present):
        findings.append("Owned runtime dependency declarations require a tenant root pyproject.toml and uv.lock.")

    if root_pyproject_present and tenant_lock_present:
        findings.extend(
            _publish_input_findings(
                tenant_repo_path=tenant_repo_path,
                root_pyproject_path=root_pyproject_path,
                tenant_lock_path=tenant_lock_path,
                project_inputs=project_inputs,
            )
        )
        with tempfile.TemporaryDirectory(prefix="odoo-dependency-workspace-") as temporary_directory_name:
            staged_root = Path(temporary_directory_name)
            _stage_dependency_metadata(
                root_pyproject_path=root_pyproject_path,
                tenant_lock_path=tenant_lock_path,
                project_inputs=project_inputs,
                staged_root=staged_root,
            )
            try:
                root_payload = _load_pyproject(staged_root / "pyproject.toml")
                root_runtime_dependencies = _validate_root_pyproject(payload=root_payload)
                requires_tenant_lock = requires_tenant_lock or bool(root_runtime_dependencies)
                if devkit_repo_path is not None:
                    require_staged_build_requirements_supplied(
                        support_root=devkit_repo_path / "docker" / "runtime-python",
                        tenant_root=staged_root,
                    )
                workspace_member_set = _workspace_members(root=staged_root, payload=root_payload)
                workspace_members = tuple(sorted(path.as_posix() for path in workspace_member_set))
                expected_members = {
                    project_input.staged_pyproject_path.parent
                    for project_input in project_inputs
                    if project_input.staged_pyproject_path.as_posix() in staged_project_paths
                }
                if workspace_member_set != expected_members:
                    extra_members = sorted(path.as_posix() for path in workspace_member_set - expected_members)
                    missing_members = sorted(path.as_posix() for path in expected_members - workspace_member_set)
                    findings.append(
                        "Tenant workspace members must exactly match owned addon projects; "
                        f"extra={extra_members}, missing={missing_members}"
                    )
            except DependencyWorkspaceError as error:
                findings.append(str(error))
            if not findings:
                tenant_lock_current = _uv_lock_is_current(staged_root)
                if not tenant_lock_current:
                    findings.append("Tenant uv.lock is not current for the combined owned-addon workspace.")
            else:
                tenant_lock_current = False

    current = not findings
    publishable = current and root_pyproject_present and tenant_lock_present and tenant_lock_current is True
    return DependencyWorkspaceInspection(
        tenant=manifest.tenant,
        current=current,
        publishable=publishable,
        requires_tenant_lock=requires_tenant_lock,
        tenant_root_pyproject_present=root_pyproject_present,
        tenant_lock_present=tenant_lock_present,
        tenant_lock_current=tenant_lock_current,
        tenant_lock_sha256=tenant_lock_sha256,
        workspace_members=workspace_members,
        projects=tuple(sorted(projects, key=lambda project: (project.path, project.owner))),
        findings=tuple(findings),
    )


def require_publishable_dependency_workspace(*, manifest: WorkspaceManifest) -> DependencyWorkspaceInspection:
    inspection = inspect_dependency_workspace(manifest=manifest)
    if not inspection.current:
        raise DependencyWorkspaceError("Dependency workspace check failed: " + "; ".join(inspection.findings))
    if not inspection.publishable:
        raise DependencyWorkspaceError(
            "Artifact schema v2 requires a tracked tenant pyproject.toml and uv.lock, even when a lockless pure-addon workspace is valid locally."
        )
    return inspection


def stage_publishable_dependency_workspace(
    *,
    manifest: WorkspaceManifest,
    destination_root: Path,
    tenant_commit: str | None = None,
    shared_addons_commit: str | None = None,
) -> DependencyWorkspaceInspection:
    inspection = require_publishable_dependency_workspace(manifest=manifest)
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    assert tenant_repo_path is not None
    tenant_repo_path = tenant_repo_path.resolve()
    shared_addons_repo_path = _resolve_shared_addons_repo_path(manifest)
    shared_addons_repo_path = shared_addons_repo_path.resolve() if shared_addons_repo_path is not None else None
    if destination_root.exists() and any(destination_root.iterdir()):
        raise DependencyWorkspaceError("Dependency workspace staging destination must be empty")
    destination_root.mkdir(parents=True, exist_ok=True)
    _stage_dependency_metadata(
        root_pyproject_path=tenant_repo_path / "pyproject.toml",
        tenant_lock_path=tenant_repo_path / "uv.lock",
        project_inputs=_discover_project_inputs(
            tenant_repo_path=tenant_repo_path,
            shared_addons_repo_path=shared_addons_repo_path,
        ),
        staged_root=destination_root,
        source_commits={
            tenant_repo_path: tenant_commit or _git_head_commit(tenant_repo_path),
            **(
                {
                    shared_addons_repo_path: shared_addons_commit or _git_head_commit(shared_addons_repo_path),
                }
                if shared_addons_repo_path is not None
                else {}
            ),
        },
    )
    return inspection


def require_staged_dependency_workspace_current(*, staged_root: Path, label: str = "dependency") -> None:
    if not _uv_lock_is_current(staged_root):
        raise DependencyWorkspaceError(f"Staged {label} uv.lock changed or is not current for the exact artifact inputs.")


def require_staged_build_requirements_supplied(*, support_root: Path, tenant_root: Path) -> None:
    supplied_requirements: set[tuple[str, str]] = set()
    for catalog_root in (support_root, tenant_root):
        payload = _load_pyproject(catalog_root / "pyproject.toml")
        for dependency in _runtime_dependencies(payload=payload, display_path="pyproject.toml"):
            match = _EXACT_BUILD_REQUIREMENT_PATTERN.fullmatch(dependency.strip())
            if match is not None:
                supplied_requirements.add(
                    (
                        re.sub(r"[-_.]+", "-", match.group("name")).lower(),
                        match.group("version"),
                    )
                )

    missing_requirements: set[str] = set()
    addons_root = tenant_root / "addons"
    for pyproject_path in _discover_pyproject_paths(addons_root) if addons_root.is_dir() else ():
        payload = _load_pyproject(pyproject_path)
        build_system = payload.get("build-system")
        requirements = build_system.get("requires", []) if isinstance(build_system, dict) else []
        for requirement in requirements:
            if not isinstance(requirement, str):
                raise DependencyWorkspaceError("Staged addon build requirements must be strings")
            match = _EXACT_BUILD_REQUIREMENT_PATTERN.fullmatch(requirement.strip())
            if match is None:
                raise DependencyWorkspaceError("Staged addon build requirements must use exact registry versions")
            requirement_key = (
                re.sub(r"[-_.]+", "-", match.group("name")).lower(),
                match.group("version"),
            )
            if requirement_key not in supplied_requirements:
                missing_requirements.add(f"{requirement_key[0]}=={requirement_key[1]}")
    if missing_requirements:
        raise DependencyWorkspaceError(
            "Addon build requirements must be supplied by the support/runtime or tenant lock catalog: "
            + ", ".join(sorted(missing_requirements))
        )


def _resolve_shared_addons_repo_path(manifest: WorkspaceManifest) -> Path | None:
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    return resolve_optional_repo_path_with_managed_checkout(
        manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "shared-addons",
    )


def _resolve_devkit_repo_path(manifest: WorkspaceManifest) -> Path | None:
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    return resolve_optional_repo_path_with_managed_checkout(
        manifest.devkit_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "devkit",
    )


def _discover_project_inputs(*, tenant_repo_path: Path, shared_addons_repo_path: Path | None) -> tuple[_ProjectInput, ...]:
    projects: list[_ProjectInput] = []
    tenant_addons_root = tenant_repo_path / "addons"
    if tenant_addons_root.is_dir():
        for pyproject_path in _discover_pyproject_paths(tenant_addons_root):
            if shared_addons_repo_path is not None and pyproject_path.is_relative_to(tenant_addons_root / "shared"):
                continue
            projects.append(
                _ProjectInput(
                    owner="tenant",
                    source_repo_path=tenant_repo_path,
                    source_pyproject_path=pyproject_path,
                    staged_pyproject_path=Path("addons") / pyproject_path.relative_to(tenant_addons_root),
                )
            )
    if shared_addons_repo_path is not None:
        for pyproject_path in _discover_pyproject_paths(shared_addons_repo_path):
            projects.append(
                _ProjectInput(
                    owner="shared_addons",
                    source_repo_path=shared_addons_repo_path,
                    source_pyproject_path=pyproject_path,
                    staged_pyproject_path=Path("addons/shared") / pyproject_path.relative_to(shared_addons_repo_path),
                )
            )
    return tuple(sorted(projects, key=lambda project: project.staged_pyproject_path.as_posix()))


def _discover_pyproject_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(root.rglob("pyproject.toml"))
        if not any(part.startswith(".") or part in _IGNORED_PATH_PARTS for part in path.relative_to(root).parts)
    )


def _reserved_dependency_namespace_findings(
    *,
    tenant_repo_path: Path,
    shared_addons_repo_path: Path | None,
) -> tuple[str, ...]:
    if shared_addons_repo_path is None:
        return ()
    reserved_root = tenant_repo_path / "addons" / "shared"
    if not reserved_root.is_dir():
        return ()
    return tuple(
        "Tenant dependency metadata cannot use the reserved shared-addons namespace: "
        + (Path("addons/shared") / path.relative_to(reserved_root)).as_posix()
        for path in _discover_pyproject_paths(reserved_root)
    )


def _owned_requirements_findings(*, tenant_repo_path: Path, shared_addons_repo_path: Path | None) -> tuple[str, ...]:
    findings: list[str] = []
    roots = (("tenant", tenant_repo_path / "addons"), ("shared_addons", shared_addons_repo_path))
    for owner, root in roots:
        if root is None or not root.is_dir():
            continue
        for requirements_path in sorted(root.rglob("requirements*.txt")):
            relative_path = requirements_path.relative_to(root).as_posix()
            if any(part.startswith(".") or part in _IGNORED_PATH_PARTS for part in requirements_path.relative_to(root).parts):
                continue
            findings.append(f"Owned {owner} requirements must move into pyproject.toml dependency metadata: {relative_path}")
    return tuple(findings)


def _load_pyproject(path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise DependencyWorkspaceError(f"Invalid dependency metadata {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise DependencyWorkspaceError(f"Dependency metadata must be a TOML table: {path.name}")
    return payload


def _member_pyproject_findings(*, payload: dict[str, Any], display_path: str) -> tuple[str, ...]:
    findings: list[str] = []
    config = _uv_config(payload)
    if config.get("managed") is False:
        findings.append(f"{display_path} cannot set tool.uv.managed = false")
    if config.get("package") is not False:
        findings.append(f"{display_path} must set tool.uv.package = false")
    for validator in (
        lambda: _validate_build_system(payload=payload, display_path=display_path),
        lambda: _validate_dependency_references(payload=payload, display_path=display_path, allow_workspace_sources=True),
    ):
        try:
            validator()
        except DependencyWorkspaceError as error:
            findings.append(str(error))
    return tuple(findings)


def _validate_root_pyproject(*, payload: dict[str, Any]) -> tuple[str, ...]:
    display_path = "pyproject.toml"
    config = _uv_config(payload)
    if config.get("managed") is False:
        raise DependencyWorkspaceError("pyproject.toml cannot set tool.uv.managed = false")
    if config.get("package") is not False:
        raise DependencyWorkspaceError("pyproject.toml must set tool.uv.package = false")
    if "build-system" in payload:
        raise DependencyWorkspaceError("pyproject.toml dependency catalog cannot define build-system")
    project = payload.get("project")
    if isinstance(project, dict) and project.get("dynamic"):
        raise DependencyWorkspaceError("pyproject.toml dependency catalog cannot use dynamic metadata")
    _validate_dependency_references(payload=payload, display_path=display_path, allow_workspace_sources=True)
    return _runtime_dependencies(payload=payload, display_path=display_path)


def _validate_build_system(*, payload: dict[str, Any], display_path: str) -> None:
    build_system = payload.get("build-system")
    if not isinstance(build_system, dict):
        raise DependencyWorkspaceError(f"{display_path} requires an explicit build-system table")
    if set(build_system) != {"requires", "build-backend"}:
        raise DependencyWorkspaceError(f"{display_path} build-system may contain only requires and build-backend")
    backend = build_system.get("build-backend")
    requirements = build_system.get("requires")
    if not isinstance(backend, str) or not backend.strip():
        raise DependencyWorkspaceError(f"{display_path} build-system requires a build-backend")
    if not isinstance(requirements, list) or not requirements or not all(isinstance(value, str) for value in requirements):
        raise DependencyWorkspaceError(f"{display_path} build-system requires a non-empty string list")
    for requirement in requirements:
        if _EXACT_BUILD_REQUIREMENT_PATTERN.fullmatch(requirement.strip()) is None:
            raise DependencyWorkspaceError(f"{display_path} build requirements must use exact registry versions")


def _project_name(*, payload: dict[str, Any], display_path: str) -> str:
    project = payload.get("project")
    name = str(project.get("name", "")).strip() if isinstance(project, dict) else ""
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if not normalized or _PACKAGE_NAME_PATTERN.fullmatch(normalized) is None:
        raise DependencyWorkspaceError(f"{display_path} requires a valid project.name")
    if isinstance(project, dict) and project.get("dynamic"):
        raise DependencyWorkspaceError(f"{display_path} cannot use dynamic project metadata")
    return normalized


def _runtime_dependencies(*, payload: dict[str, Any], display_path: str) -> tuple[str, ...]:
    project = payload.get("project")
    if project is None:
        return ()
    if not isinstance(project, dict):
        raise DependencyWorkspaceError(f"{display_path} project must be a table")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(value, str) for value in dependencies):
        raise DependencyWorkspaceError(f"{display_path} project.dependencies must be a string array")
    return tuple(value.strip() for value in dependencies if value.strip())


def _validate_dependency_references(*, payload: dict[str, Any], display_path: str, allow_workspace_sources: bool) -> None:
    for dependency in _dependency_strings(payload):
        _validate_git_reference(value=dependency, display_path=display_path)
    _validate_uv_sources(payload=payload, display_path=display_path, allow_workspace=allow_workspace_sources)
    _validate_uv_resolution_controls(payload=payload, display_path=display_path)


def _dependency_strings(payload: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    build_system = payload.get("build-system")
    if isinstance(build_system, dict):
        requirements = build_system.get("requires", [])
        if isinstance(requirements, list):
            values.extend(str(value) for value in requirements)
    project = payload.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            values.extend(str(value) for value in dependencies)
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    values.extend(str(value) for value in group)
    dependency_groups = payload.get("dependency-groups", {})
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            if isinstance(group, list):
                values.extend(str(value) for value in group if isinstance(value, str))
    return tuple(values)


def _validate_git_reference(*, value: str, display_path: str) -> None:
    stripped = value.strip()
    if not stripped:
        raise DependencyWorkspaceError(f"{display_path} contains a blank dependency declaration")
    if _UNSAFE_DIRECT_REFERENCE_PATTERN.search(stripped):
        raise DependencyWorkspaceError(f"{display_path} cannot use editable, local-path, or non-VCS direct references")
    direct_reference = _DIRECT_REFERENCE_PATTERN.match(stripped)
    if direct_reference is not None and not direct_reference.group("url").lower().startswith(("git+https://", "git+ssh://")):
        raise DependencyWorkspaceError(f"{display_path} cannot use editable, local-path, or non-VCS direct references")
    matches = tuple(_VCS_REFERENCE_PATTERN.finditer(stripped))
    for match in matches:
        if _GIT_COMMIT_PATTERN.fullmatch(match.group(1)) is None:
            raise DependencyWorkspaceError(f"{display_path} VCS dependencies must use exact lowercase git commits")
        _validate_vcs_url(match.group(0).rsplit("@", 1)[0], display_path=display_path)
    if "git+" in stripped.lower() and not matches:
        raise DependencyWorkspaceError(f"{display_path} contains an invalid VCS dependency")


def _validate_vcs_url(value: str, *, display_path: str) -> None:
    parsed = urlsplit(value.removeprefix("git+"))
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path.strip("/"):
        raise DependencyWorkspaceError(f"{display_path} contains an invalid VCS repository")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise DependencyWorkspaceError(f"{display_path} VCS repository cannot contain credentials, queries, or fragments")
    if parsed.scheme == "https" and parsed.username is not None:
        raise DependencyWorkspaceError(f"{display_path} HTTPS VCS repository cannot contain userinfo")


def _validate_uv_sources(*, payload: dict[str, Any], display_path: str, allow_workspace: bool) -> None:
    sources = _uv_config(payload).get("sources", {})
    if not isinstance(sources, dict):
        raise DependencyWorkspaceError(f"{display_path} tool.uv.sources must be a table")
    for package_name, raw_source in sources.items():
        package_label = _safe_package_label(package_name)
        source_values = raw_source if isinstance(raw_source, list) else [raw_source]
        for source in source_values:
            if not isinstance(source, dict):
                raise DependencyWorkspaceError(f"{display_path} source for {package_label} must be a table")
            if source.get("workspace") is True:
                if allow_workspace and set(source) == {"workspace"}:
                    continue
                raise DependencyWorkspaceError(f"{display_path} has an invalid workspace source for {package_label}")
            if "path" in source:
                raise DependencyWorkspaceError(f"{display_path} cannot use local path source for {package_label}")
            if "git" in source:
                if "tag" in source or "branch" in source:
                    raise DependencyWorkspaceError(f"{display_path} VCS source for {package_label} cannot use tag or branch")
                if set(source) - {"git", "rev", "subdirectory", "marker"}:
                    raise DependencyWorkspaceError(f"{display_path} has unsupported git source fields for {package_label}")
                repository = str(source.get("git", ""))
                revision = str(source.get("rev", ""))
                _validate_git_reference(value=f"{package_label} @ git+{repository}@{revision}", display_path=display_path)
                continue
            raise DependencyWorkspaceError(f"{display_path} contains unsupported source for {package_label}")


def _validate_uv_resolution_controls(*, payload: dict[str, Any], display_path: str) -> None:
    config = _uv_config(payload)
    forbidden_keys = sorted(_FORBIDDEN_UV_SOURCE_KEYS.intersection(config))
    if forbidden_keys:
        raise DependencyWorkspaceError(f"{display_path} cannot configure custom uv package sources: {', '.join(forbidden_keys)}")
    for key in _UV_DEPENDENCY_LIST_KEYS:
        if key not in config:
            continue
        values = config[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise DependencyWorkspaceError(f"{display_path} tool.uv.{key} must be a string array")
        for value in values:
            _validate_git_reference(value=value, display_path=display_path)


def _safe_package_label(value: object) -> str:
    normalized = re.sub(r"[-_.]+", "-", str(value).strip()).lower()
    return normalized if _PACKAGE_NAME_PATTERN.fullmatch(normalized) is not None else "<invalid-package>"


def _sanitized_dependency_names(dependencies: tuple[str, ...]) -> tuple[str, ...]:
    names: set[str] = set()
    for dependency in dependencies:
        match = re.match(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", dependency)
        if match is not None:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return tuple(sorted(names))


def _uv_config(payload: dict[str, Any]) -> dict[str, Any]:
    tool = payload.get("tool")
    if not isinstance(tool, dict):
        return {}
    uv = tool.get("uv")
    return uv if isinstance(uv, dict) else {}


def _workspace_members(*, root: Path, payload: dict[str, Any]) -> set[Path]:
    workspace = _uv_config(payload).get("workspace")
    if not isinstance(workspace, dict):
        raise DependencyWorkspaceError("pyproject.toml must define tool.uv.workspace")
    raw_members = workspace.get("members", [])
    raw_exclude = workspace.get("exclude", [])
    if not isinstance(raw_members, list) or not raw_members or not all(isinstance(value, str) for value in raw_members):
        raise DependencyWorkspaceError("pyproject.toml workspace members must be a non-empty string array")
    if not isinstance(raw_exclude, list) or not all(isinstance(value, str) for value in raw_exclude):
        raise DependencyWorkspaceError("pyproject.toml workspace exclude values must be a string array")
    for pattern in [*raw_members, *raw_exclude]:
        if pattern.startswith(("/", "~")) or "\\" in pattern or ".." in Path(pattern).parts:
            raise DependencyWorkspaceError("pyproject.toml contains an unsafe workspace pattern")
    excluded: set[Path] = set()
    for pattern in raw_exclude:
        excluded.update(path.relative_to(root) for path in root.glob(pattern) if path.is_dir())
    members: set[Path] = set()
    for pattern in raw_members:
        for path in root.glob(pattern):
            relative_path = path.relative_to(root)
            if path.is_dir() and (path / "pyproject.toml").is_file() and relative_path not in excluded:
                members.add(relative_path)
    return members


def _publish_input_findings(
    *,
    tenant_repo_path: Path,
    root_pyproject_path: Path,
    tenant_lock_path: Path,
    project_inputs: tuple[_ProjectInput, ...],
) -> tuple[str, ...]:
    inputs = [
        (tenant_repo_path, root_pyproject_path, "pyproject.toml"),
        (tenant_repo_path, tenant_lock_path, "uv.lock"),
        *(
            (project_input.source_repo_path, project_input.source_pyproject_path, project_input.staged_pyproject_path.as_posix())
            for project_input in project_inputs
        ),
    ]
    findings: list[str] = []
    for repo_path, source_path, display_path in inputs:
        finding = _publish_input_finding(repo_path=repo_path, source_path=source_path, display_path=display_path)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _publish_input_finding(*, repo_path: Path, source_path: Path, display_path: str) -> str | None:
    normalized_repo_path = repo_path.resolve()
    normalized_source_path = source_path.parent.resolve() / source_path.name
    try:
        relative_path = normalized_source_path.relative_to(normalized_repo_path)
    except ValueError:
        return f"Publish dependency input escapes its source repository: {display_path}"
    current_path = normalized_repo_path
    for part in relative_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            return f"Publish dependency inputs cannot use symlinks: {display_path}"
    if not source_path.is_file():
        return f"Publish dependency input must be a regular file: {display_path}"
    try:
        top_level_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=normalized_repo_path,
            capture_output=True,
            text=True,
            env=_git_command_env(),
        )
        tracked_result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative_path.as_posix()],
            cwd=normalized_repo_path,
            capture_output=True,
            text=True,
            env=_git_command_env(),
        )
    except FileNotFoundError:
        return f"Publish dependency input requires Git tracking: {display_path}"
    if top_level_result.returncode != 0:
        return f"Publish dependency input requires a Git worktree: {display_path}"
    try:
        top_level = Path(top_level_result.stdout.strip()).resolve()
    except OSError:
        return f"Publish dependency input requires a Git worktree: {display_path}"
    if top_level != normalized_repo_path or tracked_result.returncode != 0:
        return f"Publish dependency input must be tracked by its source repository: {display_path}"
    return None


def _stage_dependency_metadata(
    *,
    root_pyproject_path: Path,
    tenant_lock_path: Path,
    project_inputs: tuple[_ProjectInput, ...],
    staged_root: Path,
    source_commits: dict[Path, str] | None = None,
) -> None:
    _copy_regular_dependency_file(
        repo_path=root_pyproject_path.parent,
        source_commit=(source_commits or {}).get(root_pyproject_path.parent.resolve()),
        source_path=root_pyproject_path,
        destination_path=staged_root / "pyproject.toml",
        display_path="pyproject.toml",
    )
    _copy_regular_dependency_file(
        repo_path=root_pyproject_path.parent,
        source_commit=(source_commits or {}).get(root_pyproject_path.parent.resolve()),
        source_path=tenant_lock_path,
        destination_path=staged_root / "uv.lock",
        display_path="uv.lock",
    )
    for project_input in project_inputs:
        destination = staged_root / project_input.staged_pyproject_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_dependency_file(
            repo_path=project_input.source_repo_path,
            source_commit=(source_commits or {}).get(project_input.source_repo_path.resolve()),
            source_path=project_input.source_pyproject_path,
            destination_path=destination,
            display_path=project_input.staged_pyproject_path.as_posix(),
        )


def _copy_regular_dependency_file(
    *,
    repo_path: Path,
    source_commit: str | None,
    source_path: Path,
    destination_path: Path,
    display_path: str,
) -> None:
    normalized_repo_path = repo_path.resolve()
    normalized_source_path = source_path.parent.resolve() / source_path.name
    if source_commit is not None:
        try:
            relative_path = normalized_source_path.relative_to(normalized_repo_path).as_posix()
        except ValueError as error:
            raise DependencyWorkspaceError(f"Dependency input escapes its source repository: {display_path}") from error
        tree_result = subprocess.run(
            ["git", "ls-tree", "-z", source_commit, "--", relative_path],
            cwd=normalized_repo_path,
            capture_output=True,
            env=_git_command_env(),
        )
        entries = tuple(entry for entry in tree_result.stdout.split(b"\0") if entry)
        if tree_result.returncode != 0 or len(entries) != 1:
            raise DependencyWorkspaceError(f"Dependency input is missing from source commit: {display_path}")
        try:
            raw_metadata, raw_entry_path = entries[0].split(b"\t", 1)
            mode, entry_type, object_id = os.fsdecode(raw_metadata).split(" ", 2)
        except ValueError as error:
            raise DependencyWorkspaceError(f"Unable to parse committed dependency input: {display_path}") from error
        if os.fsdecode(raw_entry_path) != relative_path or entry_type != "blob" or mode not in {"100644", "100755"}:
            raise DependencyWorkspaceError(f"Dependency input must be a committed regular file: {display_path}")
        blob_result = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=normalized_repo_path,
            capture_output=True,
            env=_git_command_env(),
        )
        if blob_result.returncode != 0:
            raise DependencyWorkspaceError(f"Unable to materialize committed dependency input: {display_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(blob_result.stdout)
        destination_path.chmod(0o755 if mode == "100755" else 0o644)
        return
    if source_path.is_symlink() or not source_path.is_file():
        raise DependencyWorkspaceError(f"Dependency staging requires a regular file: {display_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(source_path.read_bytes())


def _git_head_commit(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=_git_command_env(),
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or _GIT_COMMIT_PATTERN.fullmatch(commit) is None:
        raise DependencyWorkspaceError("Dependency workspace staging requires an exact Git commit")
    return commit


def _uv_lock_is_current(staged_root: Path) -> bool:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "UV_")) and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["UV_NO_PROGRESS"] = "1"
    try:
        result = subprocess.run(
            ["uv", "lock", "--check", "--offline", "--no-config", "--project", str(staged_root)],
            cwd=staged_root,
            capture_output=True,
            text=True,
            env=environment,
        )
    except FileNotFoundError as error:
        raise DependencyWorkspaceError("uv is required for dependency workspace checks") from error
    return result.returncode == 0


def _git_command_env() -> dict[str, str]:
    environment = dict(os.environ)
    repository_context_keys = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
    for environment_key in tuple(environment):
        if environment_key in repository_context_keys or environment_key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(environment_key, None)
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
