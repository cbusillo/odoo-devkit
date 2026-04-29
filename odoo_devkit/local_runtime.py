from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .artifact_inputs import (
    ArtifactInputsDefinition,
    ArtifactInputsError,
    effective_artifact_input_sources,
    load_artifact_inputs_definition,
)
from .ide_support import write_pycharm_odoo_conf
from .manifest import WorkspaceManifest

ScalarValue = str | int | float | bool
ScalarMap = dict[str, ScalarValue]
DEFAULT_ARTIFACT_IMAGE_PLATFORMS = ("linux/amd64", "linux/arm64")
GIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{7,40}")
ARTIFACT_SOURCE_ENV_KEYS = ("ODOO_ADDON_REPOSITORIES", "OPENUPGRADE_ADDON_REPOSITORY")
RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR = "ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON"
ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
LEGACY_CONFIG_PARAM_PREFIX = "ENV_OVERRIDE_CONFIG_PARAM__"
LEGACY_AUTHENTIK_PREFIX = "ENV_OVERRIDE_AUTHENTIK__"
LEGACY_SHOPIFY_PREFIX = "ENV_OVERRIDE_SHOPIFY__"
LEGACY_SETTING_OVERRIDE_PREFIXES = (
    LEGACY_CONFIG_PARAM_PREFIX,
    LEGACY_AUTHENTIK_PREFIX,
    LEGACY_SHOPIFY_PREFIX,
)

PLATFORM_RUNTIME_ENV_KEYS = (
    "PLATFORM_CONTEXT",
    "PLATFORM_INSTANCE",
    "PLATFORM_RUNTIME_ENV_FILE",
    "PYTHON_VERSION",
    "ODOO_VERSION",
    "ODOO_STACK_NAME",
    "ODOO_PROJECT_NAME",
    "ODOO_STATE_ROOT",
    "ODOO_RUNTIME_CONF_HOST_PATH",
    "ODOO_PROJECT_ADDONS_HOST_PATH",
    "ODOO_SHARED_ADDONS_HOST_PATH",
    "DOCKER_IMAGE",
    "DOCKER_IMAGE_TAG",
    "DOCKER_IMAGE_REFERENCE",
    "COMPOSE_BUILD_TARGET",
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
    "ODOO_DB_NAME",
    "ODOO_DB_USER",
    "ODOO_DB_PASSWORD",
    "ODOO_FILESTORE_PATH",
    "ODOO_MASTER_PASSWORD",
    "ODOO_ADMIN_LOGIN",
    "ODOO_INSTALL_MODULES",
    "ODOO_ADDON_REPOSITORIES",
    "ODOO_UPDATE_MODULES",
    "ODOO_ADDONS_PATH",
    "ODOO_WEB_HOST_PORT",
    "ODOO_LONGPOLL_HOST_PORT",
    "ODOO_DB_HOST_PORT",
    "ODOO_LIST_DB",
    "ODOO_WEB_COMMAND",
    "ODOO_DATA_WORKFLOW_LOCK_FILE",
    "ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS",
    "ODOO_DB_MAXCONN",
    "ODOO_DB_MAXCONN_GEVENT",
    "ODOO_WORKERS",
    "ODOO_MAX_CRON_THREADS",
    "ODOO_LIMIT_TIME_CPU",
    "ODOO_LIMIT_TIME_REAL",
    "ODOO_LIMIT_TIME_REAL_CRON",
    "ODOO_LIMIT_TIME_WORKER_CRON",
    "ODOO_LIMIT_MEMORY_SOFT",
    "ODOO_LIMIT_MEMORY_HARD",
    "ODOO_DEV_MODE",
    "ODOO_LOGFILE",
    "POSTGRES_MAX_CONNECTIONS",
    "POSTGRES_SHARED_BUFFERS",
    "POSTGRES_EFFECTIVE_CACHE_SIZE",
    "POSTGRES_WORK_MEM",
    "POSTGRES_MAINTENANCE_WORK_MEM",
    "POSTGRES_MAX_WAL_SIZE",
    "POSTGRES_MIN_WAL_SIZE",
    "POSTGRES_CHECKPOINT_TIMEOUT",
    "POSTGRES_RANDOM_PAGE_COST",
    "POSTGRES_EFFECTIVE_IO_CONCURRENCY",
    "DATA_WORKFLOW_SSH_DIR",
    "DATA_WORKFLOW_SSH_KEY",
    "ODOO_UPSTREAM_HOST",
    "ODOO_UPSTREAM_USER",
    "ODOO_UPSTREAM_DB_NAME",
    "ODOO_UPSTREAM_DB_USER",
    "ODOO_UPSTREAM_FILESTORE_PATH",
    "OPENUPGRADE_ENABLED",
    "OPENUPGRADE_ADDON_REPOSITORY",
    "OPENUPGRADE_SCRIPTS_PATH",
    "OPENUPGRADE_TARGET_VERSION",
    "OPENUPGRADE_SKIP_UPDATE_ADDONS",
    "OPENUPGRADELIB_INSTALL_SPEC",
    "GITHUB_TOKEN",
    "DOKPLOY_HOST",
    "DOKPLOY_TOKEN",
)

PLATFORM_RUNTIME_PASSTHROUGH_PREFIXES = (
    "ENV_OVERRIDE_",
    "ODOO_UPSTREAM_",
)

PLATFORM_RUNTIME_PASSTHROUGH_KEYS = (
    "ODOO_KEY",
    "DATA_WORKFLOW_SSH_KEY",
    "DOKPLOY_HOST",
    "DOKPLOY_TOKEN",
    "ODOO_BASE_RUNTIME_IMAGE",
    "ODOO_BASE_DEVTOOLS_IMAGE",
    "DOCKER_IMAGE_REFERENCE",
)

DATA_WORKFLOW_SCRIPT = "/volumes/scripts/run_odoo_data_workflows.py"
DATA_WORKFLOW_SCRIPT_ENV_KEYS = {
    "ODOO_DB_HOST",
    "ODOO_DB_PORT",
    "ODOO_DB_USER",
    "ODOO_DB_PASSWORD",
    "ODOO_DB_NAME",
    "ODOO_FILESTORE_PATH",
    "ODOO_FILESTORE_OWNER",
    "DATA_WORKFLOW_SSH_DIR",
    "DATA_WORKFLOW_SSH_KEY",
    "ODOO_PROJECT_NAME",
    "ODOO_VERSION",
    "ODOO_ADDONS_PATH",
    "ODOO_ADDON_REPOSITORIES",
    "ODOO_INSTALL_MODULES",
    "ODOO_UPDATE_MODULES",
    "LOCAL_ADDONS_DIRS",
    "OPENUPGRADE_ENABLED",
    "OPENUPGRADE_SCRIPTS_PATH",
    "OPENUPGRADE_TARGET_VERSION",
    "OPENUPGRADE_SKIP_UPDATE_ADDONS",
    "ODOO_KEY",
    "ODOO_ADMIN_LOGIN",
    "ODOO_ADMIN_PASSWORD",
    "ODOO_DATA_WORKFLOW_LOCK_FILE",
    "ODOO_UPSTREAM_HOST",
    "ODOO_UPSTREAM_USER",
    "ODOO_UPSTREAM_DB_NAME",
    "ODOO_UPSTREAM_DB_USER",
    "ODOO_UPSTREAM_FILESTORE_PATH",
    "BOOTSTRAP",
    "NO_SANITIZE",
}
DATA_WORKFLOW_SCRIPT_ENV_PREFIXES = (
    "ENV_OVERRIDE_",
    "OPENUPGRADE_",
)
REQUIRED_UPSTREAM_ENV_KEYS = (
    "ODOO_UPSTREAM_HOST",
    "ODOO_UPSTREAM_USER",
    "ODOO_UPSTREAM_DB_NAME",
    "ODOO_UPSTREAM_DB_USER",
    "ODOO_UPSTREAM_FILESTORE_PATH",
)

GHCR_HOST = "ghcr.io"
PLACEHOLDER_REGISTRY_HOST = "registry.invalid"
DEFAULT_ODOO_BASE_RUNTIME_IMAGE = "registry.invalid/private-enterprise-runtime:19.0-runtime"
DEFAULT_ODOO_BASE_DEVTOOLS_IMAGE = "registry.invalid/private-enterprise-devtools:19.0-devtools"
CONTROL_PLANE_ROOT_ENV_VAR = "ODOO_CONTROL_PLANE_ROOT"

_REGISTRY_LOGINS_DONE: set[tuple[str, str, str]] = set()
_VERIFIED_IMAGE_ACCESS: set[str] = set()


@dataclass(frozen=True)
class OdooOverrideDefinition:
    config_parameters: ScalarMap
    addon_settings: dict[str, ScalarMap]


@dataclass(frozen=True)
class InstanceDefinition:
    database: str | None
    install_modules_add: tuple[str, ...]
    runtime_env: ScalarMap
    odoo_overrides: OdooOverrideDefinition


@dataclass(frozen=True)
class ContextDefinition:
    database: str | None
    install_modules: tuple[str, ...]
    runtime_env: ScalarMap
    odoo_overrides: OdooOverrideDefinition
    update_modules: str
    instances: dict[str, InstanceDefinition]


@dataclass(frozen=True)
class StackDefinition:
    schema_version: int
    odoo_version: str
    state_root: str
    addons_path: tuple[str, ...]
    runtime_env: ScalarMap
    odoo_overrides: OdooOverrideDefinition
    required_env_keys: tuple[str, ...]
    contexts: dict[str, ContextDefinition]


@dataclass(frozen=True)
class LoadedEnvironment:
    env_file_path: Path
    merged_values: dict[str, str]
    collisions: tuple[object, ...]


@dataclass(frozen=True)
class LoadedStack:
    stack_file_path: Path
    stack_definition: StackDefinition


@dataclass(frozen=True)
class RuntimeSelection:
    context_name: str
    instance_name: str
    context_definition: ContextDefinition
    instance_definition: InstanceDefinition
    database_name: str
    project_name: str
    state_path: Path
    runtime_conf_host_path: Path
    data_volume_name: str
    log_volume_name: str
    db_volume_name: str
    web_host_port: int
    longpoll_host_port: int
    db_host_port: int
    runtime_odoo_conf_path: str
    effective_install_modules: tuple[str, ...]
    effective_source_repositories: tuple[str, ...]
    effective_source_selectors: tuple[str, ...]
    effective_runtime_env: dict[str, str]
    effective_odoo_overrides: OdooOverrideDefinition


@dataclass(frozen=True)
class RuntimeContext:
    manifest: WorkspaceManifest
    repo_root: Path
    stack: LoadedStack
    artifact_inputs_definition: ArtifactInputsDefinition | None
    environment: LoadedEnvironment
    selection: RuntimeSelection
    runtime_env_file: Path


@dataclass(frozen=True)
class RuntimeSelectResult:
    runtime_env_file: Path
    pycharm_odoo_conf_file: Path


@dataclass(frozen=True)
class RuntimeInspectResult:
    payload: dict[str, object]


@dataclass(frozen=True)
class RuntimeArtifactPublishResult:
    manifest_payload: dict[str, object]
    output_file: Path | None


class RuntimeCommandError(ValueError):
    pass


def select_runtime(*, manifest: WorkspaceManifest, runtime_repo_path: Path) -> RuntimeSelectResult:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    pycharm_host_addons_paths = resolve_manifest_pycharm_addons_paths(manifest=manifest)
    write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    write_runtime_env_file(runtime_context=runtime_context)
    pycharm_odoo_conf_file = write_pycharm_odoo_conf(
        repo_root=runtime_repo_path,
        context_name=runtime_context.selection.context_name,
        instance_name=runtime_context.selection.instance_name,
        database_name=runtime_context.selection.database_name,
        db_host_port=runtime_context.selection.db_host_port,
        state_path=runtime_context.selection.state_path,
        addons_paths=runtime_context.stack.stack_definition.addons_path,
        source_environment=runtime_context.environment.merged_values,
        host_addons_paths=pycharm_host_addons_paths,
    )
    return RuntimeSelectResult(
        runtime_env_file=runtime_context.runtime_env_file,
        pycharm_odoo_conf_file=pycharm_odoo_conf_file,
    )


def inspect_runtime(*, manifest: WorkspaceManifest, runtime_repo_path: Path) -> RuntimeInspectResult:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    pycharm_host_addons_paths = resolve_manifest_pycharm_addons_paths(manifest=manifest)
    local_addons_mount_paths = resolve_manifest_local_addons_mount_paths(manifest=manifest)
    runtime_conf_file = write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    pycharm_odoo_conf_file = write_pycharm_odoo_conf(
        repo_root=runtime_repo_path,
        context_name=runtime_context.selection.context_name,
        instance_name=runtime_context.selection.instance_name,
        database_name=runtime_context.selection.database_name,
        db_host_port=runtime_context.selection.db_host_port,
        state_path=runtime_context.selection.state_path,
        addons_paths=runtime_context.stack.stack_definition.addons_path,
        source_environment=runtime_context.environment.merged_values,
        host_addons_paths=pycharm_host_addons_paths,
    )
    payload: dict[str, object] = {
        "context": runtime_context.selection.context_name,
        "instance": runtime_context.selection.instance_name,
        "database": runtime_context.selection.database_name,
        "odoo_conf_host": str(runtime_conf_file),
        "pycharm_odoo_conf_host": str(pycharm_odoo_conf_file),
        "odoo_conf_container": runtime_context.selection.runtime_odoo_conf_path,
        "addons_path": list(runtime_context.stack.stack_definition.addons_path),
        "pycharm_addons_path": list(pycharm_host_addons_paths),
        "project_addons_host_path": str(local_addons_mount_paths.project_addons_host_path),
        "shared_addons_host_path": (
            str(local_addons_mount_paths.shared_addons_host_path)
            if local_addons_mount_paths.shared_addons_host_path is not None
            else ""
        ),
        "addon_repositories": list(runtime_context.selection.effective_source_repositories),
        "addon_repository_selectors": list(runtime_context.selection.effective_source_selectors),
        "install_modules": list(runtime_context.selection.effective_install_modules),
        "note": "Use pycharm_odoo_conf_host for run configs/tooling with explicit -c config paths; odoo_conf_host is for runtime bootstrap.",
    }
    return RuntimeInspectResult(payload=payload)


