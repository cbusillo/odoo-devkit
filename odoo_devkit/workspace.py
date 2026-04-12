from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .manifest import RepoDefinition, WorkspaceManifest
from .pycharm import write_pycharm_support_files
from .runtime import resolve_runtime_repo_path
from .workspace_surface import write_workspace_surface_files


@dataclass(frozen=True)
class SyncResult:
    workspace_path: Path
    lock_file_path: Path
    generated_odoo_conf_path: Path
    runtime_env_path: Path
    pycharm_metadata_path: Path
    workspace_agents_path: Path | None
    workspace_docs_index_path: Path | None
    run_configuration_paths: tuple[Path, ...]
    materialized_sources: tuple[Path, ...]
    attached_paths: tuple[Path, ...]


def resolve_workspace_path(manifest: WorkspaceManifest) -> Path:
    workspace_root_override = os.environ.get("ODOO_WORKSPACE_ROOT")
    workspace_root = workspace_root_override or manifest.workspace.workspace_root or "~/Developer/odoo-workspaces"
    expanded_workspace_root = Path(os.path.expanduser(workspace_root))
    if not expanded_workspace_root.is_absolute():
        expanded_workspace_root = (manifest.manifest_directory / expanded_workspace_root).resolve()
    else:
        expanded_workspace_root = expanded_workspace_root.resolve()
    return expanded_workspace_root / manifest.workspace.name


def sync_workspace(*, manifest: WorkspaceManifest, devkit_repo_path: Path) -> SyncResult:
    workspace_path = resolve_workspace_path(manifest)
    workspace_path.mkdir(parents=True, exist_ok=True)
    generated_directory = workspace_path / ".generated"
    sources_directory = workspace_path / "sources"
    state_directory = workspace_path / "state"
    for managed_directory in (
        generated_directory,
        sources_directory,
        state_directory / "logs",
        state_directory / "cache",
        state_directory / "data",
    ):
        managed_directory.mkdir(parents=True, exist_ok=True)

    tenant_repo_path = _resolve_required_repo_path(manifest.tenant_repo, manifest=manifest)
    shared_addons_repo_path = _materialize_optional_repo(
        repo_definition=manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=sources_directory / "shared-addons",
    )
    runtime_repo_path = _materialize_runtime_repo(
        manifest=manifest,
        managed_checkout_path=sources_directory / "runtime",
    )
    effective_runtime_repo_path = _resolve_workspace_runtime_repo_path(
        manifest=manifest,
        devkit_repo_path=devkit_repo_path,
        materialized_runtime_repo_path=runtime_repo_path,
    )
    materialized_sources = [
        _ensure_symlink(sources_directory / "tenant", tenant_repo_path),
        _ensure_symlink(sources_directory / "devkit", devkit_repo_path.resolve()),
    ]
    if shared_addons_repo_path is not None:
        materialized_sources.append(shared_addons_repo_path)
    if runtime_repo_path is not None:
        materialized_sources.append(runtime_repo_path)
    attached_paths = tuple((workspace_path / relative_path).resolve() for relative_path in manifest.ide.attached_paths)

    generated_odoo_conf_path = generated_directory / "odoo.conf"
    _write_odoo_conf(
        manifest=manifest,
        workspace_path=workspace_path,
        generated_odoo_conf_path=generated_odoo_conf_path,
    )
    runtime_env_path = generated_directory / "runtime.env"
    _write_runtime_env(
        manifest=manifest,
        workspace_path=workspace_path,
        tenant_repo_path=tenant_repo_path,
        shared_addons_repo_path=shared_addons_repo_path,
        devkit_repo_path=devkit_repo_path,
        runtime_repo_path=effective_runtime_repo_path,
        runtime_env_path=runtime_env_path,
    )
    workspace_surface_files = write_workspace_surface_files(
        manifest=manifest,
        workspace_path=workspace_path,
        tenant_repo_path=tenant_repo_path,
        devkit_repo_path=devkit_repo_path.resolve(),
        shared_addons_repo_path=shared_addons_repo_path,
    )
    pycharm_metadata_path, run_configuration_paths = write_pycharm_support_files(
        manifest=manifest,
        tenant_repo_path=tenant_repo_path,
        workspace_path=workspace_path,
        generated_odoo_conf_path=generated_odoo_conf_path,
        attached_paths=attached_paths,
    )
    lock_file_path = workspace_path / "workspace.lock.toml"
    _write_lock_file(
        manifest=manifest,
        workspace_path=workspace_path,
        tenant_repo_path=tenant_repo_path,
        shared_addons_repo_path=shared_addons_repo_path,
        devkit_repo_path=devkit_repo_path,
        runtime_repo_path=effective_runtime_repo_path,
        lock_file_path=lock_file_path,
        run_configuration_paths=run_configuration_paths,
    )
    return SyncResult(
        workspace_path=workspace_path,
        lock_file_path=lock_file_path,
        generated_odoo_conf_path=generated_odoo_conf_path,
        runtime_env_path=runtime_env_path,
        pycharm_metadata_path=pycharm_metadata_path,
        workspace_agents_path=workspace_surface_files.workspace_agents_path,
        workspace_docs_index_path=workspace_surface_files.workspace_docs_index_path,
        run_configuration_paths=run_configuration_paths,
        materialized_sources=tuple(materialized_sources),
        attached_paths=attached_paths,
    )


