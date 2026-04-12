from __future__ import annotations

import logging
import shlex
from pathlib import Path

from .dokploy_api import (
    DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS,
    DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES,
    DOKPLOY_RUNNING_DEPLOYMENT_STATUSES,
    DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES,
    JsonObject,
    as_json_object,
    deployment_key,
    deployment_status,
    dokploy_request,
    fetch_dokploy_target_payload,
    find_matching_dokploy_schedule,
    latest_deployment_for_compose,
    latest_deployment_for_schedule,
    parse_dokploy_env_text,
    resolve_dokploy_user_id,
    schedule_key,
    serialize_dokploy_env_text,
    update_dokploy_target_env,
    upsert_dokploy_schedule,
    wait_for_dokploy_compose_deployment,
    wait_for_dokploy_schedule_deployment,
)
from .dokploy_config import (
    DokployTargetDefinition,
    find_dokploy_target_definition,
    load_dokploy_source_of_truth,
)
from .local_runtime import (
    RuntimeCommandError,
    RuntimeContext,
    load_runtime_context,
    missing_upstream_source_keys,
    parse_env_file,
    resolve_data_workflow_environment,
    runtime_environment_configuration_guidance,
    write_runtime_env_file,
    write_runtime_odoo_conf_file,
)
from .manifest import WorkspaceManifest

DATA_WORKFLOW_SCRIPT = "/volumes/scripts/run_odoo_data_workflows.py"
DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME = "platform-data-workflow"
DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION = "0 0 31 2 *"

_logger = logging.getLogger(__name__)


def run_remote_restore_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_remote_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=False,
        no_sanitize=no_sanitize,
        update_only=False,
    )


def run_remote_bootstrap_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_remote_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=True,
        no_sanitize=no_sanitize,
        update_only=False,
    )


def run_remote_update_workflow(*, manifest: WorkspaceManifest, runtime_repo_path: Path, no_sanitize: bool = False) -> None:
    run_remote_data_workflow(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        bootstrap=False,
        no_sanitize=no_sanitize,
        update_only=True,
    )


def run_remote_data_workflow(
    *,
    manifest: WorkspaceManifest,
    runtime_repo_path: Path,
    bootstrap: bool,
    no_sanitize: bool,
    update_only: bool,
) -> None:
    runtime_context = load_runtime_context(
        manifest=manifest,
        runtime_repo_path=runtime_repo_path,
        require_local_instance=False,
    )
    write_runtime_odoo_conf_file(
        runtime_selection=runtime_context.selection,
        stack_definition=runtime_context.stack.stack_definition,
        source_environment=runtime_context.environment.merged_values,
    )
    runtime_env_file = write_runtime_env_file(runtime_context=runtime_context)
    resolved_environment = resolve_data_workflow_environment(parse_env_file(runtime_env_file))
    if not bootstrap and not update_only:
        missing_environment_keys = missing_upstream_source_keys(resolved_environment)
        if missing_environment_keys:
            missing_joined = ", ".join(missing_environment_keys)
            raise RuntimeCommandError(
                "Restore requires upstream settings; missing: "
                f"{missing_joined}. {runtime_environment_configuration_guidance()} "
                "or run bootstrap intentionally."
            )

    _run_dokploy_managed_remote_data_workflow(
        runtime_context=runtime_context,
        env_values=resolved_environment,
        bootstrap=bootstrap,
        no_sanitize=no_sanitize,
        update_only=update_only,
    )


def _resolve_required_dokploy_compose_target_definition(
    runtime_context: RuntimeContext,
) -> DokployTargetDefinition:
    source_of_truth = load_dokploy_source_of_truth(runtime_context.repo_root)
    if source_of_truth is None:
        raise RuntimeCommandError("Dokploy-managed remote workflows require platform/dokploy.toml with pinned target metadata.")

    target_definition = find_dokploy_target_definition(
        source_of_truth,
        context_name=runtime_context.selection.context_name,
        instance_name=runtime_context.selection.instance_name,
    )
    if target_definition is None:
        raise RuntimeCommandError(
            "Dokploy-managed remote workflow requires a target definition in platform/dokploy.toml for "
            f"{runtime_context.selection.context_name}/{runtime_context.selection.instance_name}."
        )
    if target_definition.target_type != "compose":
        raise RuntimeCommandError(
            "Dokploy-managed remote data workflows require compose targets, but "
            f"platform/dokploy.toml configures {runtime_context.selection.context_name}/"
            f"{runtime_context.selection.instance_name} as '{target_definition.target_type}'."
        )
    compose_id = target_definition.target_id.strip()
    if not compose_id:
        raise RuntimeCommandError(
            "Dokploy-managed remote workflow requires a pinned target_id in platform/dokploy.toml for "
            f"{runtime_context.selection.context_name}/{runtime_context.selection.instance_name}."
        )
    return target_definition


