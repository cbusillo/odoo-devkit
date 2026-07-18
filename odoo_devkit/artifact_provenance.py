from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PACKAGE_VERSION_PATTERN = re.compile(r"^(?=.*[0-9])[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
_PYTHON_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]+)?$")
_PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?$")
_REPOSITORY_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPOSITORY_IDENTITY_PATTERN = re.compile(r"^[\x21-\x7e]+$")
_REPOSITORY_URL_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SCP_REPOSITORY_PATTERN = re.compile(r"^(?P<username>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._/-]+)$")
_SSH_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_REPO_RELATIVE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class ArtifactProvenanceError(ValueError):
    pass


def aggregate_dependency_evidence(
    *,
    evidence_root: Path,
    expected_platforms: tuple[str, ...],
    expected_uv_locks: tuple[dict[str, str], ...],
) -> dict[str, object]:
    normalized_platforms = tuple(sorted(_normalize_platform(platform) for platform in expected_platforms))
    if not normalized_platforms or len(normalized_platforms) != len(set(normalized_platforms)):
        raise ArtifactProvenanceError("Artifact dependency evidence requires unique target platforms")
    expected_locks = _normalize_uv_locks(expected_uv_locks)
    evidence_paths = tuple(sorted(evidence_root.rglob("dependency-provenance.json")))
    if not evidence_paths:
        raise ArtifactProvenanceError("Published artifact image did not expose dependency provenance evidence")

    environments: dict[str, dict[str, object]] = {}
    common_locks: tuple[dict[str, str], ...] | None = None
    common_external_inputs: tuple[dict[str, str], ...] | None = None
    for evidence_path in evidence_paths:
        payload = _load_json_object(evidence_path)
        if set(payload) != {
            "schema_version",
            "layout",
            "publishable",
            "target_platform",
            "uv_locks",
            "python_environment",
            "external_compatibility_inputs",
        }:
            raise ArtifactProvenanceError("Dependency evidence sidecar contains unsupported fields")
        if payload.get("schema_version") != 1 or payload.get("layout") != "two_lock":
            raise ArtifactProvenanceError("Dependency evidence sidecar must use schema 1 two_lock layout")
        if payload.get("publishable") is not True:
            raise ArtifactProvenanceError("Dependency evidence sidecar is not publishable")
        platform = _normalize_platform(_required_string(payload, "target_platform"))
        if platform not in normalized_platforms:
            raise ArtifactProvenanceError(f"Dependency evidence contains unexpected target platform: {platform}")
        if platform in environments:
            raise ArtifactProvenanceError(f"Dependency evidence contains duplicate target platform: {platform}")
        raw_locks = payload.get("uv_locks")
        if not isinstance(raw_locks, list):
            raise ArtifactProvenanceError("Dependency evidence uv_locks must be an array")
        locks = _normalize_uv_locks(tuple(_required_object(item, label="uv lock") for item in raw_locks))
        if locks != expected_locks:
            raise ArtifactProvenanceError("Published dependency lock evidence does not match the exact staged lock inputs")
        raw_external_inputs = payload.get("external_compatibility_inputs")
        if not isinstance(raw_external_inputs, list):
            raise ArtifactProvenanceError("Dependency evidence external_compatibility_inputs must be an array")
        external_inputs = _normalize_external_inputs(
            tuple(_required_object(item, label="external compatibility input") for item in raw_external_inputs)
        )
        if common_locks is None:
            common_locks = locks
        elif common_locks != locks:
            raise ArtifactProvenanceError("Dependency lock evidence differs across target platforms")
        if common_external_inputs is None:
            common_external_inputs = external_inputs
        elif common_external_inputs != external_inputs:
            raise ArtifactProvenanceError("External compatibility evidence differs across target platforms")
        environments[platform] = _normalize_python_environment(
            _required_object(payload.get("python_environment"), label="python environment")
        )

    if set(environments) != set(normalized_platforms):
        missing = sorted(set(normalized_platforms) - set(environments))
        raise ArtifactProvenanceError(f"Dependency evidence is missing target platforms: {missing}")
    assert common_locks is not None
    return {
        "target_platforms": list(normalized_platforms),
        "uv_locks": [dict(lock) for lock in common_locks],
        "python_environments": {platform: environments[platform] for platform in normalized_platforms},
        "external_compatibility_inputs": [dict(item) for item in common_external_inputs or ()],
    }