def workspace_status(*, manifest: WorkspaceManifest, devkit_repo_path: Path) -> dict[str, object]:
    workspace_path = resolve_workspace_path(manifest)
    tenant_repo_path = _resolve_required_repo_path(manifest.tenant_repo, manifest=manifest)
    status_payload: dict[str, object] = {
        "tenant": manifest.tenant,
        "workspace_path": str(workspace_path),
        "workspace_exists": workspace_path.exists(),
        "lock_file_path": str(workspace_path / "workspace.lock.toml"),
        "lock_file_exists": (workspace_path / "workspace.lock.toml").exists(),
        "tenant_repo_path": str(tenant_repo_path),
        "devkit_repo_path": str(devkit_repo_path.resolve()),
        "runtime_context": manifest.runtime.context,
        "runtime_instance": manifest.runtime.instance,
        "attached_paths": [str((workspace_path / relative_path).resolve()) for relative_path in manifest.ide.attached_paths],
        "workspace_agents_path": str(workspace_path / "AGENTS.md"),
        "workspace_agents_exists": (workspace_path / "AGENTS.md").exists(),
        "workspace_docs_index_path": str(workspace_path / "docs" / "README.md"),
        "workspace_docs_index_exists": (workspace_path / "docs" / "README.md").exists(),
    }
    shared_addons_repo_path = resolve_optional_repo_path_with_managed_checkout(
        manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "shared-addons",
    )
    if shared_addons_repo_path is not None:
        status_payload["shared_addons_repo_path"] = str(shared_addons_repo_path)
    effective_runtime_repo_path = _resolve_workspace_runtime_repo_path(
        manifest=manifest,
        devkit_repo_path=devkit_repo_path,
        materialized_runtime_repo_path=resolve_optional_repo_path_with_managed_checkout(
            manifest.runtime_repo,
            manifest=manifest,
            managed_checkout_path=workspace_path / "sources" / "runtime",
        ),
    )
    status_payload["runtime_repo_path"] = str(effective_runtime_repo_path) if effective_runtime_repo_path is not None else None
    return status_payload


def clean_workspace(*, manifest: WorkspaceManifest) -> Path:
    workspace_path = resolve_workspace_path(manifest)
    if workspace_path.exists():
        shutil.rmtree(workspace_path)
    return workspace_path


def run_in_workspace(*, manifest: WorkspaceManifest, command: tuple[str, ...]) -> int:
    if not command:
        raise ValueError("Expected a command after --")
    workspace_path = resolve_workspace_path(manifest)
    if not workspace_path.exists():
        raise ValueError(f"Workspace does not exist yet: {workspace_path}")
    completed_process = subprocess.run(command, cwd=workspace_path)
    return completed_process.returncode


def _resolve_required_repo_path(repo_definition: RepoDefinition, *, manifest: WorkspaceManifest) -> Path:
    repo_path = repo_definition.resolve_path(manifest_directory=manifest.manifest_directory)
    if repo_path is None:
        raise ValueError(f"Repo {repo_definition.name} must declare a path for the current bootstrap flow")
    if not repo_path.exists():
        raise ValueError(f"Repo path does not exist: {repo_path}")
    return repo_path