def _resolve_dokploy_schedule_runtime(
    *,
    dokploy_host: str,
    dokploy_token: str,
    compose_id: str,
    compose_name: str,
) -> tuple[str, str, str, str | None]:
    compose_payload = dokploy_request(
        host=dokploy_host,
        token=dokploy_token,
        path="/api/compose.one",
        query={"composeId": compose_id},
    )
    compose_payload_as_object = as_json_object(compose_payload)
    if compose_payload_as_object is None:
        raise RuntimeCommandError(f"Dokploy compose.one returned an invalid response for compose {compose_name!r}.")

    compose_app_name = str(compose_payload_as_object.get("appName") or "").strip()
    if not compose_app_name:
        raise RuntimeCommandError(f"Dokploy compose {compose_name!r} ({compose_id}) has no appName in API response.")

    compose_server_id = str(compose_payload_as_object.get("serverId") or "").strip()
    if compose_server_id:
        return "server", compose_server_id, compose_app_name, compose_server_id

    user_id = resolve_dokploy_user_id(host=dokploy_host, token=dokploy_token)
    return "dokploy-server", user_id, compose_app_name, None


def _build_dokploy_data_workflow_script(
    *,
    compose_app_name: str,
    database_name: str,
    filestore_path: str = "/volumes/data/filestore",
    bootstrap: bool,
    no_sanitize: bool,
    update_only: bool,
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    workflow_arguments: list[str] = []
    if bootstrap:
        workflow_arguments.append("--bootstrap")
    if no_sanitize:
        workflow_arguments.append("--no-sanitize")
    if update_only:
        workflow_arguments.append("--update-only")

    quoted_workflow_arguments = " ".join(shlex.quote(argument) for argument in workflow_arguments)
    workflow_argument_line = (
        f"workflow_arguments=({quoted_workflow_arguments})" if quoted_workflow_arguments else "workflow_arguments=()"
    )
    clear_stale_lock_line = f"clear_stale_lock={'1' if clear_stale_lock else '0'}"

    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={shlex.quote(compose_app_name)}
database_name={shlex.quote(database_name)}
filestore_root={shlex.quote(normalized_filestore_path)}
workflow_ssh_dir=/tmp/platform-data-workflow-ssh
{workflow_argument_line}
{clear_stale_lock_line}
data_workflow_lock_path={shlex.quote(data_workflow_lock_path)}

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

start_web_container() {{
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}" 2>/dev/null || true)
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null || true
    fi
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")

ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"
workflow_uid=$(docker exec "${{script_runner_container_id}}" id -u)
workflow_gid=$(docker exec "${{script_runner_container_id}}" id -g)

if [ "${{clear_stale_lock}}" = "1" ]; then
    echo "Clearing stale data workflow lock ${{data_workflow_lock_path}}"
    docker exec -u root "${{script_runner_container_id}}" rm -f "${{data_workflow_lock_path}}"
fi

trap start_web_container EXIT

web_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")
if [ "${{web_status}}" = "running" ]; then
    echo "Stopping web container ${{web_container_id}}"
    docker stop "${{web_container_id}}" >/dev/null
fi

echo "Normalizing filestore ownership for ${{database_name}}"
workflow_identity_key=$(docker exec -u root \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e DATA_WORKFLOW_SSH_DIR="${{DATA_WORKFLOW_SSH_DIR:-/root/.ssh}}" \
    -e DATA_WORKFLOW_SSH_KEY="${{DATA_WORKFLOW_SSH_KEY:-}}" \
    -e WORKFLOW_UID="${{workflow_uid}}" \
    -e WORKFLOW_GID="${{workflow_gid}}" \
    -e WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        target_owner=$(stat -c "%u:%g" /volumes/data)
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        mkdir -p "$ODOO_FILESTORE_ROOT" "$filestore_database_path"
        chown -R "$target_owner" "$filestore_database_path"
        chmod -R ug+rwX "$filestore_database_path"

        rm -rf "$WORKFLOW_SSH_DIR"
        install -d -m 700 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" "$WORKFLOW_SSH_DIR"

        if [ -f "$DATA_WORKFLOW_SSH_DIR/known_hosts" ]; then
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$DATA_WORKFLOW_SSH_DIR/known_hosts" "$WORKFLOW_SSH_DIR/known_hosts"
        fi

        source_key_path="$DATA_WORKFLOW_SSH_KEY"
        if [ -z "$source_key_path" ]; then
            for candidate_key in id_ed25519 id_ecdsa id_rsa id_dsa; do
                if [ -f "$DATA_WORKFLOW_SSH_DIR/$candidate_key" ]; then
                    source_key_path="$DATA_WORKFLOW_SSH_DIR/$candidate_key"
                    break
                fi
            done
        fi
        workflow_identity_key=""
        if [ -n "$source_key_path" ] && [ -f "$source_key_path" ]; then
            workflow_identity_key="$WORKFLOW_SSH_DIR/$(basename "$source_key_path")"
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$source_key_path" "$workflow_identity_key"
        fi
        printf "%s" "$workflow_identity_key"
    ')

echo "Running platform data workflow in container ${{script_runner_container_id}}"
docker exec \
    -e DATA_WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    -e DATA_WORKFLOW_SSH_KEY="$workflow_identity_key" \
    "${{script_runner_container_id}}" \
    python3 -u {shlex.quote(DATA_WORKFLOW_SCRIPT)} "${{workflow_arguments[@]}}"

start_web_container
trap - EXIT
"""


def _schedule_deployments(schedule: JsonObject | None) -> tuple[JsonObject, ...]:
    if not isinstance(schedule, dict):
        return ()
    raw_deployments = schedule.get("deployments")
    if not isinstance(raw_deployments, list):
        return ()
    deployment_entries: list[JsonObject] = []
    for raw_deployment in raw_deployments:
        if isinstance(raw_deployment, dict):
            deployment_entries.append(raw_deployment)
    return tuple(deployment_entries)


def _deployment_status_value(deployment: JsonObject) -> str:
    return str(deployment.get("status") or "").strip().lower()


def _has_running_schedule_deployment(schedule: JsonObject | None) -> bool:
    return any(
        _deployment_status_value(deployment) in DOKPLOY_RUNNING_DEPLOYMENT_STATUSES for deployment in _schedule_deployments(schedule)
    )


def _should_clear_stale_data_workflow_lock(schedule: JsonObject | None) -> bool:
    deployments = _schedule_deployments(schedule)
    if not deployments or _has_running_schedule_deployment(schedule):
        return False
    for deployment in deployments:
        deployment_status_value = _deployment_status_value(deployment)
        if deployment_status_value in DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES:
            return True
        if deployment_status_value in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
            return False
    return False


def _sync_dokploy_target_environment_and_deploy(
    *,
    dokploy_host: str,
    dokploy_token: str,
    target_definition: DokployTargetDefinition,
    env_values: dict[str, str],
    deploy_timeout_seconds: int,
) -> None:
    compose_id = target_definition.target_id.strip()
    compose_name = target_definition.target_name.strip() or f"{target_definition.context}-{target_definition.instance}"
    target_payload = fetch_dokploy_target_payload(
        host=dokploy_host,
        token=dokploy_token,
        target_type="compose",
        target_id=compose_id,
    )
    current_env_map = parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = dict(current_env_map)
    updated_environment_keys: list[str] = []
    for environment_key, environment_value in env_values.items():
        if desired_env_map.get(environment_key) == environment_value:
            continue
        desired_env_map[environment_key] = environment_value
        updated_environment_keys.append(environment_key)

    if updated_environment_keys:
        update_dokploy_target_env(
            host=dokploy_host,
            token=dokploy_token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=serialize_dokploy_env_text(desired_env_map),
        )
        _logger.info(
            "Updated Dokploy compose env for %s with %s key(s): %s",
            compose_name,
            len(updated_environment_keys),
            ",".join(sorted(updated_environment_keys)),
        )
        latest_compose_deployment = latest_deployment_for_compose(dokploy_host, dokploy_token, compose_id)
        previous_deployment_key = deployment_key(latest_compose_deployment or {})
        dokploy_request(
            host=dokploy_host,
            token=dokploy_token,
            path="/api/compose.deploy",
            method="POST",
            payload={"composeId": compose_id},
            timeout_seconds=deploy_timeout_seconds,
        )
        deployment_result = wait_for_dokploy_compose_deployment(
            host=dokploy_host,
            token=dokploy_token,
            compose_id=compose_id,
            before_key=previous_deployment_key,
            timeout_seconds=deploy_timeout_seconds,
        )
        _logger.info("Dokploy compose deployment completed before data workflow: %s", deployment_result)
        return

    _logger.info("Dokploy compose env already matched generated workflow env for %s; skipping pre-workflow deploy", compose_name)


def _run_dokploy_managed_remote_data_workflow(
    *,
    runtime_context: RuntimeContext,
    env_values: dict[str, str],
    bootstrap: bool,
    no_sanitize: bool,
    update_only: bool,
) -> int:
    dokploy_host = env_values.get("DOKPLOY_HOST", "").strip()
    dokploy_token = env_values.get("DOKPLOY_TOKEN", "").strip()
    if not dokploy_host or not dokploy_token:
        raise RuntimeCommandError(
            "Dokploy remote data workflow requires DOKPLOY_HOST and DOKPLOY_TOKEN "
            f"in the resolved environment. {runtime_environment_configuration_guidance()}"
        )

    target_definition = _resolve_required_dokploy_compose_target_definition(runtime_context)
    context_name = runtime_context.selection.context_name
    instance_name = runtime_context.selection.instance_name
    stack_name = f"{context_name}-{instance_name}"
    compose_id = target_definition.target_id.strip()
    compose_name = target_definition.target_name.strip() or stack_name
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = _resolve_dokploy_schedule_runtime(
        dokploy_host=dokploy_host,
        dokploy_token=dokploy_token,
        compose_id=compose_id,
        compose_name=compose_name,
    )
    schedule_app_name = _build_dokploy_data_workflow_schedule_app_name(
        context_name=context_name,
        instance_name=instance_name,
    )
    database_name = env_values.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise RuntimeCommandError(
            "Dokploy-managed remote data workflow requires ODOO_DB_NAME in the resolved environment. "
            f"Missing database name for {context_name}/{instance_name}."
        )
    filestore_path = (env_values.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore").strip() or "/volumes/data/filestore"
    schedule_timeout_seconds = target_definition.deploy_timeout_seconds or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    _sync_dokploy_target_environment_and_deploy(
        dokploy_host=dokploy_host,
        dokploy_token=dokploy_token,
        target_definition=target_definition,
        env_values=env_values,
        deploy_timeout_seconds=schedule_timeout_seconds,
    )
    existing_schedule = find_matching_dokploy_schedule(
        host=dokploy_host,
        token=dokploy_token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME,
        app_name=schedule_app_name,
    )
    if _has_running_schedule_deployment(existing_schedule):
        raise RuntimeCommandError(
            f"Dokploy-managed data workflow already has a running schedule deployment for {context_name}/{instance_name}."
        )
    schedule_payload: JsonObject = {
        "name": DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "platform data workflow",
        "script": _build_dokploy_data_workflow_script(
            compose_app_name=compose_app_name,
            database_name=database_name,
            filestore_path=filestore_path,
            bootstrap=bootstrap,
            no_sanitize=no_sanitize,
            update_only=update_only,
            clear_stale_lock=_should_clear_stale_data_workflow_lock(existing_schedule),
            data_workflow_lock_path=env_values.get("ODOO_DATA_WORKFLOW_LOCK_FILE", "/volumes/data/.data_workflow_in_progress"),
        ),
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = upsert_dokploy_schedule(
        host=dokploy_host,
        token=dokploy_token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = schedule_key(schedule)
    if not schedule_id:
        raise RuntimeCommandError(
            f"Dokploy schedule {DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME!r} for {context_name}/{instance_name} did not expose a schedule id."
        )

    latest_schedule_deployment = latest_deployment_for_schedule(dokploy_host, dokploy_token, schedule_id)
    previous_deployment_key = deployment_key(latest_schedule_deployment or {})
    _logger.info(
        "Dokploy remote data workflow: stack=%s schedule=%s schedule_type=%s compose_project=%s",
        stack_name,
        schedule_id,
        schedule_type,
        compose_app_name,
    )
    dokploy_request(
        host=dokploy_host,
        token=dokploy_token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    deployment_result = wait_for_dokploy_schedule_deployment(
        host=dokploy_host,
        token=dokploy_token,
        schedule_id=schedule_id,
        before_key=previous_deployment_key,
        timeout_seconds=schedule_timeout_seconds,
    )
    _logger.info("Dokploy schedule workflow deployment completed: %s", deployment_result)
    latest_schedule_deployment = latest_deployment_for_schedule(dokploy_host, dokploy_token, schedule_id)
    latest_schedule_status = deployment_status(latest_schedule_deployment or {})
    if latest_schedule_status and latest_schedule_status not in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
        raise RuntimeCommandError(f"Dokploy schedule {schedule_id!r} completed with non-success status {latest_schedule_status!r}.")
    _logger.info("Dokploy-managed data workflow completed for stack %s via schedule %s", stack_name, schedule_id)
    return 0


def _build_dokploy_data_workflow_schedule_app_name(*, context_name: str, instance_name: str) -> str:
    return f"platform-{context_name}-{instance_name}-data-workflow"
