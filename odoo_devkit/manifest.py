from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from odoo_devkit.toml_values import read_optional_string as _read_optional_string
from odoo_devkit.toml_values import read_required_string as _read_required_string

REPOSITORY_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RepoDefinition:
    name: str
    path: str | None = None
    url: str | None = None
    ref: str | None = None
    repository: str | None = None

    def resolve_path(self, *, manifest_directory: Path) -> Path | None:
        if self.path is None:
            return None
        expanded_path = Path(os.path.expanduser(self.path))
        if expanded_path.is_absolute():
            return expanded_path.resolve()
        return (manifest_directory / expanded_path).resolve()


@dataclass(frozen=True)
class WorkspaceDefinition:
    name: str
    python_version: str
    workspace_root: str | None = None


@dataclass(frozen=True)
class RuntimeDefinition:
    context: str
    instance: str
    database: str
    addons_paths: tuple[str, ...]
    web_base_url: str | None = None


@dataclass(frozen=True)
class RunConfigurationDefinition:
    name: str
    command: tuple[str, ...]
    working_directory: str
    shell_path: str = "/bin/zsh"
    execute_in_terminal: bool = True


@dataclass(frozen=True)
class IdeDefinition:
    mode: str
    focus_paths: tuple[str, ...]
    attached_paths: tuple[str, ...]
    run_configurations: tuple[RunConfigurationDefinition, ...]


@dataclass(frozen=True)
class CodexDefinition:
    workspace_agents: bool = True
    workspace_docs_index: bool = True


@dataclass(frozen=True)
class ArtifactsDefinition:
    inputs_file: str | None = None


@dataclass(frozen=True)
class BuildDefinition:
    """What a tenant's artifact is built from; the build records the exact digests it used."""

    odoo_version: str | None = None
    base_runtime_image: str | None = None
    base_devtools_image: str | None = None


@dataclass(frozen=True)
class WorkspaceManifest:
    schema_version: int
    tenant: str
    manifest_path: Path
    workspace: WorkspaceDefinition
    runtime: RuntimeDefinition
    ide: IdeDefinition
    codex: CodexDefinition
    artifacts: ArtifactsDefinition
    tenant_repo: RepoDefinition
    build: BuildDefinition = BuildDefinition()
    shared_addons_repo: RepoDefinition | None = None
    devkit_repo: RepoDefinition | None = None
    runtime_repo: RepoDefinition | None = None

    @property
    def manifest_directory(self) -> Path:
        return self.manifest_path.parent


def load_workspace_manifest(manifest_path: Path) -> WorkspaceManifest:
    manifest_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    schema_version = int(manifest_data.get("schema_version", 0))
    if schema_version != 1:
        raise ValueError(f"Unsupported workspace schema_version: {schema_version}")

    tenant_name = _read_required_string(manifest_data, "tenant")
    workspace_table = _read_required_table(manifest_data, "workspace")
    runtime_table = _read_required_table(manifest_data, "runtime")
    ide_table = _read_required_table(manifest_data, "ide")
    codex_table = _read_optional_table(manifest_data, "codex")
    artifacts_table = _read_optional_table(manifest_data, "artifacts")
    build_table = _read_optional_table(manifest_data, "build")
    repositories_table = _read_required_table(manifest_data, "repos")
    tenant_repo = _parse_repo_definition(repositories_table, "tenant")
    shared_addons_repo = _parse_optional_repo_definition(repositories_table, "shared_addons")
    devkit_repo = _parse_optional_repo_definition(repositories_table, "devkit")
    runtime_repo = _parse_optional_repo_definition(repositories_table, "runtime")

    workspace_definition = WorkspaceDefinition(
        name=_read_required_string(workspace_table, "name"),
        python_version=_read_required_string(workspace_table, "python"),
        workspace_root=_read_optional_string(workspace_table, "workspace_root"),
    )
    runtime_definition = RuntimeDefinition(
        context=_read_required_string(runtime_table, "context"),
        instance=_read_required_string(runtime_table, "instance"),
        database=_read_required_string(runtime_table, "database"),
        addons_paths=_read_string_tuple(runtime_table, "addons_paths"),
        web_base_url=_read_optional_string(runtime_table, "web_base_url"),
    )

    run_configurations = ide_table.get("run_configurations", [])
    if not isinstance(run_configurations, list):
        raise ValueError("Expected ide.run_configurations to be an array of tables")
    run_configuration_definitions = tuple(_parse_run_configuration_definition(entry) for entry in run_configurations)
    ide_definition = IdeDefinition(
        mode=_read_required_string(ide_table, "mode"),
        focus_paths=_read_string_tuple(ide_table, "focus_paths"),
        attached_paths=_read_string_tuple(ide_table, "attached_paths"),
        run_configurations=run_configuration_definitions,
    )
    codex_definition = CodexDefinition(
        workspace_agents=_read_optional_bool(codex_table, "workspace_agents", default=True),
        workspace_docs_index=_read_optional_bool(codex_table, "workspace_docs_index", default=True),
    )
    artifacts_definition = ArtifactsDefinition(
        inputs_file=_read_optional_string(artifacts_table, "inputs_file"),
    )
    build_definition = BuildDefinition(
        odoo_version=_read_optional_nonempty_string(build_table, "odoo_version"),
        base_runtime_image=_read_optional_nonempty_string(build_table, "base_runtime_image"),
        base_devtools_image=_read_optional_nonempty_string(build_table, "base_devtools_image"),
    )
    return WorkspaceManifest(
        schema_version=schema_version,
        tenant=tenant_name,
        manifest_path=manifest_path.resolve(),
        workspace=workspace_definition,
        runtime=runtime_definition,
        ide=ide_definition,
        codex=codex_definition,
        artifacts=artifacts_definition,
        tenant_repo=tenant_repo,
        build=build_definition,
        shared_addons_repo=shared_addons_repo,
        devkit_repo=devkit_repo,
        runtime_repo=runtime_repo,
    )


