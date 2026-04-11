from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from .local_runtime import RuntimeCommandError

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject = dict[str, JsonValue]

DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS = 600
DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES = {"cancelled", "canceled"}
DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES = {"done", "success", "succeeded", "completed", "finished", "healthy"}
DOKPLOY_RUNNING_DEPLOYMENT_STATUSES = {"pending", "queued", "running", "in_progress", "starting"}

_logger = logging.getLogger(__name__)


def dokploy_request(
    *,
    host: str,
    token: str,
    path: str,
    method: str = "GET",
    payload: JsonObject | None = None,
    query: dict[str, str | int | float] | None = None,
    timeout_seconds: int | float = 60,
) -> JsonValue:
    normalized_host = host.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{normalized_host}{normalized_path}"
    headers = {"x-api-key": token}
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=payload,
            params=query,
            timeout=timeout_seconds,
        )
    except requests.RequestException as error:
        raise RuntimeCommandError(f"Dokploy API {method} {normalized_path} request failed: {error}") from error
    if response.status_code >= 400:
        body = response.text.strip()
        raise RuntimeCommandError(f"Dokploy API {method} {normalized_path} failed ({response.status_code}): {body}")
    if not response.content:
        return {}
    try:
        parsed_payload = response.json()
    except ValueError:
        return {"raw": response.text}
    if isinstance(parsed_payload, (dict, list, str, int, float, bool)) or parsed_payload is None:
        return parsed_payload
    raise RuntimeCommandError(f"Dokploy API {method} {normalized_path} returned an unsupported payload type.")


def as_json_object(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return value


def extract_deployments(raw_payload: JsonValue) -> list[JsonObject]:
    return _extract_json_object_list(raw_payload, candidate_keys=("data", "deployments", "items", "result"))


def extract_schedules(raw_payload: JsonValue) -> list[JsonObject]:
    return _extract_json_object_list(raw_payload, candidate_keys=("data", "schedules", "items", "result"))


def _extract_json_object_list(raw_payload: JsonValue, *, candidate_keys: tuple[str, ...]) -> list[JsonObject]:
    raw_items: list[JsonValue] = []
    if isinstance(raw_payload, list):
        raw_items = raw_payload
    elif isinstance(raw_payload, dict):
        for candidate_key in candidate_keys:
            nested_items = raw_payload.get(candidate_key)
            if isinstance(nested_items, list):
                raw_items = nested_items
                break

    extracted_items: list[JsonObject] = []
    for raw_item in raw_items:
        item_as_object = as_json_object(raw_item)
        if item_as_object is not None:
            extracted_items.append(item_as_object)
    return extracted_items


def schedule_key(schedule: JsonObject) -> str:
    for key_name in ("scheduleId", "schedule_id", "id", "uuid"):
        value = schedule.get(key_name)
        if value:
            return str(value)
    return ""


def deployment_key(deployment: JsonObject) -> str:
    for key_name in ("deploymentId", "deployment_id", "id", "uuid"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return ""


def deployment_status(deployment: JsonObject) -> str:
    for key_name in ("status", "state", "deploymentStatus"):
        value = deployment.get(key_name)
        if value:
            return str(value).strip().lower()
    return ""


def latest_deployment_for_compose(host: str, token: str, compose_id: str) -> JsonObject | None:
    compose_payload = dokploy_request(
        host=host,
        token=token,
        path="/api/compose.one",
        query={"composeId": compose_id},
    )
    compose_payload_as_object = as_json_object(compose_payload)
    if compose_payload_as_object is None:
        return None
    deployments_payload = compose_payload_as_object.get("deployments")
    deployments = extract_deployments(deployments_payload if isinstance(deployments_payload, list) else [])
    return _latest_deployment_from_list(deployments)


def latest_deployment_for_schedule(host: str, token: str, schedule_id: str) -> JsonObject | None:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.allByType",
        query={"id": schedule_id, "type": "schedule"},
    )
    deployments = extract_deployments(payload)
    return _latest_deployment_from_list(deployments)


def _latest_deployment_from_list(deployments: list[JsonObject]) -> JsonObject | None:
    if not deployments:
        return None
    return max(deployments, key=_deployment_sort_key)


def _deployment_sort_key(deployment: JsonObject) -> str:
    for key_name in ("createdAt", "created_at", "updatedAt", "updated_at"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return deployment_key(deployment)


def wait_for_dokploy_schedule_deployment(
    *,
    host: str,
    token: str,
    schedule_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_schedule(host, token, schedule_id),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix="Dokploy schedule deployment failed",
    )


def wait_for_dokploy_compose_deployment(
    *,
    host: str,
    token: str,
    compose_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_compose(host, token, compose_id),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix="Dokploy compose deployment failed",
    )


def _wait_for_deployment_status(
    *,
    fetch_latest_deployment: Callable[[], JsonObject | None],
    before_key: str,
    timeout_seconds: int,
    failure_message_prefix: str,
) -> str:
    failure_statuses = {"failed", "error", "canceled", "cancelled", "killed", "unhealthy", "timeout"}
    start_time = time.monotonic()
    while time.monotonic() - start_time <= timeout_seconds:
        latest_deployment = fetch_latest_deployment()
        if not latest_deployment:
            time.sleep(3)
            continue
        latest_key = deployment_key(latest_deployment)
        latest_status = deployment_status(latest_deployment)
        if latest_key and latest_key != before_key:
            if latest_status in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
                return f"deployment={latest_key} status={latest_status}"
            if latest_status in failure_statuses:
                raise RuntimeCommandError(f"{failure_message_prefix}: deployment={latest_key} status={latest_status}")
            if not latest_status:
                return f"deployment={latest_key} status=unknown"
        time.sleep(3)
    raise RuntimeCommandError("Timed out waiting for Dokploy deployment status.")


def resolve_dokploy_user_id(*, host: str, token: str) -> str:
    payload = dokploy_request(host=host, token=token, path="/api/user.session")
    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise RuntimeCommandError("Dokploy user.session returned an invalid response payload.")
    user_payload = as_json_object(payload_as_object.get("user"))
    if user_payload is None:
        raise RuntimeCommandError("Dokploy user.session returned no user payload.")
    user_id = str(user_payload.get("id") or "").strip()
    if not user_id:
        raise RuntimeCommandError("Dokploy user.session returned no user id.")
    return user_id


def list_dokploy_schedules(*, host: str, token: str, target_id: str, schedule_type: str) -> tuple[JsonObject, ...]:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.list",
        query={"id": target_id, "scheduleType": schedule_type},
    )
    return tuple(extract_schedules(payload))


def find_matching_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
) -> JsonObject | None:
    for schedule in list_dokploy_schedules(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
    ):
        if str(schedule.get("name") or "").strip() != schedule_name:
            continue
        if str(schedule.get("appName") or "").strip() != app_name:
            continue
        return schedule
    return None


