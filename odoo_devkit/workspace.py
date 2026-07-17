from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .manifest import RepoDefinition, WorkspaceManifest
from .pycharm import write_pycharm_support_files
from .runtime import resolve_runtime_repo_path
from .runtime_environment import sanitized_subprocess_environment
from .workspace_contract import WorkspaceSource
from .workspace_surface import (
    LOCAL_NOTES_PATH,
    RESERVED_OVERRIDE_PATH,
    render_workspace_surface_files,
    write_workspace_surface_files,
)


@dataclass(frozen=True)
class SyncResult:
    workspace_path: Path
    lock_file_path: Path
    generated_odoo_conf_path: Path
    runtime_env_path: Path
    pycharm_metadata_path: Path
    workspace_agents_path: Path | None
    workspace_docs_index_path: Path | None
    workspace_session_prompt_path: Path | None
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
    sources = _workspace_sources(
        manifest=manifest,
        workspace_path=workspace_path,
        tenant_repo_path=tenant_repo_path,
        devkit_repo_path=devkit_repo_path,
    )
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
        sources=sources,
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
        sources=sources,
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
        workspace_session_prompt_path=workspace_surface_files.workspace_session_prompt_path,
        run_configuration_paths=run_configuration_paths,
        materialized_sources=tuple(materialized_sources),
        attached_paths=attached_paths,
    )


def workspace_status(*, manifest: WorkspaceManifest, devkit_repo_path: Path) -> dict[str, object]:
    workspace_path = resolve_workspace_path(manifest)
    tenant_repo_path = _resolve_required_repo_path(manifest.tenant_repo, manifest=manifest)
    sources = _workspace_sources(
        manifest=manifest,
        workspace_path=workspace_path,
        tenant_repo_path=tenant_repo_path,
        devkit_repo_path=devkit_repo_path,
    )
    lock_file_path = workspace_path / "workspace.lock.toml"
    lock_payload, lock_error = _read_workspace_lock(lock_file_path)
    stale_reasons: list[str] = []
    workspace_exists = workspace_path.exists()
    if not workspace_exists:
        stale_reasons.append("workspace_missing")

    lock_reasons = _workspace_lock_reasons(
        lock_payload=lock_payload,
        lock_error=lock_error,
        manifest=manifest,
        workspace_path=workspace_path,
    )
    stale_reasons.extend(lock_reasons)
    lock_current = not lock_reasons

    manifest_sha256 = _sha256_file(manifest.manifest_path)
    lock_manifest_sha256 = lock_payload.get("manifest_sha256") if lock_payload is not None else None
    manifest_current = isinstance(lock_manifest_sha256, str) and lock_manifest_sha256 == manifest_sha256
    if lock_payload is not None and not manifest_current:
        stale_reasons.append("manifest_changed_since_sync")

    surface_statuses, surface_reasons = _workspace_surface_statuses(
        manifest=manifest,
        workspace_path=workspace_path,
        sources=sources,
    )
    stale_reasons.extend(surface_reasons)
    surface_current = all(bool(surface_status["current"]) for surface_status in surface_statuses)

    source_statuses, source_reasons, source_contract_reasons = _workspace_source_statuses(
        sources=sources,
        workspace_path=workspace_path,
        lock_payload=lock_payload,
    )
    stale_reasons.extend(source_reasons)
    stale_reasons.extend(source_contract_reasons)
    if source_contract_reasons:
        lock_current = False
    materialization_current = all(bool(source_status["materialization_current"]) for source_status in source_statuses)
    source_baseline_current = all(
        source_status["baseline_available"] and not source_status["baseline_drift"] for source_status in source_statuses
    )
    managed_source_baseline_current = all(
        source_status["materialization"] != "managed_checkout"
        or (source_status["baseline_available"] and not source_status["baseline_drift"])
        for source_status in source_statuses
    )
    for source_status in source_statuses:
        if source_status["materialization"] == "managed_checkout" and source_status["baseline_drift"]:
            stale_reasons.append(f"managed_source_baseline_drift:{source_status['role']}")

    override_path = workspace_path / RESERVED_OVERRIDE_PATH
    override_exists = override_path.exists() or override_path.is_symlink()
    if override_exists:
        stale_reasons.append("reserved_agents_override_present")
    local_notes_path = workspace_path / LOCAL_NOTES_PATH
    local_notes_exists = local_notes_path.exists() or local_notes_path.is_symlink()
    local_notes_valid = not local_notes_exists or (local_notes_path.is_file() and not local_notes_path.is_symlink())
    if not local_notes_valid:
        stale_reasons.append("local_notes_invalid")
    current = (
        workspace_exists
        and lock_current
        and manifest_current
        and surface_current
        and materialization_current
        and managed_source_baseline_current
        and not override_exists
        and local_notes_valid
    )

    shared_addons_source = next((source for source in sources if source.role == "shared_addons"), None)
    runtime_source = next((source for source in sources if source.role == "runtime"), None)
    effective_runtime_repo_path = runtime_source.resolved_path if runtime_source is not None else None
    if effective_runtime_repo_path is None and manifest.runtime.instance == "local":
        effective_runtime_repo_path = devkit_repo_path.resolve()

    status_payload: dict[str, object] = {
        "schema_version": 1,
        "tenant": manifest.tenant,
        "workspace_path": str(workspace_path),
        "workspace_exists": workspace_exists,
        "current": current,
        "stale_reasons": stale_reasons,
        "lock_file_path": str(lock_file_path),
        "lock_file_exists": lock_file_path.exists(),
        "lock_file_current": lock_current,
        "lock_file_error": lock_error,
        "manifest": {
            "path": str(manifest.manifest_path),
            "sha256": manifest_sha256,
            "lock_sha256": lock_manifest_sha256,
            "current": manifest_current,
        },
        "surface_current": surface_current,
        "surfaces": surface_statuses,
        "materialization_current": materialization_current,
        "source_baseline_current": source_baseline_current,
        "managed_source_baseline_current": managed_source_baseline_current,
        "sources": source_statuses,
        "edit_roots": [
            {
                "role": source.role,
                "workspace_relative_path": source.workspace_relative_path.as_posix(),
                "resolved_path": str(source.resolved_path),
            }
            for source in sources
            if source.editable
        ],
        "local_notes": {
            "path": str(local_notes_path),
            "exists": local_notes_exists,
            "valid": local_notes_valid,
            "semantics": "supplemental_non_secret_notes",
        },
        "reserved_override": {
            "path": str(override_path),
            "exists": override_exists,
            "semantics": "full_replacement",
            "allowed_in_normal_flow": False,
        },
        "tenant_repo_path": str(tenant_repo_path),
        "devkit_repo_path": str(devkit_repo_path.resolve()),
        "runtime_context": manifest.runtime.context,
        "runtime_instance": manifest.runtime.instance,
        "attached_paths": [str((workspace_path / relative_path).resolve()) for relative_path in manifest.ide.attached_paths],
        "workspace_agents_path": str(workspace_path / "AGENTS.md"),
        "workspace_agents_exists": (workspace_path / "AGENTS.md").exists(),
        "workspace_docs_index_path": str(workspace_path / "docs" / "README.md"),
        "workspace_docs_index_exists": (workspace_path / "docs" / "README.md").exists(),
        "workspace_session_prompt_path": str(workspace_path / "docs" / "session-prompt.md"),
        "workspace_session_prompt_exists": (workspace_path / "docs" / "session-prompt.md").exists(),
    }
    if shared_addons_source is not None:
        status_payload["shared_addons_repo_path"] = str(shared_addons_source.resolved_path)
    status_payload["runtime_repo_path"] = str(effective_runtime_repo_path) if effective_runtime_repo_path is not None else None
    return status_payload