def up_runtime(*, manifest: WorkspaceManifest, runtime_repo_path: Path, build_images: bool) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    runtime_env_file = write_runtime_env_file(runtime_context=runtime_context)
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    if build_images:
        build_runtime(manifest=manifest, runtime_repo_path=runtime_repo_path, no_cache=False)
    run_command(runtime_repo_path=runtime_repo_path, command=compose_command + ["up", "-d", "--no-build"])


def build_runtime(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_cache: bool) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    runtime_env_file = write_runtime_env_file(runtime_context=runtime_context)
    ensure_registry_auth_for_base_images(
        build_registry_auth_environment(
            source_environment=runtime_context.environment.merged_values,
            runtime_env_file=runtime_env_file,
        )
    )
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    build_command = compose_command + ["build"]
    if no_cache:
        build_command.append("--no-cache")
    run_command(runtime_repo_path=runtime_repo_path, command=build_command)


def publish_runtime_artifact(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    image_repository: str,
    image_tag: str,
    output_file: Path | None,
    no_cache: bool,
    platforms: tuple[str, ...] = DEFAULT_ARTIFACT_IMAGE_PLATFORMS,
) -> RuntimeArtifactPublishResult:
    normalized_image_repository = image_repository.strip()
    normalized_image_tag = image_tag.strip()
    if not normalized_image_repository:
        raise RuntimeCommandError("Artifact publish requires a non-empty image repository.")
    if not normalized_image_tag:
        raise RuntimeCommandError("Artifact publish requires a non-empty image tag.")
    normalized_platforms = tuple(platform.strip() for platform in platforms if platform.strip())
    if not normalized_platforms:
        raise RuntimeCommandError("Artifact publish requires at least one target platform.")

    runtime_context = load_runtime_context(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        require_local_instance=False,
    )
    runtime_values = build_runtime_env_values(
        runtime_context=runtime_context,
        build_target_override="production",
        image_repository_override=normalized_image_repository,
        image_tag_override=normalized_image_tag,
        include_selection_sources=False,
    )
    runtime_values = apply_publish_artifact_input_manifest(
        runtime_context=runtime_context,
        runtime_values=runtime_values,
    )
    runtime_values, artifact_source_selectors = resolve_artifact_runtime_source_repository_refs(
        runtime_values=runtime_values,
    )
    ensure_registry_auth_for_base_images(runtime_values)
    ensure_registry_auth_for_image_push(
        environment_values=runtime_values,
        image_repository=normalized_image_repository,
    )

    build_environment = command_execution_env()
    github_token = resolve_github_token_for_build(runtime_values)
    if github_token is not None:
        build_environment["GITHUB_TOKEN"] = github_token

    artifact_image_digest: str | None = None
    with tempfile.TemporaryDirectory(prefix="odoo-artifact-") as temporary_directory_name:
        staged_context_root = Path(temporary_directory_name)
        build_metadata_file = staged_context_root / "build-metadata.json"
        stage_artifact_build_context(
            manifest=manifest,
            runtime_repo_path=runtime_repo_path,
            staged_context_root=staged_context_root,
        )
        build_command = [
            "docker",
            "buildx",
            "build",
            "--file",
            str(staged_context_root / "docker" / "Dockerfile"),
            "--target",
            "production",
            "--platform",
            ",".join(normalized_platforms),
            "--tag",
            f"{normalized_image_repository}:{normalized_image_tag}",
            "--metadata-file",
            str(build_metadata_file),
            "--push",
        ]
        if github_token is not None:
            build_command.extend(["--secret", "id=github_token,env=GITHUB_TOKEN"])
        if no_cache:
            build_command.append("--no-cache")
        for build_argument_name in (
            "ODOO_VERSION",
            "ODOO_BASE_RUNTIME_IMAGE",
            "ODOO_BASE_DEVTOOLS_IMAGE",
            "ODOO_ADDON_REPOSITORIES",
            "OPENUPGRADE_ADDON_REPOSITORY",
            "OPENUPGRADELIB_INSTALL_SPEC",
            "ODOO_PYTHON_SYNC_SKIP_ADDONS",
        ):
            build_command.extend(["--build-arg", f"{build_argument_name}={runtime_values.get(build_argument_name, '')}"])
        build_command.append(str(staged_context_root))
        run_command(
            runtime_repo_path=staged_context_root,
            command=build_command,
            environment_overrides=build_environment,
        )
        artifact_image_digest = resolve_buildx_metadata_image_digest(build_metadata_file)
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if tenant_repo_path is None or not tenant_repo_path.exists():
        raise RuntimeCommandError("Tenant repo path must exist before publishing an artifact.")
    tenant_commit = require_clean_git_commit(
        repo_path=tenant_repo_path.resolve(),
        label=manifest.tenant_repo.name,
    )
    runtime_commit = require_clean_git_commit(
        repo_path=runtime_repo_path.resolve(),
        label=(manifest.runtime_repo.name if manifest.runtime_repo is not None else runtime_repo_path.name),
    )
    artifact_source_entries = collect_artifact_source_entries(
        manifest=manifest,
        runtime_values=runtime_values,
    )
    base_runtime_image, _ = resolve_base_images_for_build(runtime_values)
    manifest_payload = build_runtime_artifact_manifest_payload(
        context_name=runtime_context.selection.context_name,
        source_commit=tenant_commit,
        runtime_repo_name=(manifest.runtime_repo.name if manifest.runtime_repo is not None else runtime_repo_path.name),
        runtime_repo_commit=runtime_commit,
        artifact_source_entries=artifact_source_entries,
        source_selector_entries=artifact_source_selectors,
        openupgrade_addon_repository=runtime_values.get("OPENUPGRADE_ADDON_REPOSITORY", ""),
        openupgradelib_install_spec=runtime_values.get("OPENUPGRADELIB_INSTALL_SPEC", ""),
        addon_skip_flags=parse_csv_values(runtime_values.get("ODOO_PYTHON_SYNC_SKIP_ADDONS", "")),
        image_repository=normalized_image_repository,
        image_tag=normalized_image_tag,
        image_digest=artifact_image_digest,
        enterprise_base_digest=resolve_image_digest(base_runtime_image),
        odoo_version=runtime_values.get("ODOO_VERSION", ""),
    )

    normalized_output_file = None if output_file is None else output_file.expanduser().resolve()
    if normalized_output_file is not None:
        normalized_output_file.parent.mkdir(parents=True, exist_ok=True)
        normalized_output_file.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return RuntimeArtifactPublishResult(
        manifest_payload=manifest_payload,
        output_file=normalized_output_file,
    )


def down_runtime(*, manifest: WorkspaceManifest, runtime_repo_path: Path, volumes: bool) -> None:
    load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    down_command = compose_command + ["down"]
    if volumes:
        down_command.append("--volumes")
    run_command(runtime_repo_path=runtime_repo_path, command=down_command)


def run_init_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    install_modules = ",".join(runtime_context.selection.effective_install_modules)
    addons_path_argument = ",".join(runtime_context.stack.stack_definition.addons_path)
    init_command = [
        "/odoo/odoo-bin",
        "-d",
        runtime_context.selection.database_name,
        f"--addons-path={addons_path_argument}",
        "--data-dir=/volumes/data",
        "-i",
        install_modules,
        "--db_host=database",
        "--db_port=5432",
        f"--db_user={runtime_context.environment.merged_values.get('ODOO_DB_USER', 'odoo')}",
        f"--db_password={runtime_context.environment.merged_values.get('ODOO_DB_PASSWORD', '')}",
        "--stop-after-init",
    ]

    def run_init_operation() -> None:
        compose_up_script_runner(
            runtime_repo_path=runtime_repo_path,
            runtime_env_file=runtime_env_file,
        )
        compose_exec(
            runtime_repo_path=runtime_repo_path,
            runtime_env_file=runtime_env_file,
            container_service="script-runner",
            container_command=init_command,
        )
        apply_admin_password_if_configured(
            runtime_repo_path=runtime_repo_path,
            runtime_env_file=runtime_env_file,
            runtime_selection=runtime_context.selection,
            stack_definition=runtime_context.stack.stack_definition,
            loaded_environment=runtime_context.environment.merged_values,
        )
        assert_active_admin_password_is_not_default(
            runtime_repo_path=runtime_repo_path,
            runtime_env_file=runtime_env_file,
            runtime_selection=runtime_context.selection,
            stack_definition=runtime_context.stack.stack_definition,
            loaded_environment=runtime_context.environment.merged_values,
        )

    run_with_web_temporarily_stopped(
        runtime_repo_path=runtime_repo_path,
        runtime_env_file=runtime_env_file,
        operation=run_init_operation,
    )


def run_openupgrade_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path) -> None:
    load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    up_script_runner_command = compose_command + ["up", "-d", "script-runner"]
    stop_web_command = compose_command + ["stop", "web"]
    openupgrade_exec_command = compose_command + [
        "exec",
        "-T",
        "script-runner",
        "python3",
        "/volumes/scripts/run_openupgrade.py",
    ]
    up_web_command = compose_command + ["up", "-d", "web"]
    run_command_best_effort(runtime_repo_path=runtime_repo_path, command=stop_web_command)
    try:
        run_command(runtime_repo_path=runtime_repo_path, command=up_script_runner_command)
        run_command(runtime_repo_path=runtime_repo_path, command=openupgrade_exec_command)
    finally:
        run_command_best_effort(runtime_repo_path=runtime_repo_path, command=up_web_command)


def run_restore_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_local_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=False,
        no_sanitize=no_sanitize,
        update_only=False,
    )


def run_bootstrap_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_local_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=True,
        no_sanitize=no_sanitize,
        update_only=False,
    )


def run_update_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_local_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=False,
        no_sanitize=no_sanitize,
        update_only=True,
    )


def run_local_data_workflow(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    bootstrap: bool,
    no_sanitize: bool,
    update_only: bool,
) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    runtime_env_file = write_runtime_env_file(runtime_context=runtime_context)
    data_workflow_environment = resolve_data_workflow_environment(parse_env_file(runtime_env_file))
    if not bootstrap and not update_only:
        missing_environment_keys = missing_upstream_source_keys(data_workflow_environment)
        if missing_environment_keys:
            missing_joined = ", ".join(missing_environment_keys)
            raise RuntimeCommandError(
                "Restore requires upstream settings; missing: "
                f"{missing_joined}. {runtime_environment_configuration_guidance()} "
                "or run bootstrap intentionally."
            )

    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    run_command(runtime_repo_path=runtime_repo_path, command=compose_command + ["build", "web"])

    database_up_command = compose_command + ["up", "-d", "--remove-orphans", "database"]
    script_runner_up_command = compose_command + ["up", "-d", "--remove-orphans", "script-runner"]
    stop_web_command = compose_command + ["stop", "web"]
    up_web_command = compose_command + ["up", "-d", "--remove-orphans", "web"]

    run_command_best_effort(runtime_repo_path=runtime_repo_path, command=database_up_command)
    wait_for_compose_service(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file, service_name="database")

    run_command_best_effort(runtime_repo_path=runtime_repo_path, command=script_runner_up_command)
    wait_for_compose_service(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file, service_name="script-runner")

    normalize_local_filestore_permissions(
        runtime_repo_path=runtime_repo_path,
        runtime_env_file=runtime_env_file,
        data_workflow_environment=data_workflow_environment,
    )

    data_workflow_exec_environment = command_execution_env()
    data_workflow_exec_environment.update(data_workflow_script_environment(data_workflow_environment))
    data_workflow_command = compose_command + build_data_workflow_exec_args(
        data_workflow_environment=data_workflow_environment,
        bootstrap=bootstrap,
        no_sanitize=no_sanitize,
        update_only=update_only,
    )

    run_command_best_effort(runtime_repo_path=runtime_repo_path, command=stop_web_command)
    try:
        run_command(
            runtime_repo_path=runtime_repo_path,
            command=data_workflow_command,
            environment_overrides=data_workflow_exec_environment,
            allowed_return_codes={0, 10},
        )
    finally:
        run_command_best_effort(runtime_repo_path=runtime_repo_path, command=up_web_command)


