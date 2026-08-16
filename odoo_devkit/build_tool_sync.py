from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dependency_workspace import DependencyWorkspaceError, require_staged_build_requirements_supplied

_EXACT_REQUIREMENT_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
_FULL_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SECTION_PATTERN_TEMPLATE = r"(?ms)^\[{section}\]\s*$.*?(?=^\[|\Z)"
_REF_PATTERN = re.compile(r"(?m)^(?P<prefix>\s*ref\s*=\s*)(?P<quote>[\"'])(?P<value>[^\"']+)(?P=quote)(?P<suffix>\s*(?:#.*)?)$")
_IGNORED_ADDON_PATH_PARTS = frozenset({".git", ".venv", "build", "dist", "__pycache__"})


class BuildToolSyncError(ValueError):
    pass


@dataclass(frozen=True)
class BuildToolChange:
    path: str
    kind: str
    before: str
    after: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class BuildToolSyncPlan:
    tenant_root: Path
    devkit_root: Path
    devkit_ref: str
    catalog: dict[str, str]
    rendered_files: dict[Path, str]
    changes: tuple[BuildToolChange, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def build_tool_changed(self) -> bool:
        return any(change.kind.startswith("build-system.requires:") for change in self.changes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "changed": self.changed,
            "build_tool_changed": self.build_tool_changed,
            "devkit_ref": self.devkit_ref,
            "catalog": dict(sorted(self.catalog.items())),
            "changes": [change.to_dict() for change in self.changes],
        }


def plan_build_tool_sync(*, tenant_root: Path, devkit_root: Path, devkit_ref: str) -> BuildToolSyncPlan:
    tenant_root = _resolve_regular_directory(tenant_root, label="Tenant root")
    devkit_root = _resolve_regular_directory(devkit_root, label="Devkit root")
    if _FULL_GIT_SHA_PATTERN.fullmatch(devkit_ref) is None:
        raise BuildToolSyncError("Devkit ref must be an exact lowercase 40-character Git commit.")
    _require_devkit_checkout(devkit_root=devkit_root, devkit_ref=devkit_ref)

    catalog = _load_exact_catalog(devkit_root / "docker" / "runtime-python" / "pyproject.toml")
    rendered_files: dict[Path, str] = {}
    changes: list[BuildToolChange] = []

    workspace_path = tenant_root / "workspace.toml"
    workspace_text = _read_regular_text(workspace_path)
    rendered_workspace, workspace_changes = _render_workspace_refs(
        text=workspace_text,
        path=workspace_path,
        tenant_root=tenant_root,
        devkit_ref=devkit_ref,
    )
    if workspace_changes:
        rendered_files[workspace_path] = rendered_workspace
        changes.extend(workspace_changes)

    addons_root = _resolve_regular_directory(tenant_root / "addons", label="Tenant addons root")
    for pyproject_path in sorted(addons_root.rglob("pyproject.toml")):
        if _IGNORED_ADDON_PATH_PARTS.intersection(pyproject_path.relative_to(addons_root).parts):
            continue
        original_text = _read_regular_text(pyproject_path)
        rendered_text, file_changes = _render_addon_requirements(
            text=original_text,
            path=pyproject_path,
            tenant_root=tenant_root,
            catalog=catalog,
        )
        if file_changes:
            rendered_files[pyproject_path] = rendered_text
            changes.extend(file_changes)

    return BuildToolSyncPlan(
        tenant_root=tenant_root,
        devkit_root=devkit_root,
        devkit_ref=devkit_ref,
        catalog=catalog,
        rendered_files=rendered_files,
        changes=tuple(changes),
    )


def apply_build_tool_sync(plan: BuildToolSyncPlan) -> None:
    originals = {path: path.read_bytes() for path in plan.rendered_files}
    try:
        for path, rendered_text in plan.rendered_files.items():
            _atomic_write_text(path=path, content=rendered_text)
        _validate_applied_plan(plan)
    except Exception as error:
        rollback_failures: list[str] = []
        for path, original_bytes in originals.items():
            try:
                _atomic_write_bytes(path=path, content=original_bytes)
            except OSError as rollback_error:
                rollback_failures.append(f"{path}: {rollback_error}")
        if rollback_failures:
            raise BuildToolSyncError(
                f"Build-tool synchronization failed and rollback was incomplete: {'; '.join(rollback_failures)}"
            ) from error
        raise


def _load_exact_catalog(path: Path) -> dict[str, str]:
    payload = _load_toml(path)
    project = payload.get("project")
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(dependencies, list) or not all(isinstance(value, str) for value in dependencies):
        raise BuildToolSyncError(f"{path} project.dependencies must be a string array.")
    catalog: dict[str, str] = {}
    for dependency in dependencies:
        match = _EXACT_REQUIREMENT_PATTERN.fullmatch(dependency.strip())
        if match is None:
            continue
        raw_name = match.group("name")
        version = match.group("version")
        if raw_name is None or version is None:
            raise BuildToolSyncError(f"Unable to parse exact build-tool requirement: {dependency}")
        name = _normalize_requirement_name(raw_name)
        previous = catalog.get(name)
        if previous is None:
            catalog[name] = version
        elif previous != version:
            raise BuildToolSyncError(f"Build-tool catalog supplies conflicting versions for {name}: {previous}, {version}.")
    if not catalog:
        raise BuildToolSyncError(f"{path} does not contain any exact build-tool requirements.")
    return catalog


def _render_workspace_refs(
    *,
    text: str,
    path: Path,
    tenant_root: Path,
    devkit_ref: str,
) -> tuple[str, tuple[BuildToolChange, ...]]:
    _parse_toml_text(text=text, path=path)
    rendered = text
    changes: list[BuildToolChange] = []
    for section in ("repos.devkit", "repos.runtime"):
        rendered, previous = _replace_section_ref(text=rendered, section=section, devkit_ref=devkit_ref, path=path)
        if previous != devkit_ref:
            changes.append(
                BuildToolChange(
                    path=path.relative_to(tenant_root).as_posix(),
                    kind=f"{section}.ref",
                    before=previous,
                    after=devkit_ref,
                )
            )
    _parse_toml_text(text=rendered, path=path)
    return rendered, tuple(changes)


def _replace_section_ref(*, text: str, section: str, devkit_ref: str, path: Path) -> tuple[str, str]:
    section_pattern = re.compile(_SECTION_PATTERN_TEMPLATE.format(section=re.escape(section)))
    section_match = section_pattern.search(text)
    if section_match is None:
        raise BuildToolSyncError(f"{path} is missing [{section}].")
    section_text = section_match.group()
    ref_match = _REF_PATTERN.search(section_text)
    if ref_match is None:
        raise BuildToolSyncError(f"{path} [{section}] is missing ref.")
    previous = ref_match.group("value")
    replacement = (
        f"{ref_match.group('prefix')}{ref_match.group('quote')}{devkit_ref}{ref_match.group('quote')}{ref_match.group('suffix')}"
    )
    rendered_section = section_text[: ref_match.start()] + replacement + section_text[ref_match.end() :]
    return text[: section_match.start()] + rendered_section + text[section_match.end() :], previous


def _render_addon_requirements(
    *,
    text: str,
    path: Path,
    tenant_root: Path,
    catalog: dict[str, str],
) -> tuple[str, tuple[BuildToolChange, ...]]:
    payload = _parse_toml_text(text=text, path=path)
    build_system = payload.get("build-system")
    requirements = build_system.get("requires", []) if isinstance(build_system, dict) else []
    if not isinstance(requirements, list) or not all(isinstance(value, str) for value in requirements):
        raise BuildToolSyncError(f"{path} build-system.requires must be a string array.")

    rendered = text
    changes: list[BuildToolChange] = []
    for requirement in requirements:
        match = _EXACT_REQUIREMENT_PATTERN.fullmatch(requirement.strip())
        requirement_name = _requirement_name(requirement)
        if requirement_name not in catalog:
            continue
        if match is None:
            raise BuildToolSyncError(f"{path} centrally managed build requirement must use an exact version: {requirement}")
        next_requirement = f"{match.group('name')}=={catalog[requirement_name]}"
        if requirement == next_requirement:
            continue
        rendered = _replace_build_system_requirement(
            text=rendered,
            path=path,
            previous=requirement,
            replacement=next_requirement,
        )
        changes.append(
            BuildToolChange(
                path=path.relative_to(tenant_root).as_posix(),
                kind=f"build-system.requires:{requirement_name}",
                before=requirement,
                after=next_requirement,
            )
        )
    _parse_toml_text(text=rendered, path=path)
    return rendered, tuple(changes)


def _replace_build_system_requirement(*, text: str, path: Path, previous: str, replacement: str) -> str:
    section_pattern = re.compile(_SECTION_PATTERN_TEMPLATE.format(section=re.escape("build-system")))
    section_match = section_pattern.search(text)
    if section_match is None:
        raise BuildToolSyncError(f"{path} is missing [build-system].")
    section_text = section_match.group()
    pattern = re.compile(rf"(?P<quote>[\"']){re.escape(previous)}(?P=quote)")
    matches = tuple(pattern.finditer(section_text))
    if len(matches) != 1:
        raise BuildToolSyncError(f"Expected exactly one {previous!r} string in {path} [build-system], found {len(matches)}.")
    match = matches[0]
    quote = match.group("quote")
    rendered_section = section_text[: match.start()] + f"{quote}{replacement}{quote}" + section_text[match.end() :]
    return text[: section_match.start()] + rendered_section + text[section_match.end() :]


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", requirement)
    return _normalize_requirement_name(match.group(1)) if match is not None else ""


def _normalize_requirement_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _validate_applied_plan(plan: BuildToolSyncPlan) -> None:
    for path in plan.rendered_files:
        _load_toml(path)
    try:
        require_staged_build_requirements_supplied(
            support_root=plan.devkit_root / "docker" / "runtime-python",
            tenant_root=plan.tenant_root,
        )
    except DependencyWorkspaceError as error:
        raise BuildToolSyncError(str(error)) from error
    try:
        result = subprocess.run(
            ["uv", "lock", "--check", "--offline", "--no-config"],
            cwd=plan.tenant_root,
            capture_output=True,
            text=True,
            env=_sanitized_command_environment(),
            timeout=120,
        )
    except FileNotFoundError as error:
        raise BuildToolSyncError("uv is required for tenant lock validation.") from error
    except subprocess.TimeoutExpired as error:
        raise BuildToolSyncError("Tenant lock validation timed out after 120 seconds.") from error
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "uv lock check failed"
        raise BuildToolSyncError(f"Tenant lock validation failed: {message}")


def _read_regular_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BuildToolSyncError(f"Expected a tracked-style regular file: {path}")
    return path.read_text()


def _load_toml(path: Path) -> dict[str, Any]:
    return _parse_toml_text(text=_read_regular_text(path), path=path)


def _parse_toml_text(*, text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise BuildToolSyncError(f"Invalid TOML in {path}: {error}") from error


def _resolve_regular_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise BuildToolSyncError(f"{label} must be a regular directory: {path}")
    return path.resolve()


def _require_devkit_checkout(*, devkit_root: Path, devkit_ref: str) -> None:
    repository_root = _git_output(devkit_root, "rev-parse", "--show-toplevel")
    if Path(repository_root).resolve() != devkit_root:
        raise BuildToolSyncError(f"Devkit root must be the Git worktree root: {devkit_root}")
    head_ref = _git_output(devkit_root, "rev-parse", "HEAD")
    if head_ref != devkit_ref:
        raise BuildToolSyncError(f"Devkit checkout HEAD {head_ref} does not match requested ref {devkit_ref}.")
    catalog_paths = ("docker/runtime-python/pyproject.toml", "docker/runtime-python/uv.lock")
    for catalog_path in catalog_paths:
        result = _run_git(devkit_root, "ls-files", "--error-unmatch", "--", catalog_path)
        if result.returncode != 0:
            raise BuildToolSyncError(f"Devkit catalog input must be tracked at {devkit_ref}: {catalog_path}")
    for diff_arguments in (("diff", "--quiet", "--", *catalog_paths), ("diff", "--cached", "--quiet", "--", *catalog_paths)):
        result = _run_git(devkit_root, *diff_arguments)
        if result.returncode == 1:
            raise BuildToolSyncError("Devkit runtime-python catalog or lock has uncommitted changes.")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "git diff failed"
            raise BuildToolSyncError(f"Unable to validate devkit catalog state: {message}")


def _git_output(root: Path, *arguments: str) -> str:
    result = _run_git(root, *arguments)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise BuildToolSyncError(f"Unable to validate devkit checkout: {message}")
    return result.stdout.strip()


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            env=_git_command_environment(),
            timeout=30,
        )
    except FileNotFoundError as error:
        raise BuildToolSyncError("git is required for devkit checkout validation.") from error
    except subprocess.TimeoutExpired as error:
        raise BuildToolSyncError("Devkit Git validation timed out after 30 seconds.") from error


def _sanitized_command_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "UV_")) and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["UV_NO_PROGRESS"] = "1"
    return environment