def _parse_repo_definition(repositories_table: dict[str, object], key: str) -> RepoDefinition:
    return _repo_definition(_read_required_table(repositories_table, key))


def _parse_optional_repo_definition(repositories_table: dict[str, object], key: str) -> RepoDefinition | None:
    if key not in repositories_table:
        return None
    repository_value = repositories_table[key]
    if not isinstance(repository_value, dict):
        raise ValueError(f"Expected [repos.{key}] to be a table")
    return _repo_definition(repository_value)


def _repo_definition(repository_table: dict[str, object]) -> RepoDefinition:
    repository = _read_optional_nonempty_string(repository_table, "repository")
    if repository is not None and REPOSITORY_SLUG_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"Expected repository to be an owner/name slug, got {repository!r}")
    return RepoDefinition(
        name=_read_required_string(repository_table, "name"),
        path=_read_optional_string(repository_table, "path"),
        url=_read_optional_string(repository_table, "url"),
        ref=_read_optional_string(repository_table, "ref"),
        repository=repository,
    )


def _parse_run_configuration_definition(entry: object) -> RunConfigurationDefinition:
    if not isinstance(entry, dict):
        raise ValueError("Expected [[ide.run_configurations]] entries to be tables")
    command_value = entry.get("command")
    if not isinstance(command_value, list) or not all(isinstance(item, str) for item in command_value):
        raise ValueError("Expected ide.run_configurations.command to be a string array")
    return RunConfigurationDefinition(
        name=_read_required_string(entry, "name"),
        command=tuple(command_value),
        working_directory=_read_required_string(entry, "working_directory"),
        shell_path=_read_optional_string(entry, "shell_path") or "/bin/zsh",
        execute_in_terminal=_read_optional_bool(entry, "execute_in_terminal", default=True),
    )


def _read_required_table(source: dict[str, object], key: str) -> dict[str, object]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Expected [{key}] to be a table")
    return value


def _read_optional_table(source: dict[str, object], key: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected [{key}] to be a table when present")
    return value


def _read_optional_nonempty_string(source: dict[str, object], key: str) -> str | None:
    value = _read_optional_string(source, key)
    if value is not None and not value.strip():
        raise ValueError(f"Expected {key} to be a non-empty string when present")
    return value.strip() if value is not None else None


def _read_optional_bool(source: dict[str, object], key: str, *, default: bool) -> bool:
    value = source.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"Expected {key} to be a boolean when present")
    return value


def _read_string_tuple(source: dict[str, object], key: str) -> tuple[str, ...]:
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected {key} to be a string array")
    return tuple(value)