def load_runtime_context(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    require_local_instance: bool = True,
) -> RuntimeContext:
    if require_local_instance:
        assert_local_instance(instance_name=manifest.runtime.instance, operation_name="platform runtime")
    stack_file_path = resolve_stack_file_path(runtime_repo_path)
    loaded_stack = load_stack(stack_file_path)
    loaded_environment = load_environment(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    try:
        artifact_inputs_definition = load_artifact_inputs_definition(manifest=manifest)
    except ArtifactInputsError as error:
        raise RuntimeCommandError(str(error)) from error
    effective_stack_definition = resolve_manifest_runtime_stack_definition(
        manifest=manifest,
        stack_definition=loaded_stack.stack_definition,
    )
    runtime_selection = resolve_runtime_selection(
        stack_definition=effective_stack_definition,
        artifact_inputs_definition=artifact_inputs_definition,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
        repo_root=runtime_repo_path,
    )
    runtime_env_file = runtime_env_file_for_scope(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    return RuntimeContext(
        manifest=manifest,
        repo_root=runtime_repo_path,
        stack=LoadedStack(stack_file_path=loaded_stack.stack_file_path, stack_definition=effective_stack_definition),
        artifact_inputs_definition=artifact_inputs_definition,
        environment=loaded_environment,
        selection=runtime_selection,
        runtime_env_file=runtime_env_file,
    )


def resolve_manifest_runtime_stack_definition(*, manifest: WorkspaceManifest, stack_definition: StackDefinition) -> StackDefinition:
    project_addons_paths = resolve_manifest_container_addons_paths(manifest=manifest)
    if not project_addons_paths:
        return stack_definition
    effective_addons_paths: list[str] = []
    seen_paths: set[str] = set()
    for addons_path in stack_definition.addons_path:
        if addons_path.startswith("/opt/project/addons"):
            continue
        if addons_path in seen_paths:
            continue
        seen_paths.add(addons_path)
        effective_addons_paths.append(addons_path)
    for addons_path in project_addons_paths:
        if addons_path in seen_paths:
            continue
        seen_paths.add(addons_path)
        effective_addons_paths.append(addons_path)
    return StackDefinition(
        schema_version=stack_definition.schema_version,
        odoo_version=stack_definition.odoo_version,
        state_root=stack_definition.state_root,
        addons_path=tuple(effective_addons_paths),
        runtime_env=stack_definition.runtime_env,
        odoo_overrides=stack_definition.odoo_overrides,
        required_env_keys=stack_definition.required_env_keys,
        contexts=stack_definition.contexts,
    )


def resolve_manifest_container_addons_paths(*, manifest: WorkspaceManifest) -> tuple[str, ...]:
    resolved_paths: list[str] = []
    seen_paths: set[str] = set()
    for manifest_addons_path in manifest.runtime.addons_paths:
        container_path = _resolve_manifest_container_addons_path(manifest_addons_path)
        if container_path in seen_paths:
            continue
        seen_paths.add(container_path)
        resolved_paths.append(container_path)
    return tuple(resolved_paths)


def _resolve_manifest_container_addons_path(manifest_addons_path: str) -> str:
    raw_path = manifest_addons_path.strip()
    if raw_path == "sources/tenant/addons":
        return "/opt/project/addons"
    if raw_path.startswith("sources/tenant/addons/"):
        return "/opt/project/addons/" + raw_path.removeprefix("sources/tenant/addons/")
    if raw_path == "sources/shared-addons":
        return "/opt/project/addons/shared"
    if raw_path.startswith("sources/shared-addons/"):
        return "/opt/project/addons/shared/" + raw_path.removeprefix("sources/shared-addons/")
    if raw_path == "sources/runtime":
        return "/opt/project/runtime"
    if raw_path.startswith("sources/runtime/"):
        return "/opt/project/runtime/" + raw_path.removeprefix("sources/runtime/")
    return raw_path


def resolve_manifest_pycharm_addons_paths(*, manifest: WorkspaceManifest) -> tuple[str, ...]:
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if tenant_repo_path is None or not tenant_repo_path.exists():
        raise RuntimeCommandError("Tenant repo path must exist before generating PyCharm Odoo config.")
    shared_addons_repo_path = resolve_optional_repo_path_with_managed_checkout(
        manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "shared-addons",
    )
    runtime_repo_path = resolve_optional_repo_path_with_managed_checkout(
        manifest.runtime_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "runtime",
    )
    resolved_paths = [
        _resolve_manifest_addons_path(
            manifest_addons_path=addons_path,
            workspace_path=workspace_path,
            tenant_repo_path=tenant_repo_path.resolve(),
            shared_addons_repo_path=shared_addons_repo_path,
            runtime_repo_path=runtime_repo_path,
        )
        for addons_path in manifest.runtime.addons_paths
    ]
    return tuple(str(path) for path in resolved_paths)


def _resolve_manifest_addons_path(
    *,
    manifest_addons_path: str,
    workspace_path: Path,
    tenant_repo_path: Path,
    shared_addons_repo_path: Path | None,
    runtime_repo_path: Path | None,
) -> Path:
    raw_path = manifest_addons_path.strip()
    candidate_path = Path(raw_path).expanduser()
    if candidate_path.is_absolute():
        return candidate_path.resolve()
    if raw_path == "sources/tenant":
        return tenant_repo_path
    if raw_path.startswith("sources/tenant/"):
        return (tenant_repo_path / raw_path.removeprefix("sources/tenant/")).resolve()
    if raw_path == "sources/shared-addons":
        if shared_addons_repo_path is None:
            raise RuntimeCommandError(
                "Workspace manifest references sources/shared-addons, but that repo is not available. Run `platform workspace sync` first when using repo-addressable shared addons."
            )
        return shared_addons_repo_path.resolve()
    if raw_path.startswith("sources/shared-addons/"):
        if shared_addons_repo_path is None:
            raise RuntimeCommandError(
                "Workspace manifest references sources/shared-addons, but that repo is not available. Run `platform workspace sync` first when using repo-addressable shared addons."
            )
        return (shared_addons_repo_path / raw_path.removeprefix("sources/shared-addons/")).resolve()
    if raw_path == "sources/runtime":
        if runtime_repo_path is None:
            raise RuntimeCommandError(
                "Workspace manifest references sources/runtime, but that repo is not available. Run `platform workspace sync` first when using repo-addressable runtime sources."
            )
        return runtime_repo_path.resolve()
    if raw_path.startswith("sources/runtime/"):
        if runtime_repo_path is None:
            raise RuntimeCommandError(
                "Workspace manifest references sources/runtime, but that repo is not available. Run `platform workspace sync` first when using repo-addressable runtime sources."
            )
        return (runtime_repo_path / raw_path.removeprefix("sources/runtime/")).resolve()
    return (workspace_path / raw_path).resolve()


def resolve_stack_file_path(runtime_repo_path: Path) -> Path:
    stack_file_path = runtime_repo_path / "platform" / "stack.toml"
    if not stack_file_path.exists():
        raise RuntimeCommandError(f"Stack file not found: {stack_file_path}")
    return stack_file_path


def assert_local_instance(*, instance_name: str, operation_name: str) -> None:
    if instance_name == "local":
        return
    raise RuntimeCommandError(
        f"{operation_name} manages local host runtime only and requires --instance local. "
        "Use control-plane ship/promote workflows for release actions, or the "
        "explicit Dokploy-managed data workflows (`platform runtime restore` "
        "or `platform runtime workflow --workflow bootstrap|update`) when you "
        "need a remote data operation."
    )


def load_stack(stack_file_path: Path) -> LoadedStack:
    try:
        payload = tomllib.loads(stack_file_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeCommandError(f"Invalid stack file {stack_file_path}: {error}") from error
    stack_definition = parse_stack_definition(payload, stack_file_path=stack_file_path)
    return LoadedStack(stack_file_path=stack_file_path, stack_definition=stack_definition)


def parse_stack_definition(payload: dict[str, object], *, stack_file_path: Path) -> StackDefinition:
    schema_version = _read_required_int(payload, "schema_version")
    if schema_version != 1:
        raise RuntimeCommandError(f"Unsupported stack schema_version: {schema_version}")
    _ensure_legacy_addon_source_keys_absent(payload, scope="stack", replacement="artifact-inputs.toml")
    contexts_table = _read_required_table(payload, "contexts", scope="stack")
    contexts: dict[str, ContextDefinition] = {}
    for context_name, raw_context in contexts_table.items():
        contexts[context_name] = _parse_context_definition(context_name, raw_context)
    stack_definition = StackDefinition(
        schema_version=schema_version,
        odoo_version=_read_required_string(payload, "odoo_version", scope="stack"),
        state_root=_read_optional_string(payload, "state_root", scope="stack") or "",
        addons_path=_read_string_tuple(payload, "addons_path", scope="stack"),
        runtime_env=_read_optional_scalar_map(payload, "runtime_env", scope="stack"),
        odoo_overrides=_read_optional_odoo_override_definition(payload, scope="stack"),
        required_env_keys=_read_optional_string_tuple(payload, "required_env_keys", scope="stack"),
        contexts=contexts,
    )
    return expand_project_addons_paths(stack_definition=stack_definition, stack_file_path=stack_file_path)


def _parse_context_definition(context_name: str, raw_context: object) -> ContextDefinition:
    context_table = _ensure_table(raw_context, scope=f"contexts.{context_name}")
    _ensure_legacy_addon_source_keys_absent(
        context_table,
        scope=f"contexts.{context_name}",
        replacement="artifact-inputs.toml contexts",
    )
    instances_table = _read_required_table(context_table, "instances", scope=f"contexts.{context_name}")
    instances: dict[str, InstanceDefinition] = {}
    for instance_name, raw_instance in instances_table.items():
        instances[instance_name] = _parse_instance_definition(
            context_name=context_name, instance_name=instance_name, raw_instance=raw_instance
        )
    return ContextDefinition(
        database=_read_optional_string(context_table, "database", scope=f"contexts.{context_name}"),
        install_modules=_read_optional_string_tuple(context_table, "install_modules", scope=f"contexts.{context_name}"),
        runtime_env=_read_optional_scalar_map(context_table, "runtime_env", scope=f"contexts.{context_name}"),
        odoo_overrides=_read_optional_odoo_override_definition(context_table, scope=f"contexts.{context_name}"),
        update_modules=_read_optional_string(context_table, "update_modules", scope=f"contexts.{context_name}") or "AUTO",
        instances=instances,
    )


def _parse_instance_definition(*, context_name: str, instance_name: str, raw_instance: object) -> InstanceDefinition:
    instance_table = _ensure_table(raw_instance, scope=f"contexts.{context_name}.instances.{instance_name}")
    _ensure_legacy_addon_source_keys_absent(
        instance_table,
        scope=f"contexts.{context_name}.instances.{instance_name}",
        replacement="artifact-inputs.toml instance overrides",
    )
    return InstanceDefinition(
        database=_read_optional_string(instance_table, "database", scope=f"contexts.{context_name}.instances.{instance_name}"),
        install_modules_add=_read_optional_string_tuple(
            instance_table,
            "install_modules_add",
            scope=f"contexts.{context_name}.instances.{instance_name}",
        ),
        runtime_env=_read_optional_scalar_map(
            instance_table,
            "runtime_env",
            scope=f"contexts.{context_name}.instances.{instance_name}",
        ),
        odoo_overrides=_read_optional_odoo_override_definition(
            instance_table,
            scope=f"contexts.{context_name}.instances.{instance_name}",
        ),
    )


def expand_project_addons_paths(*, stack_definition: StackDefinition, stack_file_path: Path) -> StackDefinition:
    repo_root = stack_file_path.parent.parent
    grouped_paths = discover_project_addon_group_paths(repo_root)
    if not grouped_paths:
        return stack_definition
    expanded_paths: list[str] = []
    seen_paths: set[str] = set()
    for addons_path in stack_definition.addons_path:
        if addons_path not in seen_paths:
            seen_paths.add(addons_path)
            expanded_paths.append(addons_path)
        if addons_path == "/opt/project/addons":
            for grouped_path in grouped_paths:
                if grouped_path in seen_paths:
                    continue
                seen_paths.add(grouped_path)
                expanded_paths.append(grouped_path)
    return StackDefinition(
        schema_version=stack_definition.schema_version,
        odoo_version=stack_definition.odoo_version,
        state_root=stack_definition.state_root,
        addons_path=tuple(expanded_paths),
        runtime_env=stack_definition.runtime_env,
        odoo_overrides=stack_definition.odoo_overrides,
        required_env_keys=stack_definition.required_env_keys,
        contexts=stack_definition.contexts,
    )


def _ensure_legacy_addon_source_keys_absent(
    source: dict[str, object],
    *,
    scope: str,
    replacement: str,
) -> None:
    legacy_keys = (
        "addon_repositories",
        "addon_repository_selectors",
        "addon_repositories_add",
        "addon_repository_selectors_add",
    )
    configured_keys = [key for key in legacy_keys if key in source]
    if not configured_keys:
        return
    formatted_keys = ", ".join(configured_keys)
    raise RuntimeCommandError(
        f"Legacy addon source keys are no longer supported in {scope}: {formatted_keys}. Move addon source selection into {replacement}."
    )


def empty_odoo_override_definition() -> OdooOverrideDefinition:
    return OdooOverrideDefinition(config_parameters={}, addon_settings={})


def merge_odoo_override_definitions(
    *odoo_override_sources: OdooOverrideDefinition,
) -> OdooOverrideDefinition:
    config_parameters: ScalarMap = {}
    addon_settings: dict[str, ScalarMap] = {}
    for override_source in odoo_override_sources:
        config_parameters.update(override_source.config_parameters)
        for addon_name, setting_values in override_source.addon_settings.items():
            addon_settings.setdefault(addon_name, {}).update(setting_values)
    return OdooOverrideDefinition(
        config_parameters=config_parameters,
        addon_settings=addon_settings,
    )


def discover_project_addon_group_paths(repo_root: Path) -> tuple[str, ...]:
    addons_root = repo_root / "addons"
    if not addons_root.is_dir():
        return ()
    grouped_paths: list[str] = []
    for child_path in sorted(addons_root.iterdir()):
        if not child_path.is_dir():
            continue
        if child_path.name.startswith((".", "__")):
            continue
        if (child_path / "__manifest__.py").exists() or (child_path / "__openerp__.py").exists():
            continue
        grouped_paths.append(f"/opt/project/addons/{child_path.name}")
    return tuple(grouped_paths)


def load_environment(*, repo_root: Path, context_name: str, instance_name: str, collision_mode: str = "warn") -> LoadedEnvironment:
    _ = collision_mode
    explicit_environment = os.environ.get(RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, "").strip()
    if explicit_environment:
        ensure_legacy_local_environment_files_are_absent(repo_root)
        return load_environment_from_explicit_payload(
            raw_payload=explicit_environment,
            context_name=context_name,
            instance_name=instance_name,
        )
    control_plane_root = resolve_control_plane_root()
    if control_plane_root is None:
        legacy_file_display = legacy_local_environment_file_display(repo_root)
        if legacy_file_display is not None:
            raise RuntimeCommandError(
                "Legacy devkit-local env/secrets files are no longer supported for runtime environment authority: "
                f"{legacy_file_display}. Remove them, set {CONTROL_PLANE_ROOT_ENV_VAR}, and migrate runtime values into "
                "harbor `config/runtime-environments.toml`."
            )
        raise RuntimeCommandError(
            "Runtime environment resolution now requires the control-plane contract. "
            f"Set {CONTROL_PLANE_ROOT_ENV_VAR} to a valid harbor checkout and configure runtime values in "
            "`config/runtime-environments.toml`."
        )
    ensure_legacy_local_environment_files_are_absent(repo_root)
    return load_environment_from_control_plane(
        control_plane_root=control_plane_root,
        context_name=context_name,
        instance_name=instance_name,
    )


def load_environment_from_explicit_payload(
    *,
    raw_payload: str,
    context_name: str,
    instance_name: str,
) -> LoadedEnvironment:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise RuntimeCommandError(f"{RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR} must contain a JSON object.") from error
    if not isinstance(payload, dict):
        raise RuntimeCommandError(f"{RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR} must contain a JSON object.")
    payload_context = clean_optional_value(str(payload.get("context", "")))
    payload_instance = clean_optional_value(str(payload.get("instance", "")))
    if payload_context != context_name or payload_instance != instance_name:
        raise RuntimeCommandError(
            f"{RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR} context/instance does not match the selected runtime. "
            f"Payload={payload_context}/{payload_instance} selected={context_name}/{instance_name}."
        )
    raw_environment = payload.get("environment")
    if not isinstance(raw_environment, dict):
        raise RuntimeCommandError(f"{RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR} must include an environment object.")
    resolved_environment = {
        environment_key: str(environment_value)
        for environment_key, environment_value in raw_environment.items()
        if isinstance(environment_key, str)
    }
    if not resolved_environment:
        raise RuntimeCommandError(f"{RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR} environment object must not be empty.")
    synthetic_env_file = Path(".generated") / "runtime-env" / f"{context_name}.{instance_name}.env"
    return LoadedEnvironment(
        env_file_path=synthetic_env_file,
        merged_values=resolved_environment,
        collisions=(),
    )


def resolve_control_plane_root() -> Path | None:
    configured_root = os.environ.get(CONTROL_PLANE_ROOT_ENV_VAR, "").strip()
    if not configured_root:
        return None
    return Path(configured_root).expanduser().resolve()


def runtime_environment_configuration_guidance(*, noun: str = "these") -> str:
    if resolve_control_plane_root() is None:
        return (
            f"Set {CONTROL_PLANE_ROOT_ENV_VAR} to a valid harbor checkout and configure {noun} in "
            "`config/runtime-environments.toml`."
        )
    return (
        f"Configure {noun} in the control-plane runtime environments file resolved through "
        f"{CONTROL_PLANE_ROOT_ENV_VAR} (`config/runtime-environments.toml` by default)."
    )


def legacy_local_environment_files(repo_root: Path) -> list[Path]:
    legacy_files = [
        repo_root / ".env",
        repo_root / "platform" / ".env",
        repo_root / "platform" / "secrets.toml",
    ]
    return [legacy_file for legacy_file in legacy_files if legacy_file.exists()]


def legacy_local_environment_file_display(repo_root: Path) -> str | None:
    existing_legacy_files = legacy_local_environment_files(repo_root)
    if not existing_legacy_files:
        return None
    return ", ".join(str(legacy_file) for legacy_file in existing_legacy_files)


def ensure_legacy_local_environment_files_are_absent(repo_root: Path) -> None:
    legacy_file_display = legacy_local_environment_file_display(repo_root)
    if legacy_file_display is None:
        return
    raise RuntimeCommandError(
        "Local runtime environment authority is configured to come from the control plane via "
        f"{CONTROL_PLANE_ROOT_ENV_VAR}, but legacy devkit-local env/secrets files still exist: {legacy_file_display}. "
        "Remove or migrate those files before continuing so environment authority stays single-source and fail-closed."
    )


def load_environment_from_control_plane(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
) -> LoadedEnvironment:
    control_plane_command = [
        "uv",
        "--directory",
        str(control_plane_root),
        "run",
        "launchplane",
        "environments",
        "resolve",
        "--context",
        context_name,
        "--instance",
        instance_name,
        "--json-output",
    ]
    result = subprocess.run(control_plane_command, capture_output=True, text=True, env=command_execution_env())
    if result.returncode != 0:
        details = clean_optional_value(result.stderr) or clean_optional_value(result.stdout)
        raise RuntimeCommandError(
            "Unable to resolve runtime environment from control plane. "
            f"Ensure {CONTROL_PLANE_ROOT_ENV_VAR} points at a valid harbor checkout."
            + (f"\nControl plane reported: {details}" if details else "")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeCommandError(
            "Control plane returned invalid runtime environment payload while resolving local runtime config."
        ) from error
    raw_environment = payload.get("environment")
    if not isinstance(raw_environment, dict):
        raise RuntimeCommandError("Control plane runtime environment payload did not include an environment object.")
    resolved_environment = {
        environment_key: str(environment_value)
        for environment_key, environment_value in raw_environment.items()
        if isinstance(environment_key, str)
    }
    synthetic_env_file = control_plane_root / ".generated" / "runtime-env" / f"{context_name}.{instance_name}.env"
    return LoadedEnvironment(
        env_file_path=synthetic_env_file,
        merged_values=resolved_environment,
        collisions=(),
    )


def parse_env_file(env_file_path: Path) -> dict[str, str]:
    parsed_values: dict[str, str] = {}
    for raw_line in env_file_path.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[len("export ") :].strip()
        if "=" not in stripped_line:
            continue
        key_part, value_part = stripped_line.split("=", 1)
        environment_key = key_part.strip()
        environment_value = value_part.strip()
        is_quoted_value = (
            len(environment_value) >= 2 and environment_value[0] == environment_value[-1] and environment_value[0] in {'"', "'"}
        )
        if is_quoted_value:
            environment_value = environment_value[1:-1]
        elif " #" in environment_value:
            environment_value = environment_value.split(" #", 1)[0].rstrip()
        parsed_values[environment_key] = environment_value
    return parsed_values


def resolve_data_workflow_environment(raw_values: dict[str, str]) -> dict[str, str]:
    variable_pattern = re.compile(r"\$\{([^}]+)}")
    resolved_cache: dict[str, str] = {}

    def resolve_expression(expression: str, resolving_names: set[str]) -> str:
        variable_name, default_value = expression, ""
        if ":-" in expression:
            variable_name, default_value = (part.strip() for part in expression.split(":-", 1))
        cached_value = resolved_cache.get(variable_name)
        if cached_value is not None:
            return cached_value
        if variable_name in raw_values:
            return resolve_value(variable_name, resolving_names)
        return os.environ.get(variable_name, default_value)

    def resolve_value(variable_name: str, resolving_names: set[str]) -> str:
        cached_value = resolved_cache.get(variable_name)
        if cached_value is not None:
            return cached_value
        if variable_name in resolving_names:
            return raw_values.get(variable_name, "")

        resolving_names.add(variable_name)
        resolved_value = raw_values.get(variable_name, "")
        previous_value: str | None = None
        while previous_value != resolved_value:
            previous_value = resolved_value
            resolved_value = variable_pattern.sub(
                lambda match: resolve_expression(match.group(1), resolving_names),
                resolved_value,
            )
        resolved_value = os.path.expandvars(resolved_value)
        resolved_value = os.path.expanduser(resolved_value)
        resolved_cache[variable_name] = resolved_value
        resolving_names.discard(variable_name)
        return resolved_value

    return {environment_key: resolve_value(environment_key, set()) for environment_key in raw_values}


def data_workflow_script_environment(environment_values: dict[str, str]) -> dict[str, str]:
    filtered_values: dict[str, str] = {}
    for environment_key, environment_value in environment_values.items():
        if environment_key in DATA_WORKFLOW_SCRIPT_ENV_KEYS:
            filtered_values[environment_key] = environment_value
            continue
        if any(environment_key.startswith(prefix) for prefix in DATA_WORKFLOW_SCRIPT_ENV_PREFIXES):
            filtered_values[environment_key] = environment_value
    return filtered_values


def missing_upstream_source_keys(environment_values: dict[str, str]) -> tuple[str, ...]:
    missing_keys: list[str] = []
    for environment_key in REQUIRED_UPSTREAM_ENV_KEYS:
        if environment_values.get(environment_key, "").strip():
            continue
        missing_keys.append(environment_key)
    return tuple(missing_keys)


def resolve_runtime_selection(
    *,
    stack_definition: StackDefinition,
    artifact_inputs_definition: ArtifactInputsDefinition | None,
    context_name: str,
    instance_name: str,
    repo_root: Path,
) -> RuntimeSelection:
    context_definition = stack_definition.contexts.get(context_name)
    if context_definition is None:
        available_contexts = ", ".join(sorted(stack_definition.contexts))
        raise RuntimeCommandError(f"Unknown context '{context_name}'. Available: {available_contexts}")
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        available_instances = ", ".join(sorted(context_definition.instances))
        raise RuntimeCommandError(
            f"Unknown instance '{instance_name}' for context '{context_name}'. Available: {available_instances}"
        )
    effective_install_modules = merge_effective_modules(
        context_definition=context_definition, instance_definition=instance_definition
    )
    effective_source_repositories = resolve_runtime_source_repositories(
        artifact_inputs_definition=artifact_inputs_definition,
        context_name=context_name,
        instance_name=instance_name,
    )
    effective_source_selectors = tuple(
        repository_spec for repository_spec in effective_source_repositories if repository_spec_declares_selector(repository_spec)
    )
    effective_runtime_env = merge_effective_runtime_env(
        stack_definition=stack_definition,
        context_definition=context_definition,
        instance_definition=instance_definition,
    )
    effective_odoo_overrides = merge_effective_odoo_overrides(
        stack_definition=stack_definition,
        context_definition=context_definition,
        instance_definition=instance_definition,
    )
    base_web_port, base_longpoll_port, base_db_port = port_seed_for_context(context_name)
    instance_offset = port_offset_for_instance(instance_name)
    database_name = instance_definition.database or context_definition.database or context_name
    state_root_path = resolve_local_platform_state_root(stack_definition=stack_definition, repo_root=repo_root)
    state_path = state_root_path / f"{context_name}-{instance_name}"
    return RuntimeSelection(
        context_name=context_name,
        instance_name=instance_name,
        context_definition=context_definition,
        instance_definition=instance_definition,
        database_name=database_name,
        project_name=f"odoo-{context_name}-{instance_name}",
        state_path=state_path,
        runtime_conf_host_path=state_path / "data" / "platform.odoo.conf",
        data_volume_name=f"odoo-{context_name}-{instance_name}-data",
        log_volume_name=f"odoo-{context_name}-{instance_name}-logs",
        db_volume_name=f"odoo-{context_name}-{instance_name}-db",
        web_host_port=base_web_port + instance_offset,
        longpoll_host_port=base_longpoll_port + instance_offset,
        db_host_port=base_db_port + instance_offset,
        runtime_odoo_conf_path="/tmp/platform.odoo.conf",
        effective_install_modules=effective_install_modules,
        effective_source_repositories=effective_source_repositories,
        effective_source_selectors=effective_source_selectors,
        effective_runtime_env=effective_runtime_env,
        effective_odoo_overrides=effective_odoo_overrides,
    )


def merge_effective_modules(*, context_definition: ContextDefinition, instance_definition: InstanceDefinition) -> tuple[str, ...]:
    effective_install_modules: list[str] = []
    for module_name in (*context_definition.install_modules, *instance_definition.install_modules_add):
        if module_name not in effective_install_modules:
            effective_install_modules.append(module_name)
    return tuple(effective_install_modules)


def resolve_runtime_source_repositories(
    *,
    artifact_inputs_definition: ArtifactInputsDefinition | None,
    context_name: str,
    instance_name: str,
) -> tuple[str, ...]:
    if artifact_inputs_definition is None:
        return ()
    return tuple(
        source_definition.repository_spec()
        for source_definition in effective_artifact_input_sources(
            artifact_inputs_definition=artifact_inputs_definition,
            context_name=context_name,
            instance_name=instance_name,
        )
    )


def merge_effective_runtime_env(
    *,
    stack_definition: StackDefinition,
    context_definition: ContextDefinition,
    instance_definition: InstanceDefinition,
) -> dict[str, str]:
    effective_runtime_env: dict[str, str] = {}
    for runtime_source in (stack_definition.runtime_env, context_definition.runtime_env, instance_definition.runtime_env):
        for key, raw_value in runtime_source.items():
            effective_runtime_env[key] = str(raw_value)
    return effective_runtime_env


def merge_effective_odoo_overrides(
    *,
    stack_definition: StackDefinition,
    context_definition: ContextDefinition,
    instance_definition: InstanceDefinition,
) -> OdooOverrideDefinition:
    return merge_odoo_override_definitions(
        stack_definition.odoo_overrides,
        context_definition.odoo_overrides,
        instance_definition.odoo_overrides,
    )


def port_seed_for_context(context_name: str) -> tuple[int, int, int]:
    return {
        "opw": (8069, 8072, 15432),
        "cm": (9069, 9072, 25432),
    }.get(context_name, (11069, 11072, 45432))


def port_offset_for_instance(instance_name: str) -> int:
    return {
        "local": 0,
        "dev": 100,
        "testing": 200,
        "prod": 300,
    }.get(instance_name, 0)


def resolve_local_platform_state_root(*, stack_definition: StackDefinition, repo_root: Path) -> Path:
    configured_root = stack_definition.state_root.strip()
    if not configured_root:
        return (repo_root / ".platform" / "state").resolve()
    expanded_state_root = Path(configured_root).expanduser()
    if expanded_state_root.is_absolute():
        return expanded_state_root.resolve()
    return (repo_root / expanded_state_root).resolve()


def write_runtime_odoo_conf_file(
    *,
    runtime_selection: RuntimeSelection,
    stack_definition: StackDefinition,
    source_environment: dict[str, str],
) -> Path:
    runtime_selection.runtime_conf_host_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[options]",
        f"db_name = {runtime_selection.database_name}",
        f"db_user = {source_environment.get('ODOO_DB_USER', 'odoo')}",
        f"db_password = {source_environment.get('ODOO_DB_PASSWORD', '')}",
        "db_host = database",
        "db_port = 5432",
        "list_db = False",
        f"addons_path = {','.join(stack_definition.addons_path)}",
        "data_dir = /volumes/data",
        "",
        f"; context={runtime_selection.context_name}",
        f"; instance={runtime_selection.instance_name}",
        f"; install_modules={','.join(runtime_selection.effective_install_modules)}",
    ]
    runtime_selection.runtime_conf_host_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return runtime_selection.runtime_conf_host_path


def write_runtime_env_file(*, runtime_context: RuntimeContext) -> Path:
    runtime_env_file = runtime_context.runtime_env_file
    runtime_env_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_values = build_runtime_env_values(runtime_context=runtime_context)
    runtime_env_file.write_text(render_runtime_env(runtime_values), encoding="utf-8")
    return runtime_env_file


def build_runtime_env_values(
    *,
    runtime_context: RuntimeContext,
    build_target_override: str | None = None,
    image_repository_override: str | None = None,
    image_tag_override: str | None = None,
    include_selection_sources: bool = True,
) -> dict[str, str]:
    stack_definition = runtime_context.stack.stack_definition
    runtime_selection = runtime_context.selection
    source_environment = runtime_context.environment.merged_values
    local_addons_mount_paths = resolve_manifest_local_addons_mount_paths(manifest=runtime_context.manifest)
    openupgrade_environment = {key: str(value) for key, value in runtime_selection.effective_runtime_env.items()}
    openupgrade_environment.update(source_environment)
    effective_source_repositories = effective_runtime_source_repositories(
        runtime_selection=runtime_selection,
        source_environment=openupgrade_environment,
        include_selection_sources=include_selection_sources,
    )
    compose_build_target = build_target_override or openupgrade_environment.get("COMPOSE_BUILD_TARGET", "development")
    docker_image = image_repository_override or runtime_selection.project_name
    docker_image_tag = image_tag_override or source_environment.get(
        "DOCKER_IMAGE_TAG",
        "prod-local" if runtime_selection.instance_name == "local" and compose_build_target == "production" else "latest",
    )
    runtime_values = {
        "PLATFORM_CONTEXT": runtime_selection.context_name,
        "PLATFORM_INSTANCE": runtime_selection.instance_name,
        "PLATFORM_RUNTIME_ENV_FILE": str(runtime_context.runtime_env_file),
        "PYTHON_VERSION": source_environment.get("PYTHON_VERSION", "3.13"),
        "ODOO_VERSION": stack_definition.odoo_version,
        "ODOO_STACK_NAME": f"{runtime_selection.context_name}-{runtime_selection.instance_name}",
        "ODOO_PROJECT_NAME": runtime_selection.project_name,
        "ODOO_STATE_ROOT": str(runtime_selection.state_path),
        "ODOO_RUNTIME_CONF_HOST_PATH": str(runtime_selection.runtime_conf_host_path),
        "ODOO_PROJECT_ADDONS_HOST_PATH": str(local_addons_mount_paths.project_addons_host_path),
        "ODOO_SHARED_ADDONS_HOST_PATH": str(local_addons_mount_paths.shared_addons_host_path)
        if local_addons_mount_paths.shared_addons_host_path is not None
        else source_environment.get("ODOO_SHARED_ADDONS_HOST_PATH", str(runtime_context.repo_root / "addons" / "shared")),
        "DOCKER_IMAGE": docker_image,
        "DOCKER_IMAGE_TAG": docker_image_tag,
        "COMPOSE_BUILD_TARGET": compose_build_target,
        "ODOO_DATA_VOLUME": runtime_selection.data_volume_name,
        "ODOO_LOG_VOLUME": runtime_selection.log_volume_name,
        "ODOO_DB_VOLUME": runtime_selection.db_volume_name,
        "ODOO_DB_NAME": runtime_selection.database_name,
        "ODOO_DB_USER": source_environment.get("ODOO_DB_USER", "odoo"),
        "ODOO_DB_PASSWORD": source_environment.get("ODOO_DB_PASSWORD", ""),
        "ODOO_FILESTORE_PATH": source_environment.get("ODOO_FILESTORE_PATH", "/volumes/data/filestore"),
        "ODOO_MASTER_PASSWORD": source_environment.get("ODOO_MASTER_PASSWORD", ""),
        "ODOO_ADMIN_LOGIN": source_environment.get("ODOO_ADMIN_LOGIN", ""),
        "ODOO_ADMIN_PASSWORD": source_environment.get("ODOO_ADMIN_PASSWORD", ""),
        "ODOO_INSTALL_MODULES": ",".join(runtime_selection.effective_install_modules),
        "ODOO_ADDON_REPOSITORIES": ",".join(effective_source_repositories),
        "ODOO_UPDATE_MODULES": runtime_selection.context_definition.update_modules,
        "ODOO_ADDONS_PATH": ",".join(stack_definition.addons_path),
        "ODOO_WEB_HOST_PORT": str(runtime_selection.web_host_port),
        "ODOO_LONGPOLL_HOST_PORT": str(runtime_selection.longpoll_host_port),
        "ODOO_DB_HOST_PORT": str(runtime_selection.db_host_port),
        "ODOO_LIST_DB": "False",
        "ODOO_WEB_COMMAND": f"python3 /volumes/scripts/run_odoo_startup.py -c {runtime_selection.runtime_odoo_conf_path}",
        "ODOO_DATA_WORKFLOW_LOCK_FILE": source_environment.get(
            "ODOO_DATA_WORKFLOW_LOCK_FILE",
            "/volumes/data/.data_workflow_in_progress",
        ),
        "ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS": source_environment.get("ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS", "7200"),
        "DATA_WORKFLOW_SSH_DIR": source_environment.get(
            "DATA_WORKFLOW_SSH_DIR",
            str(Path.home() / ".ssh") if runtime_selection.instance_name == "local" else "/root/.ssh",
        ),
        "OPENUPGRADE_ENABLED": openupgrade_environment.get("OPENUPGRADE_ENABLED", "False"),
        "OPENUPGRADE_ADDON_REPOSITORY": openupgrade_environment.get("OPENUPGRADE_ADDON_REPOSITORY", ""),
        "OPENUPGRADE_SCRIPTS_PATH": openupgrade_environment.get("OPENUPGRADE_SCRIPTS_PATH", ""),
        "OPENUPGRADE_TARGET_VERSION": openupgrade_environment.get("OPENUPGRADE_TARGET_VERSION", ""),
        "OPENUPGRADE_SKIP_UPDATE_ADDONS": openupgrade_environment.get("OPENUPGRADE_SKIP_UPDATE_ADDONS", "True"),
        "OPENUPGRADELIB_INSTALL_SPEC": openupgrade_environment.get("OPENUPGRADELIB_INSTALL_SPEC", ""),
        "ODOO_PYTHON_SYNC_SKIP_ADDONS": source_environment.get("ODOO_PYTHON_SYNC_SKIP_ADDONS", ""),
        "GITHUB_TOKEN": source_environment.get("GITHUB_TOKEN", ""),
    }
    if openupgrade_enabled(openupgrade_environment):
        runtime_values["OPENUPGRADE_ADDON_REPOSITORY"] = resolve_openupgrade_addon_repository(openupgrade_environment)
        runtime_values["OPENUPGRADELIB_INSTALL_SPEC"] = resolve_openupgradelib_install_spec(openupgrade_environment)
        runtime_values["ODOO_PYTHON_SYNC_SKIP_ADDONS"] = "openupgrade_framework,openupgrade_scripts,openupgrade_scripts_custom"
    for environment_key in sorted(source_environment):
        include_value = environment_key in PLATFORM_RUNTIME_PASSTHROUGH_KEYS or any(
            environment_key.startswith(prefix) for prefix in PLATFORM_RUNTIME_PASSTHROUGH_PREFIXES
        )
        if include_value:
            runtime_values[environment_key] = source_environment[environment_key]
    for runtime_key, runtime_value in runtime_selection.effective_runtime_env.items():
        runtime_values[runtime_key] = runtime_value
    apply_typed_odoo_instance_override_payload(
        runtime_values=runtime_values,
        context_name=runtime_selection.context_name,
        instance_name=runtime_selection.instance_name,
        odoo_overrides=runtime_selection.effective_odoo_overrides,
    )
    return runtime_values


def apply_typed_odoo_instance_override_payload(
    *,
    runtime_values: dict[str, str],
    context_name: str,
    instance_name: str,
    odoo_overrides: OdooOverrideDefinition | None = None,
) -> None:
    payload = build_typed_odoo_instance_override_payload(
        runtime_values=runtime_values,
        context_name=context_name,
        instance_name=instance_name,
        odoo_overrides=odoo_overrides,
    )
    if payload is None:
        return
    if runtime_values.get(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, "").strip():
        raise RuntimeCommandError(
            f"{ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY} cannot be combined with legacy ENV_OVERRIDE_* setting inputs."
        )
    encoded_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    runtime_values[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = base64.b64encode(encoded_payload).decode("ascii")
    for runtime_key in tuple(runtime_values):
        if runtime_key.startswith(LEGACY_SETTING_OVERRIDE_PREFIXES):
            runtime_values.pop(runtime_key, None)


def build_typed_odoo_instance_override_payload(
    *,
    runtime_values: dict[str, str],
    context_name: str,
    instance_name: str,
    odoo_overrides: OdooOverrideDefinition | None = None,
) -> dict[str, object] | None:
    config_parameters: list[dict[str, object]] = []
    addon_settings: list[dict[str, object]] = []
    effective_odoo_overrides = odoo_overrides or empty_odoo_override_definition()
    has_typed_odoo_overrides = bool(effective_odoo_overrides.config_parameters or effective_odoo_overrides.addon_settings)
    has_legacy_setting_overrides = any(runtime_key.startswith(LEGACY_SETTING_OVERRIDE_PREFIXES) for runtime_key in runtime_values)
    if has_typed_odoo_overrides and has_legacy_setting_overrides:
        raise RuntimeCommandError("Typed stack odoo_overrides cannot be combined with legacy ENV_OVERRIDE_* setting inputs.")
    for config_key in sorted(effective_odoo_overrides.config_parameters):
        config_parameters.append(
            {
                "key": config_key.strip().lower(),
                "value": {
                    "source": "literal",
                    "value": effective_odoo_overrides.config_parameters[config_key],
                },
            }
        )
    for addon_name in sorted(effective_odoo_overrides.addon_settings):
        setting_values = effective_odoo_overrides.addon_settings[addon_name]
        for setting_name in sorted(setting_values):
            addon_settings.append(
                {
                    "addon": addon_name.strip().lower(),
                    "setting": setting_name.strip().lower(),
                    "value": {"source": "literal", "value": setting_values[setting_name]},
                }
            )
    for runtime_key in sorted(runtime_values):
        runtime_value = runtime_values[runtime_key]
        if runtime_key.startswith(LEGACY_CONFIG_PARAM_PREFIX):
            suffix = runtime_key[len(LEGACY_CONFIG_PARAM_PREFIX) :].strip().lower()
            if not suffix:
                continue
            config_parameters.append(
                {
                    "key": suffix.replace("__", "."),
                    "value": {"source": "literal", "value": runtime_value},
                }
            )
            continue
        if runtime_key.startswith(LEGACY_AUTHENTIK_PREFIX):
            suffix = runtime_key[len(LEGACY_AUTHENTIK_PREFIX) :].strip().lower()
            if not suffix:
                continue
            addon_settings.append(
                {
                    "addon": "authentik_sso",
                    "setting": suffix,
                    "value": {"source": "literal", "value": runtime_value},
                }
            )
            continue
        if runtime_key.startswith(LEGACY_SHOPIFY_PREFIX):
            suffix = runtime_key[len(LEGACY_SHOPIFY_PREFIX) :].strip().lower()
            if not suffix:
                continue
            addon_settings.append(
                {
                    "addon": "shopify",
                    "setting": suffix,
                    "value": {"source": "literal", "value": runtime_value},
                }
            )
    if not config_parameters and not addon_settings:
        return None
    return {
        "schema_version": 1,
        "context": context_name,
        "instance": instance_name,
        "config_parameters": config_parameters,
        "addon_settings": addon_settings,
    }


def apply_publish_artifact_input_manifest(
    *,
    runtime_context: RuntimeContext,
    runtime_values: dict[str, str],
) -> dict[str, str]:
    artifact_inputs_definition = runtime_context.artifact_inputs_definition
    if artifact_inputs_definition is None:
        return runtime_values
    effective_sources = effective_artifact_input_sources(
        artifact_inputs_definition=artifact_inputs_definition,
        context_name=runtime_context.selection.context_name,
        instance_name=runtime_context.selection.instance_name,
    )
    updated_values = dict(runtime_values)
    updated_values["ODOO_ADDON_REPOSITORIES"] = ",".join(
        source_definition.repository_spec() for source_definition in effective_sources
    )
    return updated_values


@dataclass(frozen=True)
class LocalAddonsMountPaths:
    project_addons_host_path: Path
    shared_addons_host_path: Path | None


def resolve_manifest_local_addons_mount_paths(*, manifest: WorkspaceManifest) -> LocalAddonsMountPaths:
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if tenant_repo_path is None or not tenant_repo_path.exists():
        raise RuntimeCommandError("Tenant repo path must exist before generating local runtime addon mounts.")
    project_addons_host_path = (tenant_repo_path / "addons").resolve()
    if not project_addons_host_path.exists():
        raise RuntimeCommandError(f"Tenant addons path does not exist: {project_addons_host_path}")
    shared_addons_repo_path = resolve_optional_repo_path_with_managed_checkout(
        manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "shared-addons",
    )
    if shared_addons_repo_path is not None and not shared_addons_repo_path.exists():
        raise RuntimeCommandError(f"Shared addons path does not exist: {shared_addons_repo_path}")
    return LocalAddonsMountPaths(
        project_addons_host_path=project_addons_host_path,
        shared_addons_host_path=shared_addons_repo_path.resolve() if shared_addons_repo_path is not None else None,
    )


def stage_artifact_build_context(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    staged_context_root: Path,
) -> None:
    tenant_repo_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if tenant_repo_path is None or not tenant_repo_path.exists():
        raise RuntimeCommandError("Tenant repo path must exist before staging an artifact build context.")
    shared_addons_repo_path = resolve_manifest_shared_addons_repo_path(manifest=manifest)

    staged_context_root.mkdir(parents=True, exist_ok=True)
    copy_required_path(
        source_path=runtime_repo_path / "docker",
        destination_path=staged_context_root / "docker",
        label="runtime docker directory",
    )
    copy_required_path(
        source_path=runtime_repo_path / "platform" / "config",
        destination_path=staged_context_root / "platform" / "config",
        label="runtime platform config directory",
    )
    copy_required_path(
        source_path=runtime_repo_path / "pyproject.toml",
        destination_path=staged_context_root / "pyproject.toml",
        label="runtime pyproject.toml",
    )
    copy_required_path(
        source_path=runtime_repo_path / "uv.lock",
        destination_path=staged_context_root / "uv.lock",
        label="runtime uv.lock",
    )

    staged_addons_root = staged_context_root / "addons"
    tenant_addons_root = tenant_repo_path / "addons"
    if not tenant_addons_root.exists():
        raise RuntimeCommandError(f"Tenant addons path does not exist: {tenant_addons_root}")
    staged_addons_root.mkdir(parents=True, exist_ok=True)
    for child_path in sorted(tenant_addons_root.iterdir()):
        if shared_addons_repo_path is not None and child_path.name == "shared":
            continue
        copy_required_path(
            source_path=child_path,
            destination_path=staged_addons_root / child_path.name,
            label=f"tenant addon path {child_path}",
        )

    if shared_addons_repo_path is not None:
        staged_shared_addons_root = staged_addons_root / "shared"
        staged_shared_addons_root.mkdir(parents=True, exist_ok=True)
        for child_path in sorted(shared_addons_repo_path.iterdir()):
            copy_required_path(
                source_path=child_path,
                destination_path=staged_shared_addons_root / child_path.name,
                label=f"shared addon path {child_path}",
            )


def copy_required_path(*, source_path: Path, destination_path: Path, label: str) -> None:
    if not source_path.exists():
        raise RuntimeCommandError(f"Missing required {label}: {source_path}")
    if source_path.is_dir():
        shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
        return
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)


def resolve_manifest_shared_addons_repo_path(*, manifest: WorkspaceManifest) -> Path | None:
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    return resolve_optional_repo_path_with_managed_checkout(
        manifest.shared_addons_repo,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "shared-addons",
    )


def collect_artifact_source_entries(
    *,
    manifest: WorkspaceManifest,
    runtime_values: dict[str, str],
) -> tuple[dict[str, str], ...]:
    source_entries: list[dict[str, str]] = []
    shared_addons_repo_path = resolve_manifest_shared_addons_repo_path(manifest=manifest)
    if manifest.shared_addons_repo is not None and shared_addons_repo_path is not None:
        source_entries.append(
            {
                "repository": manifest.shared_addons_repo.url or manifest.shared_addons_repo.name,
                "ref": require_clean_git_commit(
                    repo_path=shared_addons_repo_path.resolve(),
                    label=manifest.shared_addons_repo.name,
                ),
            }
        )

    seen_repository_refs: set[tuple[str, str]] = {(entry["repository"], entry["ref"]) for entry in source_entries}
    for env_key in ARTIFACT_SOURCE_ENV_KEYS:
        raw_value = runtime_values.get(env_key, "")
        for repository, ref in parse_artifact_source_repository_entries(raw_value, require_exact_shas=True):
            repository_key = (repository, ref)
            if repository_key in seen_repository_refs:
                continue
            seen_repository_refs.add(repository_key)
            source_entries.append({"repository": repository, "ref": ref})
    return tuple(source_entries)


def parse_artifact_source_repository_entries(
    raw_value: str,
    *,
    require_exact_shas: bool = False,
) -> tuple[tuple[str, str], ...]:
    entries = []
    seen_entries: set[tuple[str, str]] = set()
    for raw_entry in raw_value.replace("\n", ",").split(","):
        candidate_entry = raw_entry.strip()
        if not candidate_entry:
            continue
        repository_name, separator, repository_ref = candidate_entry.rpartition("@")
        if not separator or not repository_name.strip() or not repository_ref.strip():
            raise RuntimeCommandError(
                f"Artifact publish requires addon repositories to use '<repo>@<ref>' form. Received: {candidate_entry}"
            )
        normalized_repository = repository_name.strip()
        normalized_ref = repository_ref.strip()
        if require_exact_shas and not GIT_SHA_PATTERN.fullmatch(normalized_ref):
            raise RuntimeCommandError(
                f"Artifact publish requires addon repositories to use exact git SHAs. Received: {candidate_entry}"
            )
        normalized_entry = (normalized_repository, normalized_ref)
        if normalized_entry in seen_entries:
            continue
        seen_entries.add(normalized_entry)
        entries.append(normalized_entry)
    return tuple(entries)


def resolve_artifact_runtime_source_repository_refs(
    *,
    runtime_values: dict[str, str],
) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
    resolved_values = dict(runtime_values)
    github_token = clean_optional_value(runtime_values.get("GITHUB_TOKEN"))
    selector_metadata: list[dict[str, str]] = []
    for env_key in ARTIFACT_SOURCE_ENV_KEYS:
        raw_value = runtime_values.get(env_key, "")
        parsed_entries = parse_artifact_source_repository_entries(raw_value)
        if not parsed_entries:
            continue
        resolved_entries: list[tuple[str, str]] = []
        for repository, ref in parsed_entries:
            resolved_ref = ref
            if not GIT_SHA_PATTERN.fullmatch(ref):
                resolved_ref = resolve_source_repository_ref_to_git_sha(
                    repository=repository,
                    ref=ref,
                    github_token=github_token,
                )
                selector_metadata.append(
                    {
                        "repository": repository,
                        "selector": ref,
                        "resolved_ref": resolved_ref,
                    }
                )
            resolved_entries.append((repository, resolved_ref))
        resolved_values[env_key] = ",".join(f"{repository}@{resolved_ref}" for repository, resolved_ref in resolved_entries)
    return resolved_values, tuple(selector_metadata)


def resolve_source_repository_ref_to_git_sha(*, repository: str, ref: str, github_token: str | None = None) -> str:
    normalized_repository = repository.strip()
    normalized_ref = ref.strip()
    if GIT_SHA_PATTERN.fullmatch(normalized_ref):
        return normalized_ref
    remote_url = resolve_source_repository_remote_url(normalized_repository)
    execution_env = command_execution_env()
    normalized_token = clean_optional_value(github_token)
    if normalized_token and remote_url.startswith("https://github.com/"):
        encoded_auth = base64.b64encode(f"x-access-token:{normalized_token}".encode()).decode("ascii")
        execution_env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded_auth}",
            }
        )
    ls_remote_result = subprocess.run(
        ["git", "ls-remote", "--refs", remote_url, normalized_ref],
        capture_output=True,
        text=True,
        env=execution_env,
    )
    if ls_remote_result.returncode != 0:
        details = clean_optional_value(ls_remote_result.stderr) or clean_optional_value(ls_remote_result.stdout)
        raise RuntimeCommandError(
            "Artifact publish could not resolve addon repository ref "
            f"{normalized_repository}@{normalized_ref}." + (f"\nGit reported: {details}" if details else "")
        )
    resolved_shas = tuple(line.split("\t", 1)[0].strip() for line in ls_remote_result.stdout.splitlines() if line.strip())
    unique_resolved_shas = tuple(dict.fromkeys(resolved_shas))
    if not unique_resolved_shas:
        raise RuntimeCommandError(
            "Artifact publish requires addon repository selectors to resolve to an exact git SHA. "
            f"No remote ref matched {normalized_repository}@{normalized_ref}."
        )
    if len(unique_resolved_shas) != 1 or not GIT_SHA_PATTERN.fullmatch(unique_resolved_shas[0]):
        raise RuntimeCommandError(
            "Artifact publish requires addon repository selectors to resolve unambiguously to one git SHA. "
            f"Received matches for {normalized_repository}@{normalized_ref}: {', '.join(unique_resolved_shas)}"
        )
    return unique_resolved_shas[0]


