from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .local_runtime import RuntimeCommandError, resolve_control_plane_root

CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE_ENV_VAR = "ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE"


@dataclass(frozen=True)
class DokployTargetDefinition:
    context: str
    instance: str
    project_name: str = ""
    target_type: str = "compose"
    target_id: str = ""
    target_name: str = ""
    git_branch: str = ""
    source_git_ref: str = "origin/main"
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = None
    healthcheck_enabled: bool = True
    healthcheck_path: str = "/web/health"
    healthcheck_timeout_seconds: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class DokploySourceOfTruth:
    schema_version: int
    targets: tuple[DokployTargetDefinition, ...] = ()


def load_dokploy_source_of_truth(repo_root: Path) -> DokploySourceOfTruth | None:
    source_file_path = _resolve_dokploy_source_file_path(repo_root)
    if source_file_path is None:
        return None
    try:
        raw_payload = tomllib.loads(source_file_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeCommandError(f"Invalid dokploy source-of-truth file {source_file_path}: {error}") from error
    control_plane_root = resolve_control_plane_root()
    target_ids_file_path: Path | None = None
    if control_plane_root is not None:
        target_ids_file_path = _resolve_dokploy_target_ids_file_path(control_plane_root, source_file_path=source_file_path)
        configured_target_ids_file = os.environ.get(CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE_ENV_VAR, "").strip()
        should_load_target_ids = bool(configured_target_ids_file) or target_ids_file_path.exists()
        if should_load_target_ids:
            raw_payload = _apply_dokploy_target_id_catalog(
                raw_payload,
                target_id_catalog=_load_dokploy_target_id_catalog(target_ids_file_path),
            )
    normalized_payload = _normalize_dokploy_source_payload(raw_payload)
    schema_version = _read_required_int(normalized_payload, "schema_version", scope="dokploy")
    targets_payload = normalized_payload.get("targets")
    if not isinstance(targets_payload, list):
        raise RuntimeCommandError("Dokploy route catalog targets must be an array of target tables.")
    targets = tuple(
        _parse_dokploy_target_definition(raw_target, label=f"dokploy.targets[{target_index}]")
        for target_index, raw_target in enumerate(targets_payload, start=1)
    )
    if control_plane_root is not None:
        missing_target_id_routes = [
            f"{target.context}/{target.instance}" for target in targets if not target.target_id.strip()
        ]
        if missing_target_id_routes:
            missing_joined = ", ".join(missing_target_id_routes)
            target_ids_display = (
                str(target_ids_file_path) if target_ids_file_path is not None else "config/dokploy-targets.toml"
            )
            raise RuntimeCommandError(
                "Control-plane Dokploy route catalog resolved through ODOO_CONTROL_PLANE_ROOT is missing pinned target ids for "
                f"{missing_joined}. Define them in {target_ids_display} or inline target_id values in {source_file_path}."
            )
    return DokploySourceOfTruth(schema_version=schema_version, targets=targets)


def _resolve_dokploy_source_file_path(repo_root: Path) -> Path | None:
    control_plane_root = resolve_control_plane_root()
    if control_plane_root is None:
        return None
    control_plane_source_file = control_plane_root / "config" / "dokploy.toml"
    if control_plane_source_file.exists():
        return control_plane_source_file
    return None


def _resolve_dokploy_target_ids_file_path(control_plane_root: Path, *, source_file_path: Path) -> Path:
    configured_target_ids_file = os.environ.get(CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE_ENV_VAR, "").strip()
    if configured_target_ids_file:
        candidate_path = Path(configured_target_ids_file)
        if not candidate_path.is_absolute():
            candidate_path = control_plane_root / candidate_path
        return candidate_path
    return source_file_path.parent / "dokploy-targets.toml"


def _load_dokploy_target_id_catalog(target_ids_file_path: Path) -> Mapping[str, object]:
    try:
        raw_payload = tomllib.loads(target_ids_file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeCommandError(f"Dokploy target-id catalog file not found: {target_ids_file_path}") from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeCommandError(f"Invalid Dokploy target-id catalog file {target_ids_file_path}: {error}") from error
    if not isinstance(raw_payload, Mapping):
        raise RuntimeCommandError(f"Dokploy target-id catalog {target_ids_file_path} must contain a top-level table.")
    return raw_payload


def _apply_dokploy_target_id_catalog(
    raw_payload: Mapping[str, object],
    *,
    target_id_catalog: Mapping[str, object],
) -> dict[str, object]:
    merged_payload = dict(raw_payload)
    raw_targets = merged_payload.get("targets")
    if not isinstance(raw_targets, list):
        raise RuntimeCommandError("Dokploy route catalog targets must be an array of target tables.")
    catalog_targets = target_id_catalog.get("targets")
    if not isinstance(catalog_targets, list):
        raise RuntimeCommandError("Dokploy target-id catalog targets must be an array of target tables.")

    override_map: dict[tuple[str, str], str] = {}
    for index, raw_target in enumerate(catalog_targets, start=1):
        target_table = _ensure_mapping(raw_target, label=f"dokploy-target-ids.targets[{index}]")
        context_name = _read_required_string(target_table, "context", scope=f"dokploy-target-ids.targets[{index}]")
        instance_name = _read_required_string(target_table, "instance", scope=f"dokploy-target-ids.targets[{index}]")
        target_id = _read_required_string(target_table, "target_id", scope=f"dokploy-target-ids.targets[{index}]")
        target_route = (context_name, instance_name)
        if target_route in override_map:
            raise RuntimeCommandError(
                f"Duplicate Dokploy target-id override for {context_name}/{instance_name} in target-id catalog"
            )
        override_map[target_route] = target_id

    remaining_routes = set(override_map)
    merged_targets: list[object] = []
    for index, raw_target in enumerate(raw_targets, start=1):
        target_table = _ensure_mapping(raw_target, label=f"dokploy.targets[{index}]")
        merged_target = dict(target_table)
        context_name = str(merged_target.get("context") or "").strip()
        instance_name = str(merged_target.get("instance") or "").strip()
        target_route = (context_name, instance_name)
        override_target_id = override_map.get(target_route)
        if override_target_id is not None:
            merged_target["target_id"] = override_target_id
            remaining_routes.discard(target_route)
        merged_targets.append(merged_target)

    if remaining_routes:
        unknown_routes = ", ".join(
            f"{context_name}/{instance_name}" for context_name, instance_name in sorted(remaining_routes)
        )
        raise RuntimeCommandError(
            "Dokploy target-id catalog contains route(s) that are not present in the control-plane route catalog: "
            f"{unknown_routes}"
        )

    merged_payload["targets"] = merged_targets
    return merged_payload


def find_dokploy_target_definition(
    source_of_truth: DokploySourceOfTruth,
    *,
    context_name: str,
    instance_name: str,
) -> DokployTargetDefinition | None:
    for target in source_of_truth.targets:
        if target.context == context_name and target.instance == instance_name:
            return target
    return None


def _parse_dokploy_target_definition(raw_target: object, *, label: str) -> DokployTargetDefinition:
    target_table = _ensure_mapping(raw_target, label=label)
    target_type = _read_optional_string(target_table, "target_type", scope=label) or "compose"
    if target_type not in {"compose", "application"}:
        raise RuntimeCommandError(f"{label}.target_type must be 'compose' or 'application'.")
    deploy_timeout_seconds = _read_optional_int(target_table, "deploy_timeout_seconds", scope=label)
    healthcheck_timeout_seconds = _read_optional_int(target_table, "healthcheck_timeout_seconds", scope=label)
    return DokployTargetDefinition(
        context=_read_required_string(target_table, "context", scope=label),
        instance=_read_required_string(target_table, "instance", scope=label),
        project_name=_read_optional_string(target_table, "project_name", scope=label) or "",
        target_type=target_type,
        target_id=_read_optional_string(target_table, "target_id", scope=label) or "",
        target_name=_read_optional_string(target_table, "target_name", scope=label) or "",
        git_branch=_read_optional_string(target_table, "git_branch", scope=label) or "",
        source_git_ref=_read_optional_string(target_table, "source_git_ref", scope=label) or "origin/main",
        require_test_gate=_read_optional_bool(target_table, "require_test_gate", scope=label, default=False),
        require_prod_gate=_read_optional_bool(target_table, "require_prod_gate", scope=label, default=False),
        deploy_timeout_seconds=deploy_timeout_seconds,
        healthcheck_enabled=_read_optional_bool(target_table, "healthcheck_enabled", scope=label, default=True),
        healthcheck_path=_read_optional_string(target_table, "healthcheck_path", scope=label) or "/web/health",
        healthcheck_timeout_seconds=healthcheck_timeout_seconds,
        env=_read_optional_string_map(target_table, "env", scope=label),
        domains=_read_optional_string_tuple(target_table, "domains", scope=label),
    )


def _normalize_dokploy_source_payload(raw_value: object) -> Mapping[str, object]:
    if not isinstance(raw_value, Mapping):
        raise RuntimeCommandError("Dokploy route catalog must contain a top-level table.")

    normalized_payload = dict(raw_value)
    allowed_top_level_keys = {"defaults", "profiles", "projects", "schema_version", "targets"}
    unknown_keys = sorted(key for key in normalized_payload if key not in allowed_top_level_keys)
    if unknown_keys:
        unknown_key_list = ", ".join(unknown_keys)
        raise RuntimeCommandError(f"Unknown top-level dokploy keys: {unknown_key_list}")

    raw_targets = normalized_payload.get("targets")
    if not isinstance(raw_targets, list):
        raise RuntimeCommandError("Dokploy route catalog targets must be an array of target tables.")

    defaults = _expect_mapping(normalized_payload.get("defaults"), label="defaults")
    raw_profiles = _expect_mapping(normalized_payload.get("profiles"), label="profiles")
    raw_projects = _expect_mapping(normalized_payload.get("projects"), label="projects")
    resolved_profiles: dict[str, dict[str, object]] = {}
    targets: list[object] = []
    for target_index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, Mapping):
            raise RuntimeCommandError(f"dokploy.targets[{target_index}] must be a table.")
        target_payload = dict(raw_target)
        profile_name = str(target_payload.pop("profile", "") or "").strip()
        merged_target = dict(defaults)
        if profile_name:
            merged_target = _merge_dokploy_settings(
                merged_target,
                _resolve_dokploy_profile(
                    profile_name,
                    raw_profiles=raw_profiles,
                    raw_projects=raw_projects,
                    resolved_profiles=resolved_profiles,
                    active_profiles=(),
                ),
            )
        merged_target = _merge_dokploy_settings(merged_target, target_payload)
        targets.append(
            _resolve_dokploy_project_reference(
                merged_target,
                raw_projects=raw_projects,
                label=f"targets[{target_index}]",
            )
        )

    return {
        "schema_version": normalized_payload.get("schema_version"),
        "targets": targets,
    }


def _resolve_dokploy_profile(
    profile_name: str,
    *,
    raw_profiles: Mapping[str, object],
    raw_projects: Mapping[str, object],
    resolved_profiles: dict[str, dict[str, object]],
    active_profiles: tuple[str, ...],
) -> dict[str, object]:
    if profile_name in resolved_profiles:
        return dict(resolved_profiles[profile_name])
    if profile_name in active_profiles:
        profile_chain = " -> ".join((*active_profiles, profile_name))
        raise RuntimeCommandError(f"Dokploy profile inheritance cycle detected: {profile_chain}")

    raw_profile = raw_profiles.get(profile_name)
    if raw_profile is None:
        raise RuntimeCommandError(f"Unknown dokploy profile: {profile_name}")
    if not isinstance(raw_profile, Mapping):
        raise RuntimeCommandError(f"Dokploy profile '{profile_name}' must be a table/object")

    profile_payload = dict(raw_profile)
    parent_profile_name = str(profile_payload.pop("extends", "") or "").strip()
    merged_profile: dict[str, object] = {}
    if parent_profile_name:
        merged_profile = _resolve_dokploy_profile(
            parent_profile_name,
            raw_profiles=raw_profiles,
            raw_projects=raw_projects,
            resolved_profiles=resolved_profiles,
            active_profiles=(*active_profiles, profile_name),
        )
    merged_profile = _merge_dokploy_settings(merged_profile, profile_payload)
    merged_profile = _resolve_dokploy_project_reference(
        merged_profile,
        raw_projects=raw_projects,
        label=f"profiles.{profile_name}",
    )
    resolved_profiles[profile_name] = dict(merged_profile)
    return merged_profile


def _resolve_dokploy_project_reference(
    payload: dict[str, object],
    *,
    raw_projects: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    resolved_payload = dict(payload)
    raw_project_alias = resolved_payload.pop("project", None)
    if raw_project_alias in (None, ""):
        return resolved_payload

    project_alias = str(raw_project_alias).strip()
    if not project_alias:
        return resolved_payload
    if str(resolved_payload.get("project_name") or "").strip():
        raise RuntimeCommandError(f"{label} cannot define both project and project_name")

    raw_project_value = raw_projects.get(project_alias)
    if raw_project_value is None:
        raise RuntimeCommandError(f"Unknown dokploy project alias '{project_alias}' in {label}")
    if isinstance(raw_project_value, str):
        project_name = raw_project_value.strip()
    elif isinstance(raw_project_value, Mapping):
        project_name = str(raw_project_value.get("project_name") or "").strip()
    else:
        raise RuntimeCommandError(f"Dokploy project alias '{project_alias}' in {label} must be a string or table")
    if not project_name:
        raise RuntimeCommandError(f"Dokploy project alias '{project_alias}' in {label} is missing project_name")
    resolved_payload["project_name"] = project_name
    return resolved_payload


def _expect_mapping(raw_value: object, *, label: str) -> dict[str, object]:
    if raw_value in (None, ""):
        return {}
    if not isinstance(raw_value, Mapping):
        raise RuntimeCommandError(f"Dokploy {label} must be a table/object")
    if not all(isinstance(key, str) for key in raw_value):
        raise RuntimeCommandError(f"Dokploy {label} keys must be strings")
    return dict(raw_value)


def _merge_dokploy_settings(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    merged_settings = dict(base)
    for key_name, key_value in overlay.items():
        base_env = merged_settings.get("env")
        if key_name == "env" and isinstance(base_env, Mapping) and isinstance(key_value, Mapping):
            merged_environment: dict[str, object] = {}
            for environment_key, environment_value in base_env.items():
                if isinstance(environment_key, str):
                    merged_environment[environment_key] = environment_value
            for environment_key, environment_value in key_value.items():
                if isinstance(environment_key, str):
                    merged_environment[environment_key] = environment_value
            merged_settings["env"] = merged_environment
            continue
        merged_settings[key_name] = key_value
    return merged_settings


def _ensure_mapping(raw_value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw_value, Mapping):
        raise RuntimeCommandError(f"{label} must be a table/object.")
    if not all(isinstance(key, str) for key in raw_value):
        raise RuntimeCommandError(f"{label} keys must be strings.")
    return raw_value


def _read_required_string(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> str:
    value = raw_value.get(key_name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeCommandError(f"Missing required string {scope}.{key_name}")
    return value.strip()


def _read_optional_string(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> str | None:
    value = raw_value.get(key_name)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RuntimeCommandError(f"{scope}.{key_name} must be a string")
    return value.strip()


def _read_required_int(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> int:
    value = raw_value.get(key_name)
    if not isinstance(value, int):
        raise RuntimeCommandError(f"Missing required integer {scope}.{key_name}")
    return value


def _read_optional_int(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> int | None:
    value = raw_value.get(key_name)
    if value is None:
        return None
    if not isinstance(value, int):
        raise RuntimeCommandError(f"{scope}.{key_name} must be an integer")
    return value


def _read_optional_bool(raw_value: Mapping[str, object], key_name: str, *, scope: str, default: bool) -> bool:
    value = raw_value.get(key_name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuntimeCommandError(f"{scope}.{key_name} must be a boolean")
    return value


def _read_optional_string_tuple(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> tuple[str, ...]:
    value = raw_value.get(key_name)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeCommandError(f"{scope}.{key_name} must be an array of strings")
    rendered_values: list[str] = []
    for item_index, raw_item in enumerate(value, start=1):
        if not isinstance(raw_item, str):
            raise RuntimeCommandError(f"{scope}.{key_name}[{item_index}] must be a string")
        rendered_values.append(raw_item.strip())
    return tuple(rendered_values)


def _read_optional_string_map(raw_value: Mapping[str, object], key_name: str, *, scope: str) -> dict[str, str]:
    value = raw_value.get(key_name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeCommandError(f"{scope}.{key_name} must be a table/object")
    rendered_values: dict[str, str] = {}
    for raw_key, raw_item in value.items():
        if not isinstance(raw_key, str):
            raise RuntimeCommandError(f"{scope}.{key_name} keys must be strings")
        if not isinstance(raw_item, (str, int, float, bool)):
            raise RuntimeCommandError(f"{scope}.{key_name}.{raw_key} must be scalar")
        rendered_values[raw_key] = str(raw_item)
    return rendered_values