def _resolve_workspace_runtime_repo_path(
    *,
    manifest: WorkspaceManifest,
    devkit_repo_path: Path,
    materialized_runtime_repo_path: Path | None,
) -> Path | None:
    if materialized_runtime_repo_path is not None:
        return materialized_runtime_repo_path.resolve()
    if manifest.runtime_repo is not None:
        return resolve_runtime_repo_path(manifest)
    if manifest.runtime.instance == "local":
        return devkit_repo_path.resolve()
    return None


def resolve_optional_repo_path(repo_definition: RepoDefinition | None, *, manifest: WorkspaceManifest) -> Path | None:
    return resolve_optional_repo_path_with_managed_checkout(
        repo_definition,
        manifest=manifest,
        managed_checkout_path=None,
    )


def resolve_optional_repo_path_with_managed_checkout(
    repo_definition: RepoDefinition | None,
    *,
    manifest: WorkspaceManifest,
    managed_checkout_path: Path | None,
) -> Path | None:
    if repo_definition is None:
        return None
    repo_path = repo_definition.resolve_path(manifest_directory=manifest.manifest_directory)
    if repo_path is not None:
        if not repo_path.exists():
            raise ValueError(f"Optional repo path does not exist: {repo_path}")
        return repo_path
    if repo_definition.url is None:
        return None
    if managed_checkout_path is None or not managed_checkout_path.exists():
        return None
    if not _git_is_work_tree(managed_checkout_path):
        raise ValueError(f"Managed repo checkout is not a git work tree: {managed_checkout_path}")
    _assert_managed_repo_origin(managed_checkout_path, repo_definition=repo_definition)
    return managed_checkout_path.resolve()


def _materialize_optional_repo(
    *,
    repo_definition: RepoDefinition | None,
    manifest: WorkspaceManifest,
    managed_checkout_path: Path,
) -> Path | None:
    if repo_definition is None:
        return None
    repo_path = resolve_optional_repo_path(repo_definition, manifest=manifest)
    if repo_path is not None:
        return _ensure_symlink(managed_checkout_path, repo_path)
    if repo_definition.url is None:
        return None
    if not repo_definition.ref:
        raise ValueError(
            f"Repo {repo_definition.name} must declare ref when workspace sync materializes it from url {repo_definition.url!r}."
        )
    return _ensure_managed_repo_checkout(managed_checkout_path=managed_checkout_path, repo_definition=repo_definition)


def _materialize_runtime_repo(*, manifest: WorkspaceManifest, managed_checkout_path: Path) -> Path | None:
    runtime_repo_definition = manifest.runtime_repo
    if runtime_repo_definition is None or runtime_repo_definition.url is None:
        return None
    return _materialize_optional_repo(
        repo_definition=runtime_repo_definition,
        manifest=manifest,
        managed_checkout_path=managed_checkout_path,
    )


def _ensure_managed_repo_checkout(*, managed_checkout_path: Path, repo_definition: RepoDefinition) -> Path:
    declared_url = repo_definition.url
    declared_ref = repo_definition.ref
    assert declared_url is not None
    assert declared_ref is not None
    if managed_checkout_path.exists() and not _git_is_work_tree(managed_checkout_path):
        raise ValueError(f"Managed repo checkout path is not a git work tree: {managed_checkout_path}")
    if managed_checkout_path.exists():
        _assert_managed_repo_origin(managed_checkout_path, repo_definition=repo_definition)
        if _git_dirty(managed_checkout_path):
            raise ValueError(
                f"Managed repo checkout is dirty: {managed_checkout_path}. Clean it or remove the workspace before syncing again."
            )
        _run_git_command(managed_checkout_path, "fetch", "--tags", "--prune", "origin", declared_ref)
    else:
        managed_checkout_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git_command(None, "clone", "--origin", "origin", declared_url, str(managed_checkout_path))
        _run_git_command(managed_checkout_path, "fetch", "--tags", "--prune", "origin", declared_ref)
    _run_git_command(managed_checkout_path, "checkout", "--detach", "FETCH_HEAD")
    return managed_checkout_path.resolve()