def resolve_source_repository_remote_url(repository: str) -> str:
    normalized_repository = repository.strip()
    if not normalized_repository:
        raise RuntimeCommandError("Artifact publish requires a non-empty addon repository name.")
    if normalized_repository.startswith("git@") or "://" in normalized_repository:
        return normalized_repository
    candidate_path = Path(normalized_repository).expanduser()
    if candidate_path.is_absolute():
        return str(candidate_path)
    if normalized_repository.count("/") == 1:
        return f"https://github.com/{normalized_repository}.git"
    return normalized_repository


def require_clean_git_commit(*, repo_path: Path, label: str) -> str:
    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=command_execution_env(),
    )
    if head_result.returncode != 0:
        raise RuntimeCommandError(f"Unable to resolve git commit for {label}: {repo_path}")
    head_commit = head_result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_commit):
        raise RuntimeCommandError(f"Unable to resolve a full git commit for {label}: {repo_path}")

    dirty_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=command_execution_env(),
    )
    if dirty_result.returncode != 0:
        raise RuntimeCommandError(f"Unable to determine git status for {label}: {repo_path}")
    if dirty_result.stdout.strip():
        raise RuntimeCommandError(f"Artifact publish requires a clean git worktree for {label}: {repo_path}")
    return head_commit


def resolve_github_token_for_build(environment_values: dict[str, str]) -> str | None:
    configured_token = clean_optional_value(environment_values.get("GITHUB_TOKEN"))
    if configured_token:
        return configured_token
    return resolve_ghcr_read_token(environment_values)