def normalize_repository_identity(value: str) -> str:
    normalized = value.strip()
    lowered = normalized.lower()
    if not normalized:
        raise ArtifactProvenanceError("Repository identity cannot be empty")
    if _REPOSITORY_IDENTITY_PATTERN.fullmatch(normalized) is None:
        raise ArtifactProvenanceError("Repository identity contains unsafe characters")
    if (
        normalized.startswith(("/", "./", "../", "~"))
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or "\\" in normalized
        or lowered.startswith(("file:", "git+file:"))
    ):
        raise ArtifactProvenanceError("Repository identity cannot use a local path")
    if _REPOSITORY_SLUG_PATTERN.fullmatch(normalized):
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ArtifactProvenanceError("Repository identity cannot contain path traversal")
        normalized_slug = normalized.removesuffix(".git")
        if _REPOSITORY_SLUG_PATTERN.fullmatch(normalized_slug) is None:
            raise ArtifactProvenanceError("Repository identity must use owner/repository syntax")
        return normalized_slug

    scp_match = _SCP_REPOSITORY_PATTERN.fullmatch(normalized)
    if scp_match is not None:
        host = scp_match.group("host").lower()
        path = _normalize_repository_path(scp_match.group("path"), label="Repository identity").removesuffix(".git")
        if host == "github.com" and _REPOSITORY_SLUG_PATTERN.fullmatch(path):
            return path
        return f"ssh://{scp_match.group('username')}@{host}/{path}"

    if "://" not in normalized:
        raise ArtifactProvenanceError("Repository identity must use owner/repository or a sanitized URL")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ArtifactProvenanceError("Repository identity must use an https or ssh repository URL")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise ArtifactProvenanceError("Repository identity cannot contain credentials, queries, or fragments")
    if parsed.username is not None and (parsed.scheme != "ssh" or _SSH_USERNAME_PATTERN.fullmatch(parsed.username) is None):
        raise ArtifactProvenanceError("Repository identity contains invalid userinfo")
    path = _normalize_repository_path(parsed.path, label="Repository identity").removesuffix(".git")
    if parsed.hostname.lower() == "github.com" and _REPOSITORY_SLUG_PATTERN.fullmatch(path):
        return path
    username = parsed.username if parsed.scheme == "ssh" else None
    authority = f"{username}@{parsed.hostname.lower()}" if username else parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise ArtifactProvenanceError("Repository identity contains an invalid port") from error
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, f"/{path}", "", ""))


def _normalize_repository_path(value: str, *, label: str) -> str:
    normalized = f"/{value.strip('/')}"
    if _REPOSITORY_URL_PATH_PATTERN.fullmatch(normalized) is None:
        raise ArtifactProvenanceError(f"{label} contains unsafe path characters")
    parts = normalized.strip("/").split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ArtifactProvenanceError(f"{label} contains path traversal")
    return normalized.strip("/")


def normalize_git_commit(value: str) -> str:
    normalized = value.strip()
    if _GIT_COMMIT_PATTERN.fullmatch(normalized) is None:
        raise ArtifactProvenanceError("Source ref must be an exact lowercase 40-character git commit")
    return normalized