def _assert_managed_repo_origin(repo_path: Path, *, repo_definition: RepoDefinition) -> None:
    declared_url = repo_definition.url
    if declared_url is None:
        return
    current_origin = _git_output(repo_path, "remote", "get-url", "origin")
    if current_origin != declared_url:
        raise ValueError(f"Managed repo checkout {repo_path} points at origin {current_origin!r}, expected {declared_url!r}.")


def _run_git_command(repo_path: Path | None, *arguments: str) -> None:
    completed_process = subprocess.run(["git", *arguments], cwd=repo_path, capture_output=True, text=True)
    if completed_process.returncode != 0:
        stderr = completed_process.stderr.strip()
        stdout = completed_process.stdout.strip()
        command = "git " + " ".join(arguments)
        details = stderr or stdout or f"exit {completed_process.returncode}"
        raise ValueError(f"{command} failed: {details}")


def _ensure_symlink(link_path: Path, target_path: Path) -> Path:
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink() and link_path.resolve() == target_path.resolve():
            return link_path
        if link_path.is_symlink():
            link_path.unlink()
            link_path.symlink_to(target_path)
            return link_path
        raise ValueError(f"Managed path already exists and is not the expected symlink: {link_path}")
    link_path.symlink_to(target_path)
    return link_path


def _write_odoo_conf(*, manifest: WorkspaceManifest, workspace_path: Path, generated_odoo_conf_path: Path) -> None:
    rendered_addons_paths = [str((workspace_path / relative_path).resolve()) for relative_path in manifest.runtime.addons_paths]
    lines = [
        "[options]",
        f"db_name = {manifest.runtime.database}",
        f"addons_path = {','.join(rendered_addons_paths)}",
        f"data_dir = {workspace_path / 'state' / 'data'}",
        f"logfile = {workspace_path / 'state' / 'logs' / 'odoo.log'}",
        "list_db = False",
    ]
    if manifest.runtime.web_base_url is not None:
        lines.append(f"proxy_mode = {manifest.runtime.web_base_url.startswith('https://')}")
    generated_odoo_conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_runtime_env(
    *,
    manifest: WorkspaceManifest,
    workspace_path: Path,
    tenant_repo_path: Path,
    shared_addons_repo_path: Path | None,
    devkit_repo_path: Path,
    runtime_repo_path: Path | None,
    runtime_env_path: Path,
) -> None:
    lines = [
        f"ODOO_TENANT={manifest.tenant}",
        f"ODOO_WORKSPACE_PATH={workspace_path}",
        f"ODOO_WORKSPACE_MANIFEST={manifest.manifest_path}",
        f"ODOO_WORKSPACE_TENANT_REPO={tenant_repo_path}",
        f"ODOO_WORKSPACE_DEVKIT_REPO={devkit_repo_path.resolve()}",
        f"ODOO_WORKSPACE_RUNTIME_CONTEXT={manifest.runtime.context}",
        f"ODOO_WORKSPACE_RUNTIME_INSTANCE={manifest.runtime.instance}",
        f"ODOO_WORKSPACE_PYTHON_VERSION={manifest.workspace.python_version}",
    ]
    if shared_addons_repo_path is not None:
        lines.append(f"ODOO_WORKSPACE_SHARED_ADDONS_REPO={shared_addons_repo_path}")
    if runtime_repo_path is not None:
        lines.append(f"ODOO_WORKSPACE_RUNTIME_REPO={runtime_repo_path}")
    runtime_env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_lock_file(
    *,
    manifest: WorkspaceManifest,
    workspace_path: Path,
    tenant_repo_path: Path,
    shared_addons_repo_path: Path | None,
    devkit_repo_path: Path,
    runtime_repo_path: Path | None,
    lock_file_path: Path,
    run_configuration_paths: tuple[Path, ...],
) -> None:
    manifest_text = manifest.manifest_path.read_text(encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest_text.encode()).hexdigest()
    repo_entries = [
        _describe_repo_state("tenant", manifest.tenant_repo, tenant_repo_path),
        _describe_repo_state("devkit", manifest.devkit_repo or RepoDefinition(name="odoo-devkit"), devkit_repo_path.resolve()),
    ]
    if manifest.shared_addons_repo is not None and shared_addons_repo_path is not None:
        repo_entries.append(_describe_repo_state("shared_addons", manifest.shared_addons_repo, shared_addons_repo_path))
    runtime_repo_definition = manifest.runtime_repo
    if runtime_repo_definition is not None:
        resolved_runtime_repo_path = runtime_repo_path or resolve_runtime_repo_path(manifest)
        repo_entries.append(_describe_repo_state("runtime", runtime_repo_definition, resolved_runtime_repo_path))

    lines = [
        "schema_version = 1",
        f"tenant = {json.dumps(manifest.tenant)}",
        f"workspace_name = {json.dumps(manifest.workspace.name)}",
        f"generated_at = {json.dumps(datetime.now(UTC).isoformat())}",
        f"manifest_path = {json.dumps(str(manifest.manifest_path))}",
        f"manifest_sha256 = {json.dumps(manifest_sha256)}",
        f"workspace_path = {json.dumps(str(workspace_path))}",
        "",
        "[runtime]",
        f"context = {json.dumps(manifest.runtime.context)}",
        f"instance = {json.dumps(manifest.runtime.instance)}",
        f"database = {json.dumps(manifest.runtime.database)}",
        f"addons_paths = {_format_string_list(manifest.runtime.addons_paths)}",
        "",
        "[generated]",
        f"odoo_conf_path = {json.dumps(str(workspace_path / '.generated' / 'odoo.conf'))}",
        f"runtime_env_path = {json.dumps(str(workspace_path / '.generated' / 'runtime.env'))}",
        f"run_configurations = {_format_string_list(tuple(str(path) for path in run_configuration_paths))}",
    ]
    for repo_entry in repo_entries:
        lines.extend(
            [
                "",
                f"[repos.{repo_entry.role}]",
                f"name = {json.dumps(repo_entry.name)}",
                f"resolved_path = {json.dumps(str(repo_entry.resolved_path))}",
                f"declared_ref = {json.dumps(repo_entry.declared_ref)}",
                f"declared_url = {json.dumps(repo_entry.declared_url)}",
                f"head_commit = {json.dumps(repo_entry.head_commit)}",
                f"head_branch = {json.dumps(repo_entry.head_branch)}",
                f"dirty = {str(repo_entry.dirty).lower()}",
            ]
        )
    lock_file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RepoState:
    role: str
    name: str
    resolved_path: Path
    declared_ref: str | None
    declared_url: str | None
    head_commit: str | None
    head_branch: str | None
    dirty: bool