def ensure_registry_auth_for_image_push(
    *,
    environment_values: dict[str, str],
    image_repository: str,
) -> None:
    registry_host = extract_registry_host(image_repository)
    if registry_host != GHCR_HOST:
        return
    ghcr_username = resolve_ghcr_username(environment_values, image_repository)
    ghcr_token = resolve_ghcr_push_token(environment_values)
    if not ghcr_username:
        raise RuntimeCommandError(
            "Missing GHCR username for artifact push. Set GHCR_USERNAME in resolved environment "
            f"({runtime_environment_configuration_guidance(noun='it')}) or provide GITHUB_ACTOR in the current shell."
        )
    if not ghcr_token:
        raise RuntimeCommandError(
            "Missing GHCR token for artifact push. Set GHCR_TOKEN (preferred) or GITHUB_TOKEN in resolved environment "
            f"({runtime_environment_configuration_guidance(noun='it')}) with write:packages access."
        )
    ensure_registry_login(registry_host=GHCR_HOST, username=ghcr_username, token=ghcr_token, purpose="push")


def ensure_registry_login(*, registry_host: str, username: str, token: str, purpose: str) -> None:
    login_key = (registry_host, username, purpose)
    if login_key in _REGISTRY_LOGINS_DONE:
        return
    login_result = subprocess.run(
        ["docker", "login", registry_host, "-u", username, "--password-stdin"],
        input=f"{token}\n",
        capture_output=True,
        text=True,
        env=command_execution_env(),
    )
    if login_result.returncode != 0:
        details = clean_optional_value(login_result.stderr) or clean_optional_value(login_result.stdout)
        raise RuntimeCommandError(f"Docker login to {registry_host} failed." + (f"\nDocker reported: {details}" if details else ""))
    _REGISTRY_LOGINS_DONE.add(login_key)