def _normalize_uv_locks(raw_locks: tuple[dict[str, object] | dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    normalized_locks: list[dict[str, str]] = []
    for raw_lock in raw_locks:
        if set(raw_lock) != {"scope", "source_repository", "source_ref", "path", "sha256"}:
            raise ArtifactProvenanceError("uv lock evidence contains unsupported fields")
        scope = _required_string(raw_lock, "scope")
        if scope not in {"support_runtime", "tenant"}:
            raise ArtifactProvenanceError(f"Unsupported uv lock scope: {scope}")
        path = _normalize_relative_path(_required_string(raw_lock, "path"))
        if PurePosixPath(path).name != "uv.lock":
            raise ArtifactProvenanceError("uv lock evidence path must identify uv.lock")
        sha256 = _required_string(raw_lock, "sha256")
        if _SHA256_PATTERN.fullmatch(sha256) is None:
            raise ArtifactProvenanceError("uv lock evidence requires a lowercase SHA-256")
        normalized_locks.append(
            {
                "scope": scope,
                "source_repository": normalize_repository_identity(_required_string(raw_lock, "source_repository")),
                "source_ref": normalize_git_commit(_required_string(raw_lock, "source_ref")),
                "path": path,
                "sha256": sha256,
            }
        )
    scopes = [lock["scope"] for lock in normalized_locks]
    if set(scopes) != {"support_runtime", "tenant"} or len(scopes) != 2:
        raise ArtifactProvenanceError("Dependency evidence requires support_runtime and tenant uv locks")
    order = {"support_runtime": 0, "tenant": 1}
    return tuple(sorted(normalized_locks, key=lambda lock: order[lock["scope"]]))


def _normalize_python_environment(payload: dict[str, object]) -> dict[str, object]:
    if set(payload) != {"python_version", "packages", "package_count", "packages_sha256"}:
        raise ArtifactProvenanceError("Python environment evidence contains unsupported fields")
    python_version = _required_string(payload, "python_version").lower()
    if _PYTHON_VERSION_PATTERN.fullmatch(python_version) is None:
        raise ArtifactProvenanceError("Python environment evidence requires an exact Python version")
    raw_packages = payload.get("packages")
    if not isinstance(raw_packages, list):
        raise ArtifactProvenanceError("Python environment packages must be an array")
    packages: list[dict[str, object]] = []
    names: set[str] = set()
    for raw_package in raw_packages:
        package = _required_object(raw_package, label="python package")
        if set(package) != {"name", "version", "source"}:
            raise ArtifactProvenanceError("Python package evidence contains unsupported fields")
        name = re.sub(r"[-_.]+", "-", _required_string(package, "name")).lower()
        version = _required_string(package, "version")
        if _PACKAGE_NAME_PATTERN.fullmatch(name) is None or name in names:
            raise ArtifactProvenanceError(f"Python package evidence has invalid or duplicate name: {name}")
        if _PACKAGE_VERSION_PATTERN.fullmatch(version) is None:
            raise ArtifactProvenanceError(f"Python package evidence has invalid exact version: {version}")
        names.add(name)
        source = _normalize_package_source(_required_object(package.get("source"), label="python package source"))
        packages.append({"name": name, "version": version, "source": source})
    packages.sort(key=lambda package: str(package["name"]))
    package_count = payload.get("package_count")
    if not isinstance(package_count, int) or isinstance(package_count, bool) or package_count != len(packages):
        raise ArtifactProvenanceError("Python package_count does not match package evidence")
    canonical_json = json.dumps(packages, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    packages_sha256 = _required_string(payload, "packages_sha256")
    expected_sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if packages_sha256 != expected_sha256:
        raise ArtifactProvenanceError("Python packages_sha256 does not match canonical package evidence")
    return {
        "python_version": python_version,
        "packages": packages,
        "package_count": package_count,
        "packages_sha256": packages_sha256,
    }


def _normalize_package_source(payload: dict[str, object]) -> dict[str, str]:
    if set(payload) != {"kind", "repository", "commit"}:
        raise ArtifactProvenanceError("Python package source contains unsupported fields")
    kind = _required_string(payload, "kind")
    repository = str(payload.get("repository", "")).strip()
    commit = str(payload.get("commit", "")).strip()
    if kind == "registry":
        if repository or commit:
            raise ArtifactProvenanceError("Registry package source cannot contain repository or commit")
        return {"kind": "registry", "repository": "", "commit": ""}
    if kind != "vcs":
        raise ArtifactProvenanceError(f"Unsupported Python package source kind: {kind}")
    return {
        "kind": "vcs",
        "repository": normalize_repository_identity(repository),
        "commit": normalize_git_commit(commit),
    }


def _normalize_external_inputs(raw_inputs: tuple[dict[str, object], ...]) -> tuple[dict[str, str], ...]:
    normalized_inputs: list[dict[str, str]] = []
    for raw_input in raw_inputs:
        if set(raw_input) != {
            "source_repository",
            "source_ref",
            "dependency_file_path",
            "dependency_file_sha256",
            "format",
            "resolution_posture",
        }:
            raise ArtifactProvenanceError("External compatibility evidence contains unsupported fields")
        dependency_format = _required_string(raw_input, "format")
        if dependency_format not in {"pyproject_toml", "requirements_txt"}:
            raise ArtifactProvenanceError(f"Unsupported external dependency format: {dependency_format}")
        resolution_posture = _required_string(raw_input, "resolution_posture")
        if resolution_posture not in {"locked", "exact_source_unlocked"}:
            raise ArtifactProvenanceError(f"Unsupported external dependency posture: {resolution_posture}")
        path = _normalize_relative_path(_required_string(raw_input, "dependency_file_path"))
        file_name = PurePosixPath(path).name
        if dependency_format == "pyproject_toml" and file_name != "pyproject.toml":
            raise ArtifactProvenanceError("pyproject_toml evidence must identify pyproject.toml")
        if dependency_format == "requirements_txt" and not file_name.endswith(".txt"):
            raise ArtifactProvenanceError("requirements_txt evidence must identify a .txt file")
        sha256 = _required_string(raw_input, "dependency_file_sha256")
        if _SHA256_PATTERN.fullmatch(sha256) is None:
            raise ArtifactProvenanceError("External dependency evidence requires a lowercase SHA-256")
        normalized_inputs.append(
            {
                "source_repository": normalize_repository_identity(_required_string(raw_input, "source_repository")),
                "source_ref": normalize_git_commit(_required_string(raw_input, "source_ref")),
                "dependency_file_path": path,
                "dependency_file_sha256": sha256,
                "format": dependency_format,
                "resolution_posture": resolution_posture,
            }
        )
    normalized_inputs.sort(key=lambda item: (item["source_repository"], item["source_ref"], item["dependency_file_path"]))
    identities = [(item["source_repository"], item["source_ref"], item["dependency_file_path"]) for item in normalized_inputs]
    if len(identities) != len(set(identities)):
        raise ArtifactProvenanceError("External dependency evidence contains duplicate inputs")
    return tuple(normalized_inputs)


def _normalize_relative_path(value: str) -> str:
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith(("/", "~"))
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or "\\" in normalized
        or "://" in normalized
        or _REPO_RELATIVE_PATH_PATTERN.fullmatch(normalized) is None
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != normalized
    ):
        raise ArtifactProvenanceError("Dependency evidence path must be a safe repository-relative path")
    return path.as_posix()


def _normalize_platform(value: str) -> str:
    normalized = value.strip().lower()
    if _PLATFORM_PATTERN.fullmatch(normalized) is None:
        raise ArtifactProvenanceError(f"Invalid OCI platform: {value}")
    return normalized


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactProvenanceError(f"Invalid dependency evidence JSON: {path.name}") from error
    return _required_object(payload, label="dependency evidence")


def _required_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ArtifactProvenanceError(f"{label} must be a JSON object")
    return value


def _required_string(source: dict[str, object] | dict[str, str], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactProvenanceError(f"Dependency evidence {key} must be a non-empty string")
    return value.strip()
