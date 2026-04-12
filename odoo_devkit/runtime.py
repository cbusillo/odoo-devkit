from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .local_runtime import (
    RuntimeCommandError,
    emit_key_value_payload,
    inspect_runtime,
    run_bootstrap_workflow,
    run_init_workflow,
    run_openupgrade_workflow,
    run_restore_workflow,
    run_update_workflow,
    select_runtime,
    up_runtime,
)
from .manifest import WorkspaceManifest
from .remote_runtime import run_remote_bootstrap_workflow, run_remote_restore_workflow, run_remote_update_workflow

LOCAL_ONLY_NATIVE_WORKFLOWS = frozenset({"init", "openupgrade"})


def runtime_target_is_local(manifest: WorkspaceManifest) -> bool:
    return manifest.runtime.instance == "local"


def _raise_local_only_workflow_error(*, workflow: str, manifest: WorkspaceManifest) -> None:
    raise ValueError(
        f"workflow {workflow!r} manages local host runtime only and requires --instance local. "
        f"Received {manifest.runtime.context}/{manifest.runtime.instance}."
    )


def resolve_runtime_repo_path(manifest: WorkspaceManifest) -> Path:
    explicit_runtime_repo = manifest.runtime_repo
    if explicit_runtime_repo is not None:
        runtime_repo_path = explicit_runtime_repo.resolve_path(manifest_directory=manifest.manifest_directory)
        if runtime_repo_path is not None:
            if not runtime_repo_path.exists():
                raise ValueError(f"Runtime repo path does not exist: {runtime_repo_path}")
            return runtime_repo_path

        if explicit_runtime_repo.url is not None:
            managed_runtime_repo_path = _resolve_managed_runtime_repo_path(manifest)
            if managed_runtime_repo_path is not None:
                return managed_runtime_repo_path
            if not explicit_runtime_repo.ref:
                raise ValueError(
                    f"Runtime repo must declare ref when workspace sync materializes it from url {explicit_runtime_repo.url!r}."
                )
            raise ValueError(
                "Runtime repo is repo-addressable and must be materialized by `platform workspace sync` before "
                "runtime commands can run."
            )

        raise ValueError("Runtime repo must declare a path or url for the current bootstrap flow")

    if runtime_target_is_local(manifest):
        return _discover_devkit_repo_root()

    raise ValueError(
        "Workspace manifest must declare [repos.runtime] for non-local runtime commands. "
        "Runtime ownership is explicit and is not inferred from [repos.shared_addons]."
    )


def _resolve_managed_runtime_repo_path(manifest: WorkspaceManifest) -> Path | None:
    runtime_repo_definition = manifest.runtime_repo
    if runtime_repo_definition is None or runtime_repo_definition.url is None:
        return None
    from .workspace import resolve_optional_repo_path_with_managed_checkout, resolve_workspace_path

    workspace_path = resolve_workspace_path(manifest)
    return resolve_optional_repo_path_with_managed_checkout(
        runtime_repo_definition,
        manifest=manifest,
        managed_checkout_path=workspace_path / "sources" / "runtime",
    )


def _discover_devkit_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_runtime_platform_command(
    *,
    manifest: WorkspaceManifest,
    platform_subcommand: str,
    platform_arguments: tuple[str, ...] = (),
) -> tuple[str, ...]:
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    return (
        "uv",
        "--directory",
        str(runtime_repo_path),
        "run",
        "platform",
        platform_subcommand,
        "--context",
        manifest.runtime.context,
        "--instance",
        manifest.runtime.instance,
        *platform_arguments,
    )


def run_runtime_platform_command(
    *,
    manifest: WorkspaceManifest,
    platform_subcommand: str,
    platform_arguments: tuple[str, ...] = (),
) -> int:
    command = build_runtime_platform_command(
        manifest=manifest,
        platform_subcommand=platform_subcommand,
        platform_arguments=platform_arguments,
    )
    completed_process = subprocess.run(command, cwd=manifest.manifest_directory, check=False)
    return completed_process.returncode


def run_native_runtime_select(*, manifest: WorkspaceManifest) -> int:
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    try:
        result = select_runtime(manifest=manifest, runtime_repo_path=runtime_repo_path)
    except RuntimeCommandError as error:
        raise ValueError(str(error)) from error
    print(f"selected_context={manifest.runtime.context}")
    print(f"selected_instance={manifest.runtime.instance}")
    print(f"runtime_env_file={result.runtime_env_file}")
    print(f"pycharm_odoo_conf_file={result.pycharm_odoo_conf_file}")
    return 0


def run_native_runtime_inspect(*, manifest: WorkspaceManifest) -> int:
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    try:
        result = inspect_runtime(manifest=manifest, runtime_repo_path=runtime_repo_path)
    except RuntimeCommandError as error:
        raise ValueError(str(error)) from error
    emit_key_value_payload(result.payload, output_stream=sys.stdout)
    return 0


def run_native_runtime_up(*, manifest: WorkspaceManifest, build_images: bool) -> int:
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    try:
        up_runtime(manifest=manifest, runtime_repo_path=runtime_repo_path, build_images=build_images)
    except RuntimeCommandError as error:
        raise ValueError(str(error)) from error
    print(f"up=odoo-{manifest.runtime.context}-{manifest.runtime.instance}")
    return 0


def run_native_runtime_workflow(*, manifest: WorkspaceManifest, workflow: str) -> int | None:
    normalized_workflow = workflow.strip().lower()
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    local_runtime_target = runtime_target_is_local(manifest)
    try:
        if normalized_workflow in LOCAL_ONLY_NATIVE_WORKFLOWS and not local_runtime_target:
            _raise_local_only_workflow_error(workflow=normalized_workflow, manifest=manifest)
        if normalized_workflow == "bootstrap":
            if local_runtime_target:
                run_bootstrap_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            else:
                run_remote_bootstrap_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            print(f"bootstrap={manifest.runtime.context}-{manifest.runtime.instance}")
            print("workflow=bootstrap")
            return 0
        if normalized_workflow == "init":
            run_init_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            print(f"init=odoo-{manifest.runtime.context}-{manifest.runtime.instance}")
            print("workflow=init")
            return 0
        if normalized_workflow == "update":
            if local_runtime_target:
                run_update_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            else:
                run_remote_update_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            print(f"update={manifest.runtime.context}-{manifest.runtime.instance}")
            print("workflow=update")
            return 0
        if normalized_workflow == "openupgrade":
            run_openupgrade_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
            print(f"openupgrade={manifest.runtime.context}-{manifest.runtime.instance}")
            print("workflow=openupgrade")
            return 0
    except RuntimeCommandError as error:
        raise ValueError(str(error)) from error
    return None


def run_native_runtime_restore(*, manifest: WorkspaceManifest) -> int | None:
    runtime_repo_path = resolve_runtime_repo_path(manifest)
    try:
        if runtime_target_is_local(manifest):
            run_restore_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
        else:
            run_remote_restore_workflow(manifest=manifest, runtime_repo_path=runtime_repo_path)
    except RuntimeCommandError as error:
        raise ValueError(str(error)) from error
    print(f"restore={manifest.runtime.context}-{manifest.runtime.instance}")
    return 0