def resolve_image_digest(image_reference: str) -> str:
    candidate = image_reference.strip()
    if not candidate:
        raise RuntimeCommandError("Image digest resolution requires a non-empty image reference.")
    digest_match = re.search(r"@(sha256:[0-9a-fA-F]{64})$", candidate)
    if digest_match is not None:
        return digest_match.group(1)
    inspect_result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", candidate],
        capture_output=True,
        text=True,
        env=command_execution_env(),
    )
    if inspect_result.returncode != 0:
        details = clean_optional_value(inspect_result.stderr) or clean_optional_value(inspect_result.stdout)
        raise RuntimeCommandError(
            f"Unable to resolve image digest for '{candidate}'." + (f"\nDocker reported: {details}" if details else "")
        )
    digest_match = re.search(r"^Digest:\s*(sha256:[0-9a-fA-F]{64})\s*$", inspect_result.stdout, flags=re.MULTILINE)
    if digest_match is None:
        raise RuntimeCommandError(f"Unable to parse image digest from docker output for {candidate}.")
    return digest_match.group(1)


def resolve_buildx_metadata_image_digest(metadata_file: Path) -> str:
    try:
        raw_metadata = metadata_file.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeCommandError(f"Unable to read Buildx metadata file: {metadata_file}") from error
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError as error:
        raise RuntimeCommandError(f"Unable to parse Buildx metadata file as JSON: {metadata_file}") from error
    if not isinstance(metadata, dict):
        raise RuntimeCommandError(f"Buildx metadata file must contain a JSON object: {metadata_file}")

    candidates = [metadata.get("containerimage.digest")]
    descriptor = metadata.get("containerimage.descriptor")
    if isinstance(descriptor, dict):
        candidates.append(descriptor.get("digest"))
    for candidate in candidates:
        if isinstance(candidate, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", candidate.strip()):
            return candidate.strip()
    raise RuntimeCommandError(f"Buildx metadata file did not include a valid container image digest: {metadata_file}")


def build_runtime_artifact_manifest_payload(
    *,
    context_name: str,
    source_commit: str,
    runtime_repo_name: str,
    runtime_repo_commit: str,
    artifact_source_entries: tuple[dict[str, str], ...],
    source_selector_entries: tuple[dict[str, str], ...],
    openupgrade_addon_repository: str,
    openupgradelib_install_spec: str,
    addon_skip_flags: tuple[str, ...],
    image_repository: str,
    image_tag: str,
    image_digest: str,
    enterprise_base_digest: str,
    odoo_version: str,
) -> dict[str, object]:
    artifact_id = f"artifact-{context_name}-{image_digest.removeprefix('sha256:')[:16]}"
    build_flag_values = {
        "build_target": "production",
        "image_tag": image_tag,
        "odoo_version": odoo_version,
        "runtime_repo": runtime_repo_name,
        "runtime_repo_commit": runtime_repo_commit,
    }
    return {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "source_commit": source_commit,
        "enterprise_base_digest": enterprise_base_digest,
        "addon_sources": list(artifact_source_entries),
        "addon_selectors": list(source_selector_entries),
        "openupgrade_inputs": {
            "addon_repository": openupgrade_addon_repository,
            "install_spec": openupgradelib_install_spec,
        },
        "build_flags": {
            "addon_skip_flags": list(addon_skip_flags),
            "values": build_flag_values,
        },
        "image": {
            "repository": image_repository,
            "digest": image_digest,
            "tags": [image_tag],
        },
    }


def parse_csv_values(raw_value: str) -> tuple[str, ...]:
    values = []
    seen_values: set[str] = set()
    for raw_entry in raw_value.split(","):
        normalized_entry = raw_entry.strip()
        if not normalized_entry or normalized_entry in seen_values:
            continue
        seen_values.add(normalized_entry)
        values.append(normalized_entry)
    return tuple(values)


def repository_spec_identity(repository_spec: str) -> str:
    normalized_spec = repository_spec.strip()
    if not normalized_spec:
        return ""
    repository_name, separator, _repository_ref = normalized_spec.rpartition("@")
    if separator and repository_name.strip():
        return repository_name.strip()
    return normalized_spec


def repository_spec_declares_selector(repository_spec: str) -> bool:
    normalized_spec = repository_spec.strip()
    if not normalized_spec:
        return False
    repository_name, separator, repository_ref = normalized_spec.rpartition("@")
    if not separator or not repository_name.strip() or not repository_ref.strip():
        return False
    return not GIT_SHA_PATTERN.fullmatch(repository_ref.strip())


def render_runtime_env(runtime_values: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in runtime_values.items()) + "\n"


def effective_runtime_source_repositories(
    *,
    runtime_selection: RuntimeSelection,
    source_environment: dict[str, str],
    include_selection_sources: bool = True,
) -> tuple[str, ...]:
    effective_repositories: list[str] = []
    repository_indexes: dict[str, int] = {}

    def upsert_repository(repository_spec: str) -> None:
        normalized_repository = repository_spec.strip()
        if not normalized_repository:
            return
        repository_identity = repository_spec_identity(normalized_repository)
        existing_index = repository_indexes.get(repository_identity)
        if existing_index is None:
            repository_indexes[repository_identity] = len(effective_repositories)
            effective_repositories.append(normalized_repository)
            return
        effective_repositories[existing_index] = normalized_repository

    if include_selection_sources:
        for configured_repository in runtime_selection.effective_source_repositories:
            upsert_repository(configured_repository)
    for configured_repository in parse_csv_values(source_environment.get("ODOO_ADDON_REPOSITORIES", "")):
        upsert_repository(configured_repository)
    if openupgrade_enabled(source_environment):
        openupgrade_repository = resolve_openupgrade_addon_repository(source_environment)
        upsert_repository(openupgrade_repository)
    return tuple(effective_repositories)


def openupgrade_enabled(source_environment: dict[str, str]) -> bool:
    return source_environment.get("OPENUPGRADE_ENABLED", "False").strip().lower() in {"1", "true", "yes", "on"}


def resolve_openupgrade_addon_repository(source_environment: dict[str, str]) -> str:
    repository_name = source_environment.get("OPENUPGRADE_ADDON_REPOSITORY", "").strip()
    if repository_name:
        return repository_name
    raise RuntimeCommandError("OPENUPGRADE_ADDON_REPOSITORY must be set when OPENUPGRADE_ENABLED is true.")


def resolve_openupgradelib_install_spec(source_environment: dict[str, str]) -> str:
    install_specification = source_environment.get("OPENUPGRADELIB_INSTALL_SPEC", "").strip()
    if install_specification:
        return install_specification
    raise RuntimeCommandError("OPENUPGRADELIB_INSTALL_SPEC must be set when OPENUPGRADE_ENABLED is true.")


def runtime_env_file_for_scope(*, repo_root: Path, context_name: str, instance_name: str) -> Path:
    return repo_root / ".platform" / "env" / f"{context_name}.{instance_name}.env"


def ensure_runtime_env_file(*, repo_root: Path, context_name: str, instance_name: str) -> Path:
    runtime_env_file = runtime_env_file_for_scope(repo_root=repo_root, context_name=context_name, instance_name=instance_name)
    if runtime_env_file.exists():
        return runtime_env_file
    raise RuntimeCommandError(
        f"Runtime env file not found: {runtime_env_file}. Run 'uv run platform runtime select --manifest <workspace.toml>' first."
    )


def compose_base_command(*, runtime_repo_path: Path, runtime_env_file: Path) -> list[str]:
    compose_env_file = compose_runtime_env_file(runtime_env_file)
    compose_files = [
        runtime_repo_path / "docker-compose.yml",
        runtime_repo_path / "platform" / "compose" / "base.yaml",
    ]
    optional_override_file = runtime_repo_path / "docker-compose.override.yml"
    if optional_override_file.exists():
        compose_files.append(optional_override_file)
    missing_files = [compose_file for compose_file in compose_files if not compose_file.exists()]
    if missing_files:
        missing_display = ", ".join(str(compose_file) for compose_file in missing_files)
        raise RuntimeCommandError(f"Missing required compose files: {missing_display}")
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(runtime_repo_path),
        "--env-file",
        str(compose_env_file),
    ]
    for compose_file in compose_files:
        command.extend(["-f", str(compose_file)])
    return command