def _git_command_environment() -> dict[str, str]:
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


def _atomic_write_text(*, path: Path, content: str) -> None:
    _atomic_write_bytes(path=path, content=content.encode())


def _atomic_write_bytes(*, path: Path, content: bytes) -> None:
    file_mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(content)
    try:
        os.chmod(temporary_path, file_mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize tenant build-tool pins from the devkit runtime catalog.")
    parser.add_argument("--tenant-root", type=Path, required=True)
    parser.add_argument("--devkit-root", type=Path, required=True)
    parser.add_argument("--devkit-ref", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Exit nonzero when synchronization changes are required.")
    mode.add_argument(
        "--check-build-tools",
        action="store_true",
        help="Exit nonzero only when centrally managed addon build-tool pins differ from the catalog.",
    )
    mode.add_argument("--apply", action="store_true", help="Apply the planned changes and validate the result atomically.")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        plan = plan_build_tool_sync(
            tenant_root=arguments.tenant_root,
            devkit_root=arguments.devkit_root,
            devkit_ref=arguments.devkit_ref,
        )
        if arguments.apply and plan.changed:
            apply_build_tool_sync(plan)
        print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    except BuildToolSyncError as error:
        raise SystemExit(str(error)) from error
    if arguments.check and plan.changed:
        raise SystemExit(1)
    if arguments.check_build_tools and plan.build_tool_changed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
