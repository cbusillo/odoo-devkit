from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manifest import WorkspaceManifest, load_workspace_manifest
from .runtime import (
    run_native_runtime_inspect,
    run_native_runtime_restore,
    run_native_runtime_select,
    run_native_runtime_up,
    run_native_runtime_workflow,
    run_runtime_platform_command,
)
from .scaffold import scaffold_tenant_overlay
from .workspace import clean_workspace, run_in_workspace, sync_workspace, workspace_status


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    if not hasattr(arguments, "handler"):
        parser.print_help()
        raise SystemExit(1)
    arguments.handler(arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platform")
    subparsers = parser.add_subparsers(dest="command")

    workspace_parser = subparsers.add_parser("workspace", help="Manage tenant-focused workspaces")
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command")

    sync_parser = _add_manifest_argument(workspace_subparsers.add_parser("sync", help="Assemble or refresh a workspace"))
    sync_parser.set_defaults(handler=_handle_workspace_sync)

    status_parser = _add_manifest_argument(workspace_subparsers.add_parser("status", help="Report workspace status"))
    status_parser.set_defaults(handler=_handle_workspace_status)

    clean_parser = _add_manifest_argument(workspace_subparsers.add_parser("clean", help="Delete the assembled workspace"))
    clean_parser.set_defaults(handler=_handle_workspace_clean)

    scaffold_parser = workspace_subparsers.add_parser(
        "scaffold-tenant-overlay",
        help="Copy the thin tenant-overlay starter into a target directory",
    )
    scaffold_parser.add_argument("--output-dir", type=Path, required=True)
    scaffold_parser.add_argument("--tenant", required=True)
    scaffold_parser.add_argument("--force", action="store_true")
    scaffold_parser.set_defaults(handler=_handle_workspace_scaffold_tenant_overlay)

    run_parser = _add_manifest_argument(workspace_subparsers.add_parser("run", help="Run a command inside the workspace"))
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(handler=_handle_workspace_run)

    runtime_parser = subparsers.add_parser("runtime", help="Run local runtime workflows via the workspace manifest")
    runtime_subparsers = runtime_parser.add_subparsers(dest="runtime_command")

    runtime_select_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("select", help="Run local platform select for the manifest runtime target")
    )
    runtime_select_parser.set_defaults(handler=_handle_runtime_select)

    runtime_up_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("up", help="Run local platform up for the manifest runtime target")
    )
    runtime_up_parser.add_argument("--build", dest="build_images", action=argparse.BooleanOptionalAction, default=True)
    runtime_up_parser.set_defaults(handler=_handle_runtime_up)

    runtime_workflow_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("workflow", help="Run a local platform workflow for the manifest runtime target")
    )
    runtime_workflow_parser.add_argument("--workflow", required=True)
    runtime_workflow_parser.set_defaults(handler=_handle_runtime_workflow)

    runtime_restore_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("restore", help="Run local platform restore for the manifest runtime target")
    )
    runtime_restore_parser.set_defaults(handler=_handle_runtime_restore)

    runtime_inspect_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("inspect", help="Run local platform inspect for the manifest runtime target")
    )
    runtime_inspect_parser.set_defaults(handler=_handle_runtime_inspect)
    return parser


def _add_manifest_argument(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--manifest", type=Path, default=Path("workspace.toml"))
    return parser


def _handle_workspace_sync(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    result = sync_workspace(manifest=manifest, devkit_repo_path=_discover_repo_root())
    summary = {
        "workspace_path": str(result.workspace_path),
        "lock_file_path": str(result.lock_file_path),
        "generated_odoo_conf_path": str(result.generated_odoo_conf_path),
        "runtime_env_path": str(result.runtime_env_path),
        "pycharm_metadata_path": str(result.pycharm_metadata_path),
        "workspace_agents_path": str(result.workspace_agents_path) if result.workspace_agents_path is not None else None,
        "workspace_docs_index_path": (
            str(result.workspace_docs_index_path) if result.workspace_docs_index_path is not None else None
        ),
        "materialized_sources": [str(path) for path in result.materialized_sources],
        "attached_paths": [str(path) for path in result.attached_paths],
        "run_configuration_paths": [str(path) for path in result.run_configuration_paths],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


def _handle_workspace_status(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    summary = workspace_status(manifest=manifest, devkit_repo_path=_discover_repo_root())
    print(json.dumps(summary, indent=2, sort_keys=True))


def _handle_workspace_clean(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    workspace_path = clean_workspace(manifest=manifest)
    print(json.dumps({"workspace_path": str(workspace_path), "removed": True}, indent=2, sort_keys=True))


def _handle_workspace_scaffold_tenant_overlay(arguments: argparse.Namespace) -> None:
    result = scaffold_tenant_overlay(
        repo_root=_discover_repo_root(),
        output_directory=arguments.output_dir.expanduser().resolve(),
        tenant=arguments.tenant,
        force=arguments.force,
    )
    print(
        json.dumps(
            {
                "output_directory": str(result.output_directory),
                "tenant": arguments.tenant,
                "written_paths": [str(path) for path in result.written_paths],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _handle_workspace_run(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    command = tuple(arguments.command)
    if command and command[0] == "--":
        command = command[1:]
    exit_code = run_in_workspace(manifest=manifest, command=command)
    raise SystemExit(exit_code)


def _handle_runtime_select(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    exit_code = run_native_runtime_select(manifest=manifest)
    raise SystemExit(exit_code)


def _handle_runtime_up(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    exit_code = run_native_runtime_up(manifest=manifest, build_images=arguments.build_images)
    raise SystemExit(exit_code)


def _handle_runtime_workflow(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    native_exit_code = run_native_runtime_workflow(manifest=manifest, workflow=arguments.workflow)
    if native_exit_code is not None:
        raise SystemExit(native_exit_code)
    exit_code = run_runtime_platform_command(
        manifest=manifest,
        platform_subcommand="run",
        platform_arguments=("--workflow", arguments.workflow),
    )
    raise SystemExit(exit_code)


def _handle_runtime_restore(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    native_exit_code = run_native_runtime_restore(manifest=manifest)
    if native_exit_code is not None:
        raise SystemExit(native_exit_code)
    exit_code = run_runtime_platform_command(manifest=manifest, platform_subcommand="restore")
    raise SystemExit(exit_code)


def _handle_runtime_inspect(arguments: argparse.Namespace) -> None:
    manifest = _load_manifest(arguments.manifest)
    exit_code = run_native_runtime_inspect(manifest=manifest)
    raise SystemExit(exit_code)


def _load_manifest(manifest_path: Path) -> WorkspaceManifest:
    resolved_manifest_path = manifest_path.expanduser().resolve()
    if not resolved_manifest_path.exists():
        raise SystemExit(f"Manifest not found: {resolved_manifest_path}")
    try:
        return load_workspace_manifest(resolved_manifest_path)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _discover_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    main()
