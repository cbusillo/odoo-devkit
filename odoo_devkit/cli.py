from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .manifest import WorkspaceManifest, load_workspace_manifest
from .runtime import (
    run_native_runtime_build,
    run_native_runtime_down,
    run_native_runtime_inspect,
    run_native_runtime_logs,
    run_native_runtime_odoo_shell,
    run_native_runtime_psql,
    run_native_runtime_publish,
    run_native_runtime_restore,
    run_native_runtime_select,
    run_native_runtime_up,
    run_native_runtime_workflow,
    run_runtime_platform_command,
)
from .scaffold import scaffold_tenant_overlay, scaffold_workspace_cockpit
from .workspace import clean_workspace, run_in_workspace, sync_workspace, workspace_status
from .workspace_cockpit import load_workspace_cockpit_manifest, sync_workspace_cockpit, workspace_cockpit_status


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

    cockpit_parser = workspace_subparsers.add_parser(
        "scaffold-cockpit-root",
        help="Copy the shared manual workspace-cockpit root starter into a target directory",
    )
    cockpit_parser.add_argument("--output-dir", type=Path, required=True)
    cockpit_parser.add_argument("--force", action="store_true")
    cockpit_parser.set_defaults(handler=_handle_workspace_scaffold_cockpit_root)

    sync_cockpit_parser = workspace_subparsers.add_parser(
        "sync-cockpit-root",
        help="Regenerate a manual multi-repo cockpit root from workspace-cockpit.toml",
    )
    sync_cockpit_parser.add_argument("--config", type=Path, default=Path("workspace-cockpit.toml"))
    sync_cockpit_parser.set_defaults(handler=_handle_workspace_sync_cockpit_root)

    status_cockpit_parser = workspace_subparsers.add_parser(
        "status-cockpit-root",
        help="Report whether a manual cockpit root matches workspace-cockpit.toml",
    )
    status_cockpit_parser.add_argument("--config", type=Path, default=Path("workspace-cockpit.toml"))
    status_cockpit_parser.set_defaults(handler=_handle_workspace_status_cockpit_root)

    run_parser = _add_manifest_argument(workspace_subparsers.add_parser("run", help="Run a command inside the workspace"))
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(handler=_handle_workspace_run)

    runtime_parser = subparsers.add_parser("runtime", help="Run local runtime workflows via the workspace manifest")
    runtime_subparsers = runtime_parser.add_subparsers(dest="runtime_command")

    runtime_select_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("select", help="Run local platform select for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_select_parser)
    runtime_select_parser.set_defaults(handler=_handle_runtime_select)

    runtime_up_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("up", help="Run local platform up for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_up_parser)
    runtime_up_parser.add_argument("--build", dest="build_images", action=argparse.BooleanOptionalAction, default=True)
    runtime_up_parser.set_defaults(handler=_handle_runtime_up)

    runtime_build_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("build", help="Build the local manifest runtime images")
    )
    _add_runtime_instance_override_argument(runtime_build_parser)
    runtime_build_parser.add_argument("--no-cache", action="store_true")
    runtime_build_parser.set_defaults(handler=_handle_runtime_build)

    runtime_publish_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("publish", help="Build and publish a release artifact image from the manifest runtime inputs")
    )
    _add_runtime_instance_override_argument(runtime_publish_parser)
    runtime_publish_parser.add_argument("--image-repository", required=True)
    runtime_publish_parser.add_argument("--image-tag", required=True)
    runtime_publish_parser.add_argument("--output-file", type=Path, default=None)
    runtime_publish_parser.add_argument("--no-cache", action="store_true")
    runtime_publish_parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help="Target platform for artifact image builds. May be provided more than once; defaults to linux/amd64 and linux/arm64.",
    )
    runtime_publish_parser.set_defaults(handler=_handle_runtime_publish)

    runtime_down_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("down", help="Stop the local manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_down_parser)
    runtime_down_parser.add_argument("--volumes", action="store_true")
    runtime_down_parser.set_defaults(handler=_handle_runtime_down)

    runtime_workflow_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("workflow", help="Run a local platform workflow for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_workflow_parser)
    runtime_workflow_parser.add_argument("--workflow", required=True)
    runtime_workflow_parser.set_defaults(handler=_handle_runtime_workflow)

    runtime_restore_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("restore", help="Run local platform restore for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_restore_parser)
    runtime_restore_parser.set_defaults(handler=_handle_runtime_restore)

    runtime_inspect_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("inspect", help="Run local platform inspect for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_inspect_parser)
    runtime_inspect_parser.set_defaults(handler=_handle_runtime_inspect)

    runtime_logs_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("logs", help="Stream local runtime logs for the manifest runtime target")
    )
    _add_runtime_instance_override_argument(runtime_logs_parser)
    runtime_logs_parser.add_argument("--service", default="web")
    runtime_logs_parser.add_argument("--follow", action=argparse.BooleanOptionalAction, default=True)
    runtime_logs_parser.add_argument("--lines", type=_non_negative_int, default=200)
    runtime_logs_parser.set_defaults(handler=_handle_runtime_logs)

    runtime_psql_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("psql", help="Run psql against the local manifest runtime database")
    )
    _add_runtime_instance_override_argument(runtime_psql_parser)
    runtime_psql_parser.add_argument("psql_arguments", nargs=argparse.REMAINDER)
    runtime_psql_parser.set_defaults(handler=_handle_runtime_psql)

    runtime_odoo_shell_parser = _add_manifest_argument(
        runtime_subparsers.add_parser("odoo-shell", help="Run Odoo shell against the local manifest runtime database")
    )
    _add_runtime_instance_override_argument(runtime_odoo_shell_parser)
    runtime_odoo_shell_parser.add_argument("--script", dest="script_path", type=Path, default=None)
    runtime_odoo_shell_parser.add_argument("--service", default="script-runner")
    runtime_odoo_shell_parser.add_argument("--database", dest="database_name", default=None)
    runtime_odoo_shell_parser.add_argument("--log-file", dest="log_file", type=Path, default=None)
    runtime_odoo_shell_parser.add_argument("--dry-run", action="store_true")
    runtime_odoo_shell_parser.set_defaults(handler=_handle_runtime_odoo_shell)
    return parser


def _add_manifest_argument(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--manifest", type=Path, default=Path("workspace.toml"))
    return parser


def _add_runtime_instance_override_argument(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--instance",
        dest="runtime_instance",
        help="Override the manifest runtime instance for this command.",
    )
    return parser


def _non_negative_int(value: str) -> int:
    parsed_value = int(value)
    if parsed_value < 0:
        raise argparse.ArgumentTypeError("Value must be zero or greater.")
    return parsed_value


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
        "workspace_session_prompt_path": (
            str(result.workspace_session_prompt_path) if result.workspace_session_prompt_path is not None else None
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


def _handle_workspace_scaffold_cockpit_root(arguments: argparse.Namespace) -> None:
    result = scaffold_workspace_cockpit(
        repo_root=_discover_repo_root(),
        output_directory=arguments.output_dir.expanduser().resolve(),
        force=arguments.force,
    )
    print(
        json.dumps(
            {
                "output_directory": str(result.output_directory),
                "written_paths": [str(path) for path in result.written_paths],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _handle_workspace_sync_cockpit_root(arguments: argparse.Namespace) -> None:
    manifest_path = arguments.config.expanduser().resolve()
    result = sync_workspace_cockpit(
        manifest=load_workspace_cockpit_manifest(manifest_path),
        output_directory=manifest_path.parent,
        overwrite_existing=True,
    )
    print(
        json.dumps(
            {
                "manifest_path": str(result.manifest_path),
                "output_directory": str(result.output_directory),
                "written_paths": [str(path) for path in result.written_paths],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _handle_workspace_status_cockpit_root(arguments: argparse.Namespace) -> None:
    manifest_path = arguments.config.expanduser().resolve()
    result = workspace_cockpit_status(
        manifest=load_workspace_cockpit_manifest(manifest_path),
        output_directory=manifest_path.parent,
    )
    print(
        json.dumps(
            {
                "manifest_path": str(result.manifest_path),
                "output_directory": str(result.output_directory),
                "is_current": result.is_current,
                "file_statuses": [
                    {
                        "path": str(file_status.path),
                        "exists": file_status.exists,
                        "matches_expected": file_status.matches_expected,
                    }
                    for file_status in result.file_statuses
                ],
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
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(lambda: run_native_runtime_select(manifest=manifest))
    raise SystemExit(exit_code)


def _handle_runtime_up(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(lambda: run_native_runtime_up(manifest=manifest, build_images=arguments.build_images))
    raise SystemExit(exit_code)


def _handle_runtime_build(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(lambda: run_native_runtime_build(manifest=manifest, no_cache=arguments.no_cache))
    raise SystemExit(exit_code)


def _handle_runtime_publish(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    payload = _run_runtime_handler(
        lambda: run_native_runtime_publish(
            manifest=manifest,
            image_repository=arguments.image_repository,
            image_tag=arguments.image_tag,
            output_file=arguments.output_file,
            no_cache=arguments.no_cache,
            platforms=tuple(arguments.platform or ()),
        )
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


def _handle_runtime_down(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(lambda: run_native_runtime_down(manifest=manifest, volumes=arguments.volumes))
    raise SystemExit(exit_code)


def _handle_runtime_workflow(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    native_exit_code = _run_runtime_handler(lambda: run_native_runtime_workflow(manifest=manifest, workflow=arguments.workflow))
    if native_exit_code is not None:
        raise SystemExit(native_exit_code)
    exit_code = run_runtime_platform_command(
        manifest=manifest,
        platform_subcommand="run",
        platform_arguments=("--workflow", arguments.workflow),
    )
    raise SystemExit(exit_code)


def _handle_runtime_restore(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    native_exit_code = _run_runtime_handler(lambda: run_native_runtime_restore(manifest=manifest))
    if native_exit_code is not None:
        raise SystemExit(native_exit_code)
    exit_code = run_runtime_platform_command(manifest=manifest, platform_subcommand="restore")
    raise SystemExit(exit_code)


def _handle_runtime_inspect(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(lambda: run_native_runtime_inspect(manifest=manifest))
    raise SystemExit(exit_code)


def _handle_runtime_logs(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(
        lambda: run_native_runtime_logs(
            manifest=manifest,
            service=arguments.service,
            tail_lines=arguments.lines,
            follow=arguments.follow,
        )
    )
    raise SystemExit(exit_code)


def _handle_runtime_psql(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    psql_arguments = tuple(arguments.psql_arguments)
    if psql_arguments and psql_arguments[0] == "--":
        psql_arguments = psql_arguments[1:]
    exit_code = _run_runtime_handler(
        lambda: run_native_runtime_psql(
            manifest=manifest,
            psql_arguments=psql_arguments,
        )
    )
    raise SystemExit(exit_code)


def _handle_runtime_odoo_shell(arguments: argparse.Namespace) -> None:
    manifest = _load_runtime_manifest(arguments)
    exit_code = _run_runtime_handler(
        lambda: run_native_runtime_odoo_shell(
            manifest=manifest,
            service=arguments.service,
            database_name=arguments.database_name,
            script_path=arguments.script_path,
            log_file=arguments.log_file,
            dry_run=arguments.dry_run,
        )
    )
    raise SystemExit(exit_code)


def _load_runtime_manifest(arguments: argparse.Namespace) -> WorkspaceManifest:
    manifest = _load_manifest(arguments.manifest)
    runtime_instance_override = getattr(arguments, "runtime_instance", None)
    if runtime_instance_override is None:
        return manifest
    normalized_runtime_instance = runtime_instance_override.strip().lower()
    if not normalized_runtime_instance:
        raise SystemExit("Runtime instance override must be a non-empty value.")
    return replace(
        manifest,
        runtime=replace(
            manifest.runtime,
            instance=normalized_runtime_instance,
        ),
    )


def _run_runtime_handler(handler: Callable[[], object]) -> object:
    try:
        return handler()
    except ValueError as error:
        raise SystemExit(str(error)) from error


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