def compose_runtime_env_file(runtime_env_file: Path) -> Path:
    compose_env_file = runtime_env_file.with_suffix(".compose.env")
    runtime_env_values = parse_env_file(runtime_env_file)
    runtime_env_values.pop("DOCKER_IMAGE_REFERENCE", None)
    runtime_env_values["PLATFORM_RUNTIME_ENV_FILE"] = str(compose_env_file)
    compose_env_file.write_text(render_runtime_env(runtime_env_values), encoding="utf-8")
    return compose_env_file


def build_registry_auth_environment(*, source_environment: dict[str, str], runtime_env_file: Path) -> dict[str, str]:
    registry_auth_environment = dict(source_environment)
    registry_auth_environment.update(parse_env_file(runtime_env_file))
    return registry_auth_environment


def run_command(
    *,
    runtime_repo_path: Path,
    command: list[str],
    environment_overrides: dict[str, str] | None = None,
    allowed_return_codes: set[int] | None = None,
) -> None:
    execution_environment = command_execution_env()
    if environment_overrides is not None:
        execution_environment.update(environment_overrides)
    accepted_return_codes = allowed_return_codes or {0}
    result = subprocess.run(command, cwd=runtime_repo_path, env=execution_environment)
    if result.returncode not in accepted_return_codes:
        raise RuntimeCommandError(f"Command failed ({result.returncode}): {' '.join(command)}")


def run_command_best_effort(
    *,
    runtime_repo_path: Path,
    command: list[str],
    environment_overrides: dict[str, str] | None = None,
) -> int:
    execution_environment = command_execution_env()
    if environment_overrides is not None:
        execution_environment.update(environment_overrides)
    result = subprocess.run(command, cwd=runtime_repo_path, env=execution_environment)
    return result.returncode


def run_command_with_input(*, runtime_repo_path: Path, command: list[str], input_text: str) -> None:
    result = subprocess.run(command, input=input_text.encode(), cwd=runtime_repo_path, env=command_execution_env())
    if result.returncode != 0:
        raise RuntimeCommandError(f"Command failed ({result.returncode}): {' '.join(command)}")


def compose_exec(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    container_service: str,
    container_command: list[str],
) -> None:
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    run_command(
        runtime_repo_path=runtime_repo_path,
        command=compose_command + ["exec", "-T", container_service] + container_command,
    )


def compose_exec_with_input(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    container_service: str,
    container_command: list[str],
    input_text: str,
) -> None:
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    run_command_with_input(
        runtime_repo_path=runtime_repo_path,
        command=compose_command + ["exec", "-T", container_service] + container_command,
        input_text=input_text,
    )


def compose_up_script_runner(*, runtime_repo_path: Path, runtime_env_file: Path) -> None:
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    run_command(runtime_repo_path=runtime_repo_path, command=compose_command + ["up", "-d", "script-runner"])


def stream_runtime_logs(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    service: str,
    tail_lines: int,
    follow: bool,
) -> None:
    load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    logs_command = compose_command + ["logs", "--timestamps", "--no-color", "--tail", str(tail_lines)]
    if follow:
        logs_command.append("--follow")
    normalized_service = service.strip()
    if normalized_service:
        logs_command.append(normalized_service)
    run_command(runtime_repo_path=runtime_repo_path, command=logs_command)


def run_psql_command(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    psql_arguments: tuple[str, ...],
) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    db_user = runtime_context.environment.merged_values.get("ODOO_DB_USER", "odoo").strip() or "odoo"
    db_password = runtime_context.environment.merged_values.get("ODOO_DB_PASSWORD", "")
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    psql_command = compose_command + ["exec", "-T"]
    if db_password:
        psql_command.extend(["-e", f"PGPASSWORD={db_password}"])
    psql_command.extend(
        [
            "database",
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            db_user,
            "-d",
            runtime_context.selection.database_name,
            *psql_arguments,
        ]
    )
    run_command(runtime_repo_path=runtime_repo_path, command=psql_command)


def run_odoo_shell_command(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    service: str,
    database_name: str | None,
    script_path: Path | None,
    log_file: Path | None,
    dry_run: bool,
) -> None:
    runtime_context = load_runtime_context(manifest=manifest, runtime_repo_path=runtime_repo_path)
    runtime_env_file = ensure_runtime_env_file(
        repo_root=runtime_repo_path,
        context_name=manifest.runtime.context,
        instance_name=manifest.runtime.instance,
    )
    normalized_service = service.strip()
    if not normalized_service:
        raise RuntimeCommandError("Service name must be a non-empty value.")
    target_database = (database_name or "").strip() or runtime_context.selection.database_name
    addons_path_argument = ",".join(runtime_context.stack.stack_definition.addons_path)
    odoo_shell_command = [
        "/odoo/odoo-bin",
        "shell",
        "-d",
        target_database,
        f"--addons-path={addons_path_argument}",
        "--data-dir=/volumes/data",
        "--db_host=database",
        "--db_port=5432",
        f"--db_user={runtime_context.environment.merged_values.get('ODOO_DB_USER', 'odoo')}",
        f"--db_password={runtime_context.environment.merged_values.get('ODOO_DB_PASSWORD', '')}",
    ]
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)

    resolved_script_path: Path | None = None
    script_text: str | None = None
    if script_path is not None:
        resolved_script_path = script_path.expanduser().resolve()
        if not resolved_script_path.exists():
            raise RuntimeCommandError(f"Odoo shell script not found: {resolved_script_path}")
        script_text = resolved_script_path.read_text(encoding="utf-8")

    resolved_log_file: Path | None = None
    if log_file is not None:
        resolved_log_file = log_file.expanduser().resolve()

    odoo_shell_exec_command = compose_command + ["exec"]
    if script_text is not None or resolved_log_file is not None:
        odoo_shell_exec_command.append("-T")
    odoo_shell_exec_command.extend([normalized_service, *odoo_shell_command])

    if dry_run:
        command_display = " ".join(odoo_shell_exec_command)
        if resolved_script_path is not None:
            command_display = f"{command_display} < {resolved_script_path}"
        if resolved_log_file is not None:
            command_display = f"{command_display} > {resolved_log_file} 2>&1"
        print(f"$ {command_display}")
        return

    execution_environment = command_execution_env()
    output_handle = None
    try:
        if resolved_log_file is not None:
            resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
            output_handle = resolved_log_file.open("wb")
            if script_text is not None:
                result = subprocess.run(
                    odoo_shell_exec_command,
                    input=script_text.encode(),
                    cwd=runtime_repo_path,
                    env=execution_environment,
                    stdout=output_handle,
                    stderr=subprocess.STDOUT,
                )
            else:
                result = subprocess.run(
                    odoo_shell_exec_command,
                    cwd=runtime_repo_path,
                    env=execution_environment,
                    stdout=output_handle,
                    stderr=subprocess.STDOUT,
                )
        elif script_text is not None:
            result = subprocess.run(
                odoo_shell_exec_command,
                input=script_text.encode(),
                cwd=runtime_repo_path,
                env=execution_environment,
            )
        else:
            result = subprocess.run(
                odoo_shell_exec_command,
                cwd=runtime_repo_path,
                env=execution_environment,
            )
    finally:
        if output_handle is not None:
            output_handle.close()
    if result.returncode != 0:
        raise RuntimeCommandError(f"Command failed ({result.returncode}): {' '.join(odoo_shell_exec_command)}")


def wait_for_compose_service(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    service_name: str,
    timeout_seconds: int = 60,
) -> None:
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            compose_command + ["ps", "-q", service_name],
            cwd=runtime_repo_path,
            env=command_execution_env(),
            capture_output=True,
            text=True,
        )
        container_id = (result.stdout or "").strip()
        if container_id:
            status_result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container_id],
                cwd=runtime_repo_path,
                env=command_execution_env(),
                capture_output=True,
                text=True,
            )
            if (status_result.stdout or "").strip() == "running":
                return
        time.sleep(2)
    raise RuntimeCommandError(f"Timed out waiting for {service_name} to be running.")