def upsert_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
    schedule_payload: JsonObject,
) -> JsonObject:
    existing_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if existing_schedule is not None:
        updated_payload = dict(schedule_payload)
        updated_payload["scheduleId"] = schedule_key(existing_schedule)
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload=updated_payload,
        )
    else:
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=schedule_payload,
        )

    resolved_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if resolved_schedule is None:
        raise RuntimeCommandError(
            f"Dokploy schedule {schedule_name!r} for {schedule_type} target {target_id!r} could not be resolved after upsert."
        )
    return resolved_schedule


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for raw_line in raw_env_text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[7:].strip()
        if "=" not in stripped_line:
            continue
        key_part, value_part = stripped_line.split("=", 1)
        env_map[key_part.strip()] = value_part
    return env_map


def serialize_dokploy_env_text(env_map: dict[str, str]) -> str:
    if not env_map:
        return ""
    rendered_lines = [f"{environment_key}={environment_value}" for environment_key, environment_value in env_map.items()]
    return "\n".join(rendered_lines)


def fetch_dokploy_target_payload(*, host: str, token: str, target_type: str, target_id: str) -> JsonObject:
    if target_type == "compose":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
    elif target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/application.one",
            query={"applicationId": target_id},
        )
    else:
        raise RuntimeCommandError(f"Unsupported target type: {target_type}")

    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise RuntimeCommandError(f"Dokploy {target_type}.one returned an invalid response payload.")
    return payload_as_object


def update_dokploy_target_env(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    target_payload: JsonObject,
    env_text: str,
) -> None:
    if target_type == "compose":
        dokploy_request(
            host=host,
            token=token,
            path="/api/compose.update",
            method="POST",
            payload={"composeId": target_id, "env": env_text},
        )
        return

    if target_type == "application":
        build_args = target_payload.get("buildArgs")
        build_secrets = target_payload.get("buildSecrets")
        create_env_file = target_payload.get("createEnvFile")
        payload: JsonObject = {
            "applicationId": target_id,
            "env": env_text,
            "createEnvFile": bool(create_env_file) if isinstance(create_env_file, bool) else True,
        }
        if isinstance(build_args, str):
            payload["buildArgs"] = build_args
        if isinstance(build_secrets, str):
            payload["buildSecrets"] = build_secrets
        dokploy_request(
            host=host,
            token=token,
            path="/api/application.saveEnvironment",
            method="POST",
            payload=payload,
        )
        return

    raise RuntimeCommandError(f"Unsupported target type: {target_type}")
