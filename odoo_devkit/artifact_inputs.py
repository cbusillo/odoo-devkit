from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .manifest import WorkspaceManifest


@dataclass(frozen=True)
class ArtifactInputSourceDefinition:
    repository: str
    exact_ref: str | None = None
    selector: str | None = None

    def repository_spec(self) -> str:
        resolved_ref = self.exact_ref or self.selector or ""
        return f"{self.repository}@{resolved_ref}"


@dataclass(frozen=True)
class ArtifactInputInstanceDefinition:
    sources_add: tuple[ArtifactInputSourceDefinition, ...]


@dataclass(frozen=True)
class ArtifactInputContextDefinition:
    sources_add: tuple[ArtifactInputSourceDefinition, ...]
    instances: dict[str, ArtifactInputInstanceDefinition]


@dataclass(frozen=True)
class ArtifactInputsDefinition:
    schema_version: int
    source_file_path: Path
    sources: tuple[ArtifactInputSourceDefinition, ...]
    contexts: dict[str, ArtifactInputContextDefinition]


class ArtifactInputsError(ValueError):
    pass


def resolve_artifact_inputs_file_path(*, manifest: WorkspaceManifest) -> Path:
    configured_file = (manifest.artifacts.inputs_file or "").strip()
    if not configured_file:
        return (manifest.manifest_directory / "artifact-inputs.toml").resolve()
    expanded_path = Path(os.path.expanduser(configured_file))
    if expanded_path.is_absolute():
        return expanded_path.resolve()
    return (manifest.manifest_directory / expanded_path).resolve()


def load_artifact_inputs_definition(*, manifest: WorkspaceManifest) -> ArtifactInputsDefinition | None:
    source_file_path = resolve_artifact_inputs_file_path(manifest=manifest)
    if not source_file_path.exists():
        return None
    try:
        payload = tomllib.loads(source_file_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ArtifactInputsError(f"Invalid artifact input file {source_file_path}: {error}") from error
    return parse_artifact_inputs_definition(payload=payload, source_file_path=source_file_path)


def parse_artifact_inputs_definition(
    *,
    payload: dict[str, object],
    source_file_path: Path,
) -> ArtifactInputsDefinition:
    schema_version = _read_required_int(payload, "schema_version", scope="artifact inputs")
    if schema_version != 1:
        raise ArtifactInputsError(f"Unsupported artifact input schema_version: {schema_version}")
    contexts_table = _read_optional_table(payload, "contexts", scope="artifact inputs")
    contexts: dict[str, ArtifactInputContextDefinition] = {}
    for context_name, raw_context in contexts_table.items():
        contexts[context_name] = _parse_context_definition(context_name=context_name, raw_context=raw_context)
    return ArtifactInputsDefinition(
        schema_version=schema_version,
        source_file_path=source_file_path,
        sources=_read_source_definitions(payload, "sources", scope="artifact inputs"),
        contexts=contexts,
    )


def effective_artifact_input_sources(
    *,
    artifact_inputs_definition: ArtifactInputsDefinition,
    context_name: str,
    instance_name: str,
) -> tuple[ArtifactInputSourceDefinition, ...]:
    effective_sources: list[ArtifactInputSourceDefinition] = []
    repository_indexes: dict[str, int] = {}

    def upsert_source(source_definition: ArtifactInputSourceDefinition) -> None:
        repository_key = source_definition.repository.strip()
        if not repository_key:
            return
        existing_index = repository_indexes.get(repository_key)
        if existing_index is None:
            repository_indexes[repository_key] = len(effective_sources)
            effective_sources.append(source_definition)
            return
        effective_sources[existing_index] = source_definition

    for source_definition in artifact_inputs_definition.sources:
        upsert_source(source_definition)
    context_definition = artifact_inputs_definition.contexts.get(context_name)
    if context_definition is None:
        return tuple(effective_sources)
    for source_definition in context_definition.sources_add:
        upsert_source(source_definition)
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        return tuple(effective_sources)
    for source_definition in instance_definition.sources_add:
        upsert_source(source_definition)
    return tuple(effective_sources)


def _parse_context_definition(*, context_name: str, raw_context: object) -> ArtifactInputContextDefinition:
    context_table = _ensure_table(raw_context, scope=f"contexts.{context_name}")
    instances_table = _read_optional_table(context_table, "instances", scope=f"contexts.{context_name}")
    instances: dict[str, ArtifactInputInstanceDefinition] = {}
    for instance_name, raw_instance in instances_table.items():
        instances[instance_name] = _parse_instance_definition(
            context_name=context_name,
            instance_name=instance_name,
            raw_instance=raw_instance,
        )
    return ArtifactInputContextDefinition(
        sources_add=_read_source_definitions(context_table, "sources_add", scope=f"contexts.{context_name}"),
        instances=instances,
    )


def _parse_instance_definition(*, context_name: str, instance_name: str, raw_instance: object) -> ArtifactInputInstanceDefinition:
    instance_table = _ensure_table(raw_instance, scope=f"contexts.{context_name}.instances.{instance_name}")
    return ArtifactInputInstanceDefinition(
        sources_add=_read_source_definitions(
            instance_table,
            "sources_add",
            scope=f"contexts.{context_name}.instances.{instance_name}",
        )
    )


def _read_source_definitions(
    source: dict[str, object],
    key: str,
    *,
    scope: str,
) -> tuple[ArtifactInputSourceDefinition, ...]:
    value = source.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ArtifactInputsError(f"Expected {scope}.{key} to be an array of tables")
    definitions: list[ArtifactInputSourceDefinition] = []
    for index, raw_entry in enumerate(value):
        entry_scope = f"{scope}.{key}[{index}]"
        entry = _ensure_table(raw_entry, scope=entry_scope)
        repository = _read_required_string(entry, "repository", scope=entry_scope)
        exact_ref = _read_optional_string(entry, "exact_ref", scope=entry_scope)
        selector = _read_optional_string(entry, "selector", scope=entry_scope)
        if bool(exact_ref) == bool(selector):
            raise ArtifactInputsError(
                f"{entry_scope} must set exactly one of exact_ref or selector."
            )
        definitions.append(
            ArtifactInputSourceDefinition(
                repository=repository,
                exact_ref=exact_ref,
                selector=selector,
            )
        )
    return tuple(definitions)


def _ensure_table(raw_value: object, *, scope: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ArtifactInputsError(f"Expected {scope} to be a table")
    return raw_value


def _read_optional_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ArtifactInputsError(f"Expected {scope}.{key} to be a table when present")
    return value


def _read_required_int(source: dict[str, object], key: str, *, scope: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise ArtifactInputsError(f"Expected {scope}.{key} to be an integer")
    return value


def _read_required_string(source: dict[str, object], key: str, *, scope: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactInputsError(f"Expected {scope}.{key} to be a non-empty string")
    return value


def _read_optional_string(source: dict[str, object], key: str, *, scope: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ArtifactInputsError(f"Expected {scope}.{key} to be a string when present")
    normalized_value = value.strip()
    return normalized_value or None