def _read_workspace_lock(lock_file_path: Path) -> tuple[dict[str, object] | None, str | None]:
    if not lock_file_path.exists():
        return None, None
    try:
        return tomllib.loads(lock_file_path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return None, f"{type(error).__name__}: {error}"


def _workspace_lock_reasons(
    *,
    lock_payload: dict[str, object] | None,
    lock_error: str | None,
    manifest: WorkspaceManifest,
    workspace_path: Path,
) -> list[str]:
    if lock_error is not None:
        return ["lock_file_invalid"]
    if lock_payload is None:
        return ["lock_file_missing"]
    reasons: list[str] = []
    if lock_payload.get("schema_version") != 1:
        reasons.append("lock_schema_unsupported")
    agent_workspace_payload = lock_payload.get("agent_workspace")
    if not isinstance(agent_workspace_payload, dict) or agent_workspace_payload.get("contract_version") != 1:
        reasons.append("agent_workspace_contract_missing")
    expected_identity = {
        "tenant": manifest.tenant,
        "workspace_name": manifest.workspace.name,
        "manifest_path": str(manifest.manifest_path),
        "workspace_path": str(workspace_path),
    }
    if any(lock_payload.get(key) != value for key, value in expected_identity.items()):
        reasons.append("lock_identity_mismatch")
    return reasons


def _workspace_surface_statuses(
    *,
    manifest: WorkspaceManifest,
    workspace_path: Path,
    sources: tuple[WorkspaceSource, ...],
) -> tuple[list[dict[str, object]], list[str]]:
    statuses: list[dict[str, object]] = []
    reasons: list[str] = []
    for definition in render_workspace_surface_files(
        manifest=manifest,
        workspace_path=workspace_path,
        sources=sources,
    ):
        exists = definition.path.exists() or definition.path.is_symlink()
        if not definition.enabled:
            current = not exists
            if not current:
                reasons.append(f"disabled_surface_present:{definition.kind}")
            statuses.append(
                {
                    "kind": definition.kind,
                    "path": str(definition.path),
                    "enabled": False,
                    "state": "disabled",
                    "exists": exists,
                    "matches_expected": current,
                    "current": current,
                }
            )
            continue

        matches_expected = False
        state = "missing"
        if exists:
            state = "stale"
            if definition.path.is_file() and not definition.path.is_symlink():
                try:
                    matches_expected = definition.path.read_text(encoding="utf-8") == definition.contents
                except (OSError, UnicodeError):
                    matches_expected = False
            if matches_expected:
                state = "current"
        if state == "missing":
            reasons.append(f"surface_missing:{definition.kind}")
        elif state == "stale":
            reasons.append(f"surface_stale:{definition.kind}")
        statuses.append(
            {
                "kind": definition.kind,
                "path": str(definition.path),
                "enabled": True,
                "state": state,
                "exists": exists,
                "matches_expected": matches_expected,
                "current": matches_expected,
            }
        )
    return statuses, reasons


def _workspace_source_statuses(
    *,
    sources: tuple[WorkspaceSource, ...],
    workspace_path: Path,
    lock_payload: dict[str, object] | None,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    statuses: list[dict[str, object]] = []
    reasons: list[str] = []
    contract_reasons: list[str] = []
    repos_payload = lock_payload.get("repos") if lock_payload is not None else None
    baseline_repos = repos_payload if isinstance(repos_payload, dict) else {}
    agent_workspace_payload = lock_payload.get("agent_workspace") if lock_payload is not None else None
    compare_contract = isinstance(agent_workspace_payload, dict) and agent_workspace_payload.get("contract_version") == 1

    for source in sources:
        workspace_entry_path = workspace_path / source.workspace_relative_path
        entry_exists = workspace_entry_path.exists() or workspace_entry_path.is_symlink()
        actual_resolved_path = workspace_entry_path.resolve(strict=False) if entry_exists else None
        if source.materialization == "linked_path":
            materialization_current = (
                workspace_entry_path.is_symlink()
                and workspace_entry_path.exists()
                and actual_resolved_path == source.resolved_path.resolve(strict=False)
                and source.resolved_path.exists()
            )
        else:
            materialization_current = (
                workspace_entry_path.exists()
                and workspace_entry_path.is_dir()
                and not workspace_entry_path.is_symlink()
                and _git_is_work_tree(workspace_entry_path)
                and _git_output(workspace_entry_path, "remote", "get-url", "origin") == source.declared_url
            )
        if not entry_exists:
            materialization_state = "missing"
            reasons.append(f"source_missing:{source.role}")
        elif materialization_current:
            materialization_state = "current"
        else:
            materialization_state = "mismatched"
            reasons.append(f"source_materialization_mismatch:{source.role}")

        repo_probe_path = actual_resolved_path if actual_resolved_path is not None else source.resolved_path
        is_git_repo = repo_probe_path.exists() and repo_probe_path.is_dir() and _git_is_work_tree(repo_probe_path)
        current_repo_state = {
            "repo_kind": "git" if is_git_repo else "directory",
            "head_commit": _git_output(repo_probe_path, "rev-parse", "HEAD") if is_git_repo else None,
            "head_branch": _git_output(repo_probe_path, "rev-parse", "--abbrev-ref", "HEAD") if is_git_repo else None,
            "dirty": _git_dirty(repo_probe_path) if is_git_repo else None,
        }
        baseline_payload = baseline_repos.get(source.role)
        baseline = baseline_payload if isinstance(baseline_payload, dict) else None
        baseline_drift = [
            field
            for field in ("head_commit", "head_branch", "dirty")
            if baseline is not None and field in baseline and baseline.get(field) != current_repo_state[field]
        ]

        if compare_contract:
            expected_contract = {
                "name": source.name,
                "resolved_path": str(source.resolved_path),
                "workspace_relative_path": source.workspace_relative_path.as_posix(),
                "declared_path": source.declared_path,
                "declared_ref": source.declared_ref,
                "declared_url_sha256": _sha256_text(source.declared_url) if source.declared_url is not None else None,
                "materialization": source.materialization,
                "editable": source.editable,
                "repo_kind": current_repo_state["repo_kind"],
            }
            if (
                baseline is None
                or "declared_url" in baseline
                or any(baseline.get(key) != value for key, value in expected_contract.items())
            ):
                contract_reasons.append(f"lock_source_contract_mismatch:{source.role}")

        statuses.append(
            {
                "role": source.role,
                "name": source.name,
                "workspace_relative_path": source.workspace_relative_path.as_posix(),
                "workspace_entry_path": str(workspace_entry_path),
                "resolved_path": str(source.resolved_path),
                "actual_resolved_path": str(actual_resolved_path) if actual_resolved_path is not None else None,
                "declared_path": source.declared_path,
                "declared_url_present": source.declared_url is not None,
                "declared_ref": source.declared_ref,
                "materialization": source.materialization,
                "materialization_state": materialization_state,
                "materialization_current": materialization_current,
                "editable": source.editable,
                "baseline_available": baseline is not None,
                "baseline": _public_repo_baseline(baseline),
                "current_repo_state": current_repo_state,
                "baseline_drift": baseline_drift,
            }
        )
    return statuses, reasons, contract_reasons


def _public_repo_baseline(baseline: dict[str, object] | None) -> dict[str, object] | None:
    if baseline is None:
        return None
    safe_keys = (
        "name",
        "workspace_relative_path",
        "resolved_path",
        "declared_path",
        "declared_ref",
        "declared_url_sha256",
        "materialization",
        "editable",
        "repo_kind",
        "head_commit",
        "head_branch",
        "dirty",
    )
    return {key: baseline[key] for key in safe_keys if key in baseline}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def _workspace_sources(
    *,
    manifest: WorkspaceManifest,
    workspace_path: Path,
    tenant_repo_path: Path,
    devkit_repo_path: Path,
) -> tuple[WorkspaceSource, ...]:
    sources = [
        WorkspaceSource(
            role="tenant",
            name=manifest.tenant_repo.name,
            workspace_relative_path=Path("sources/tenant"),
            resolved_path=tenant_repo_path.resolve(),
            declared_path=manifest.tenant_repo.path,
            declared_url=manifest.tenant_repo.url,
            declared_ref=manifest.tenant_repo.ref,
            materialization="linked_path",
            editable=True,
        )
    ]
    devkit_repo_definition = manifest.devkit_repo or RepoDefinition(name="odoo-devkit")
    sources.append(
        WorkspaceSource(
            role="devkit",
            name=devkit_repo_definition.name,
            workspace_relative_path=Path("sources/devkit"),
            resolved_path=devkit_repo_path.resolve(),
            declared_path=devkit_repo_definition.path,
            declared_url=devkit_repo_definition.url,
            declared_ref=devkit_repo_definition.ref,
            materialization="linked_path",
            editable=True,
        )
    )
    if manifest.shared_addons_repo is not None:
        sources.append(
            _workspace_source_from_definition(
                role="shared_addons",
                workspace_relative_path=Path("sources/shared-addons"),
                repo_definition=manifest.shared_addons_repo,
                manifest=manifest,
                workspace_path=workspace_path,
            )
        )
    if manifest.runtime_repo is not None:
        sources.append(
            _workspace_source_from_definition(
                role="runtime",
                workspace_relative_path=Path("sources/runtime"),
                repo_definition=manifest.runtime_repo,
                manifest=manifest,
                workspace_path=workspace_path,
            )
        )
    return tuple(sources)


def _workspace_source_from_definition(
    *,
    role: str,
    workspace_relative_path: Path,
    repo_definition: RepoDefinition,
    manifest: WorkspaceManifest,
    workspace_path: Path,
) -> WorkspaceSource:
    declared_repo_path = repo_definition.resolve_path(manifest_directory=manifest.manifest_directory)
    if declared_repo_path is not None:
        resolved_path = declared_repo_path.resolve()
        materialization = "linked_path"
        editable = True
    elif repo_definition.url is not None:
        resolved_path = workspace_path / workspace_relative_path
        materialization = "managed_checkout"
        editable = False
    else:
        raise ValueError(f"Repo {repo_definition.name} must declare either path or url")
    return WorkspaceSource(
        role=role,
        name=repo_definition.name,
        workspace_relative_path=workspace_relative_path,
        resolved_path=resolved_path,
        declared_path=repo_definition.path,
        declared_url=repo_definition.url,
        declared_ref=repo_definition.ref,
        materialization=materialization,
        editable=editable,
    )


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
    if runtime_repo_definition is None:
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
    completed_process = subprocess.run(
        ["git", *arguments],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_environment(),
    )
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
    sources: tuple[WorkspaceSource, ...],
    lock_file_path: Path,
    run_configuration_paths: tuple[Path, ...],
) -> None:
    manifest_sha256 = _sha256_file(manifest.manifest_path)
    repo_entries = [_describe_repo_state(source) for source in sources]

    lines = [
        "schema_version = 1",
        f"tenant = {json.dumps(manifest.tenant)}",
        f"workspace_name = {json.dumps(manifest.workspace.name)}",
        f"generated_at = {json.dumps(datetime.now(UTC).isoformat())}",
        f"manifest_path = {json.dumps(str(manifest.manifest_path))}",
        f"manifest_sha256 = {json.dumps(manifest_sha256)}",
        f"workspace_path = {json.dumps(str(workspace_path))}",
        "",
        "[agent_workspace]",
        "contract_version = 1",
        f"local_notes_path = {json.dumps(LOCAL_NOTES_PATH)}",
        f"reserved_override_path = {json.dumps(RESERVED_OVERRIDE_PATH)}",
        'reserved_override_semantics = "full_replacement"',
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
        repo_lines = [
            "",
            f"[repos.{repo_entry.role}]",
            f"name = {json.dumps(repo_entry.name)}",
            f"workspace_relative_path = {json.dumps(repo_entry.workspace_relative_path.as_posix())}",
            f"resolved_path = {json.dumps(str(repo_entry.resolved_path))}",
            f"materialization = {json.dumps(repo_entry.materialization)}",
            f"editable = {str(repo_entry.editable).lower()}",
            f"repo_kind = {json.dumps(repo_entry.repo_kind)}",
        ]
        _append_optional_toml_string(repo_lines, "declared_path", repo_entry.declared_path)
        _append_optional_toml_string(repo_lines, "declared_ref", repo_entry.declared_ref)
        _append_optional_toml_string(repo_lines, "declared_url_sha256", repo_entry.declared_url_sha256)
        _append_optional_toml_string(repo_lines, "head_commit", repo_entry.head_commit)
        _append_optional_toml_string(repo_lines, "head_branch", repo_entry.head_branch)
        if repo_entry.dirty is not None:
            repo_lines.append(f"dirty = {str(repo_entry.dirty).lower()}")
        lines.extend(repo_lines)
    lock_file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RepoState:
    role: str
    name: str
    workspace_relative_path: Path
    resolved_path: Path
    declared_path: str | None
    declared_ref: str | None
    declared_url_sha256: str | None
    materialization: str
    editable: bool
    repo_kind: str
    head_commit: str | None
    head_branch: str | None
    dirty: bool | None


def _describe_repo_state(source: WorkspaceSource) -> RepoState:
    is_git_repo = source.resolved_path.exists() and source.resolved_path.is_dir() and _git_is_work_tree(source.resolved_path)
    return RepoState(
        role=source.role,
        name=source.name,
        workspace_relative_path=source.workspace_relative_path,
        resolved_path=source.resolved_path,
        declared_path=source.declared_path,
        declared_ref=source.declared_ref,
        declared_url_sha256=_sha256_text(source.declared_url) if source.declared_url is not None else None,
        materialization=source.materialization,
        editable=source.editable,
        repo_kind="git" if is_git_repo else "directory",
        head_commit=_git_output(source.resolved_path, "rev-parse", "HEAD") if is_git_repo else None,
        head_branch=_git_output(source.resolved_path, "rev-parse", "--abbrev-ref", "HEAD") if is_git_repo else None,
        dirty=_git_dirty(source.resolved_path) if is_git_repo else None,
    )


def _append_optional_toml_string(lines: list[str], key: str, value: str | None) -> None:
    if value is not None:
        lines.append(f"{key} = {json.dumps(value)}")


def _git_output(repo_path: Path, *arguments: str) -> str | None:
    completed_process = subprocess.run(
        ["git", *arguments],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_environment(),
    )
    if completed_process.returncode != 0:
        return None
    output = completed_process.stdout.strip()
    return output or None


def _git_is_work_tree(repo_path: Path) -> bool:
    completed_process = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_environment(),
    )
    if completed_process.returncode != 0:
        return False
    return completed_process.stdout.strip() == "true"


def _git_dirty(repo_path: Path) -> bool:
    completed_process = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_environment(),
    )
    if completed_process.returncode != 0:
        return False
    return bool(completed_process.stdout.strip())


def _format_string_list(values: tuple[str, ...]) -> str:
    if not values:
        return "[]"
    quoted_values = ", ".join(json.dumps(value) for value in values)
    return f"[{quoted_values}]"