def normalize_local_filestore_permissions(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    data_workflow_environment: dict[str, str],
) -> None:
    filestore_root = (data_workflow_environment.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore").strip()
    if not filestore_root:
        filestore_root = "/volumes/data/filestore"
    database_name = data_workflow_environment.get("ODOO_DB_NAME", "").strip()
    filestore_database_path = filestore_root
    if Path(filestore_root).name != database_name:
        filestore_database_path = f"{filestore_root.rstrip('/')}/{database_name}"
    permission_script = (
        "set -euo pipefail; "
        "target_owner=$(stat -c '%u:%g' /volumes/data); "
        f"mkdir -p {shlex.quote(filestore_root)} {shlex.quote(filestore_database_path)}; "
        f'chown -R "$target_owner" {shlex.quote(filestore_database_path)}; '
        f"chmod -R ug+rwX {shlex.quote(filestore_database_path)}"
    )
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    run_command(
        runtime_repo_path=runtime_repo_path,
        command=compose_command + ["exec", "-T", "--user", "root", "script-runner", "/bin/bash", "-lc", permission_script],
    )


def build_data_workflow_exec_args(
    *,
    data_workflow_environment: dict[str, str],
    bootstrap: bool,
    no_sanitize: bool,
    update_only: bool,
) -> list[str]:
    exec_arguments = ["exec", "-T"]
    for environment_key in sorted(data_workflow_script_environment(data_workflow_environment)):
        exec_arguments.extend(["-e", environment_key])
    if bootstrap:
        exec_arguments.extend(["-e", "BOOTSTRAP=1"])
    if no_sanitize:
        exec_arguments.extend(["-e", "NO_SANITIZE=1"])
    if update_only:
        exec_arguments.extend(["-e", "UPDATE_ONLY=1"])
    exec_arguments.extend(["script-runner", "python3", "-u", DATA_WORKFLOW_SCRIPT])
    if bootstrap:
        exec_arguments.append("--bootstrap")
    if no_sanitize:
        exec_arguments.append("--no-sanitize")
    if update_only:
        exec_arguments.append("--update-only")
    return exec_arguments


def run_with_web_temporarily_stopped(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    operation: Callable[[], None],
) -> None:
    compose_command = compose_base_command(runtime_repo_path=runtime_repo_path, runtime_env_file=runtime_env_file)
    stop_web_command = compose_command + ["stop", "web"]
    up_web_command = compose_command + ["up", "-d", "web"]
    run_command_best_effort(runtime_repo_path=runtime_repo_path, command=stop_web_command)
    try:
        operation()
    finally:
        run_command_best_effort(runtime_repo_path=runtime_repo_path, command=up_web_command)


def apply_admin_password_if_configured(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    runtime_selection: RuntimeSelection,
    stack_definition: StackDefinition,
    loaded_environment: dict[str, str],
) -> None:
    admin_password = loaded_environment.get("ODOO_ADMIN_PASSWORD", "").strip()
    if not admin_password:
        return
    configured_admin_login = loaded_environment.get("ODOO_ADMIN_LOGIN", "").strip() or "admin"
    addons_path_argument = ",".join(stack_definition.addons_path)
    odoo_shell_command = [
        "/odoo/odoo-bin",
        "shell",
        "-d",
        runtime_selection.database_name,
        f"--addons-path={addons_path_argument}",
        "--data-dir=/volumes/data",
        "--db_host=database",
        "--db_port=5432",
        f"--db_user={loaded_environment.get('ODOO_DB_USER', 'odoo')}",
        f"--db_password={loaded_environment.get('ODOO_DB_PASSWORD', '')}",
    ]
    script_payload = {"password": admin_password, "login": configured_admin_login}
    odoo_shell_script = textwrap.dedent(
        """
        import json

        payload = json.loads('__PAYLOAD__')
        admin_user = env['res.users'].sudo().with_context(active_test=False).search(
            [('login', '=', payload['login'])],
            limit=1,
        )
        if not admin_user:
            raise ValueError(f"Configured admin user not found: {payload['login']}")
        admin_user.with_context(no_reset_password=True).sudo().write({'password': payload['password']})
        print("admin_password_updated=true")
        env.cr.commit()
        """
    ).replace("__PAYLOAD__", json.dumps(script_payload))
    compose_exec_with_input(
        runtime_repo_path=runtime_repo_path,
        runtime_env_file=runtime_env_file,
        container_service="script-runner",
        container_command=odoo_shell_command,
        input_text=odoo_shell_script,
    )


def assert_active_admin_password_is_not_default(
    *,
    runtime_repo_path: Path,
    runtime_env_file: Path,
    runtime_selection: RuntimeSelection,
    stack_definition: StackDefinition,
    loaded_environment: dict[str, str],
) -> None:
    addons_path_argument = ",".join(stack_definition.addons_path)
    odoo_shell_command = [
        "/odoo/odoo-bin",
        "shell",
        "-d",
        runtime_selection.database_name,
        f"--addons-path={addons_path_argument}",
        "--data-dir=/volumes/data",
        "--db_host=database",
        "--db_port=5432",
        f"--db_user={loaded_environment.get('ODOO_DB_USER', 'odoo')}",
        f"--db_password={loaded_environment.get('ODOO_DB_PASSWORD', '')}",
    ]
    configured_admin_login = loaded_environment.get("ODOO_ADMIN_LOGIN", "").strip() or "admin"
    login_names_to_check = ["admin"]
    if configured_admin_login not in login_names_to_check:
        login_names_to_check.append(configured_admin_login)
    script_payload = {"logins": login_names_to_check}
    odoo_shell_script = textwrap.dedent(
        """
        import json
        from odoo.exceptions import AccessDenied

        payload = json.loads('__PAYLOAD__')

        for login_name in payload['logins']:
            target_user = env['res.users'].sudo().with_context(active_test=False).search(
                [('login', '=', login_name)],
                limit=1,
            )
            if not target_user:
                continue

            authenticated = False
            try:
                auth_info = env['res.users'].sudo().authenticate(
                    {'type': 'password', 'login': login_name, 'password': 'admin'},
                    {'interactive': False},
                )
                authenticated = bool(auth_info)
            except AccessDenied:
                authenticated = False

            if authenticated:
                raise ValueError(f"Insecure configuration: active password for {login_name} is 'admin'.")

        print("admin_default_password_active=false")
        """
    ).replace("__PAYLOAD__", json.dumps(script_payload))
    compose_exec_with_input(
        runtime_repo_path=runtime_repo_path,
        runtime_env_file=runtime_env_file,
        container_service="script-runner",
        container_command=odoo_shell_command,
        input_text=odoo_shell_script,
    )


def command_execution_env() -> dict[str, str]:
    execution_env = dict(os.environ)
    for runtime_key in PLATFORM_RUNTIME_ENV_KEYS:
        execution_env.pop(runtime_key, None)
    for passthrough_key in PLATFORM_RUNTIME_PASSTHROUGH_KEYS:
        execution_env.pop(passthrough_key, None)
    for environment_key in tuple(execution_env):
        if any(environment_key.startswith(prefix) for prefix in PLATFORM_RUNTIME_PASSTHROUGH_PREFIXES):
            execution_env.pop(environment_key, None)
    return execution_env


def clean_optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def extract_registry_host(image_reference: str) -> str | None:
    candidate = image_reference.strip()
    if not candidate:
        return None
    without_digest = candidate.split("@", 1)[0]
    first_segment = without_digest.split("/", 1)[0]
    if "." in first_segment or ":" in first_segment or first_segment == "localhost":
        return first_segment.lower()
    return None


def extract_registry_owner(image_reference: str) -> str | None:
    candidate = image_reference.strip()
    if not candidate:
        return None
    without_digest = candidate.split("@", 1)[0]
    first_segment, separator, remainder = without_digest.partition("/")
    if not separator:
        return None
    if not ("." in first_segment or ":" in first_segment or first_segment == "localhost"):
        return None
    owner, owner_separator, _package_name = remainder.partition("/")
    if owner_separator and owner:
        return owner
    return None


def resolve_base_images_for_build(environment_values: dict[str, str]) -> tuple[str, str]:
    runtime_image = clean_optional_value(environment_values.get("ODOO_BASE_RUNTIME_IMAGE")) or DEFAULT_ODOO_BASE_RUNTIME_IMAGE
    devtools_image = clean_optional_value(environment_values.get("ODOO_BASE_DEVTOOLS_IMAGE")) or DEFAULT_ODOO_BASE_DEVTOOLS_IMAGE
    return runtime_image, devtools_image


def require_configured_base_images_for_build(environment_values: dict[str, str]) -> list[str]:
    required_images = []
    for environment_key, image_reference in (
        ("ODOO_BASE_RUNTIME_IMAGE", resolve_base_images_for_build(environment_values)[0]),
        ("ODOO_BASE_DEVTOOLS_IMAGE", resolve_base_images_for_build(environment_values)[1]),
    ):
        registry_host = extract_registry_host(image_reference)
        if registry_host == PLACEHOLDER_REGISTRY_HOST:
            raise RuntimeCommandError(
                f"{environment_key} must be set to a real private base image before local builds run. "
                f"{runtime_environment_configuration_guidance(noun='it')} Do not keep it in checked-in public config."
            )
        if image_reference not in required_images:
            required_images.append(image_reference)
    return required_images


def resolve_ghcr_username(environment_values: dict[str, str], image_reference: str) -> str | None:
    candidates = (
        environment_values.get("GHCR_USERNAME"),
        os.environ.get("GHCR_USERNAME"),
        environment_values.get("GITHUB_ACTOR"),
        os.environ.get("GITHUB_ACTOR"),
        extract_registry_owner(image_reference),
    )
    for candidate in candidates:
        cleaned = clean_optional_value(candidate)
        if cleaned:
            return cleaned
    return None


def resolve_ghcr_push_token(environment_values: dict[str, str]) -> str | None:
    candidates = (
        environment_values.get("GHCR_TOKEN"),
        os.environ.get("GHCR_TOKEN"),
        environment_values.get("GITHUB_TOKEN"),
        os.environ.get("GITHUB_TOKEN"),
    )
    return first_clean_optional_value(candidates) or resolve_gh_auth_token()


def resolve_ghcr_read_token(environment_values: dict[str, str]) -> str | None:
    candidates = (
        environment_values.get("GHCR_READ_TOKEN"),
        os.environ.get("GHCR_READ_TOKEN"),
        environment_values.get("GHCR_TOKEN"),
        os.environ.get("GHCR_TOKEN"),
        environment_values.get("GITHUB_TOKEN"),
        os.environ.get("GITHUB_TOKEN"),
    )
    return first_clean_optional_value(candidates) or resolve_gh_auth_token()


def first_clean_optional_value(candidates: Iterable[str | None]) -> str | None:
    for candidate in candidates:
        cleaned = clean_optional_value(candidate)
        if cleaned:
            return cleaned
    return None


def resolve_gh_auth_token() -> str | None:
    gh_token_result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, env=command_execution_env())
    if gh_token_result.returncode == 0:
        gh_token = clean_optional_value(gh_token_result.stdout)
        if gh_token:
            return gh_token
    return None


def verify_base_image_access(image_reference: str) -> None:
    if image_reference in _VERIFIED_IMAGE_ACCESS:
        return
    inspect_result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image_reference], capture_output=True, text=True, env=command_execution_env()
    )
    if inspect_result.returncode != 0:
        details = clean_optional_value(inspect_result.stderr) or clean_optional_value(inspect_result.stdout)
        raise RuntimeCommandError(
            "Unable to read base image metadata for "
            f"'{image_reference}'. Ensure the GHCR token grants read access to the package."
            + (f"\nDocker reported: {details}" if details else "")
        )
    _VERIFIED_IMAGE_ACCESS.add(image_reference)


def ensure_registry_auth_for_base_images(environment_values: dict[str, str]) -> None:
    images = require_configured_base_images_for_build(environment_values)
    ghcr_images = [image for image in images if extract_registry_host(image) == GHCR_HOST]
    if not ghcr_images:
        return
    ghcr_username = resolve_ghcr_username(environment_values, ghcr_images[0])
    ghcr_token = resolve_ghcr_read_token(environment_values)
    if not ghcr_username:
        raise RuntimeCommandError(
            "Missing GHCR username for private base image pull. Set GHCR_USERNAME in resolved environment "
            f"({runtime_environment_configuration_guidance(noun='it')}) or provide GITHUB_ACTOR in the current shell."
        )
    if not ghcr_token:
        raise RuntimeCommandError(
            "Missing GHCR token for private base image pull. Set GHCR_READ_TOKEN (preferred), GHCR_TOKEN, "
            f"or GITHUB_TOKEN in resolved environment ({runtime_environment_configuration_guidance(noun='it')}) "
            "with read:packages access."
        )
    ensure_registry_login(registry_host=GHCR_HOST, username=ghcr_username, token=ghcr_token, purpose="read")
    for image in ghcr_images:
        verify_base_image_access(image)


def emit_key_value_payload(payload: dict[str, object], *, output_stream: TextIO) -> None:
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}={json.dumps(value)}", file=output_stream)
            continue
        print(f"{key}={value}", file=output_stream)


def _read_required_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a table")
    return value


def _read_optional_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a table when present")
    return value


def _ensure_table(value: object, *, scope: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeCommandError(f"Expected {scope} to be a table")
    return value


def _read_required_string(source: dict[str, object], key: str, *, scope: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a non-empty string")
    return value


def _read_optional_string(source: dict[str, object], key: str, *, scope: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a string when present")
    return value


def _read_required_int(source: dict[str, object], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise RuntimeCommandError(f"Expected {key} to be an integer")
    return value


def _read_string_tuple(source: dict[str, object], key: str, *, scope: str) -> tuple[str, ...]:
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a string array")
    return tuple(value)


def _read_optional_string_tuple(source: dict[str, object], key: str, *, scope: str) -> tuple[str, ...]:
    value = source.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a string array when present")
    return tuple(value)


def _read_optional_scalar_map(source: dict[str, object], key: str, *, scope: str) -> ScalarMap:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeCommandError(f"Expected {scope}.{key} to be a table when present")
    scalar_map: ScalarMap = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise RuntimeCommandError(f"Expected {scope}.{key} keys to be strings")
        if not isinstance(raw_value, (str, int, float, bool)):
            raise RuntimeCommandError(f"Expected {scope}.{key}.{raw_key} to be a scalar value")
        scalar_map[raw_key] = raw_value
    return scalar_map


def _read_optional_odoo_override_definition(source: dict[str, object], *, scope: str) -> OdooOverrideDefinition:
    override_table = _read_optional_table(source, "odoo_overrides", scope=scope)
    if not override_table:
        return empty_odoo_override_definition()

    config_parameters = _read_optional_scalar_map(
        override_table,
        "config_parameters",
        scope=f"{scope}.odoo_overrides",
    )
    raw_addon_settings = _read_optional_table(
        override_table,
        "addon_settings",
        scope=f"{scope}.odoo_overrides",
    )
    addon_settings: dict[str, ScalarMap] = {}
    for addon_name, raw_setting_values in raw_addon_settings.items():
        if not isinstance(addon_name, str) or not addon_name.strip():
            raise RuntimeCommandError(f"Expected {scope}.odoo_overrides.addon_settings addon names to be non-empty strings")
        if not isinstance(raw_setting_values, dict):
            raise RuntimeCommandError(f"Expected {scope}.odoo_overrides.addon_settings.{addon_name} to be a table")
        setting_values: ScalarMap = {}
        for setting_name, raw_setting_value in raw_setting_values.items():
            if not isinstance(setting_name, str) or not setting_name.strip():
                raise RuntimeCommandError(
                    f"Expected {scope}.odoo_overrides.addon_settings.{addon_name} setting names to be non-empty strings"
                )
            if not isinstance(raw_setting_value, (str, int, float, bool)):
                raise RuntimeCommandError(
                    f"Expected {scope}.odoo_overrides.addon_settings.{addon_name}.{setting_name} to be a scalar value"
                )
            setting_values[setting_name] = raw_setting_value
        addon_settings[addon_name.strip().lower()] = setting_values
    return OdooOverrideDefinition(
        config_parameters=config_parameters,
        addon_settings=addon_settings,
    )