def _describe_repo_state(role: str, repo_definition: RepoDefinition, repo_path: Path) -> RepoState:
    return RepoState(
        role=role,
        name=repo_definition.name,
        resolved_path=repo_path,
        declared_ref=repo_definition.ref,
        declared_url=repo_definition.url,
        head_commit=_git_output(repo_path, "rev-parse", "HEAD"),
        head_branch=_git_output(repo_path, "rev-parse", "--abbrev-ref", "HEAD"),
        dirty=_git_dirty(repo_path),
    )


def _git_output(repo_path: Path, *arguments: str) -> str | None:
    completed_process = subprocess.run(["git", *arguments], cwd=repo_path, capture_output=True, text=True)
    if completed_process.returncode != 0:
        return None
    output = completed_process.stdout.strip()
    return output or None


def _git_is_work_tree(repo_path: Path) -> bool:
    completed_process = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path, capture_output=True, text=True)
    if completed_process.returncode != 0:
        return False
    return completed_process.stdout.strip() == "true"


def _git_dirty(repo_path: Path) -> bool:
    completed_process = subprocess.run(["git", "status", "--short"], cwd=repo_path, capture_output=True, text=True)
    if completed_process.returncode != 0:
        return False
    return bool(completed_process.stdout.strip())


def _format_string_list(values: tuple[str, ...]) -> str:
    if not values:
        return "[]"
    quoted_values = ", ".join(json.dumps(value) for value in values)
    return f"[{quoted_values}]"
