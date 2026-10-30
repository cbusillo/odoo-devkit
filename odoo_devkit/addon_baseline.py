from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_binding(binding: Path) -> dict[str, Any]:
    expected = json.loads(binding.read_bytes())
    if not isinstance(expected, dict) or any(
        not isinstance(expected.get(key), str)
        for key in ("postgres_image", "postgres_version", "architecture", "database", "database_identity")
    ):
        raise ValueError("Baseline needs a complete PostgreSQL binding")
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", expected["postgres_image"]):
        raise ValueError("Baseline requires an immutable PostgreSQL image")
    if expected["architecture"] != "x86_64" or not re.fullmatch(
        r"postgres \(PostgreSQL\) [0-9]+\.[^\n]+", expected["postgres_version"]
    ):
        raise ValueError("Baseline requires the qualified PostgreSQL identity and AMD64 architecture")
    initialization = expected.get("initialization")
    settings = expected.get("settings")
    if (
        not isinstance(initialization, dict)
        or not initialization
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in initialization.values())
        or not re.fullmatch(r"[A-Za-z0-9_]+", expected["database"])
        or expected.get("synthetic_only") is not True
        or not re.fullmatch(r"[A-Za-z0-9_.@-]+\|[A-Za-z0-9_.@-]+\|[A-Za-z0-9_.@-]+\|c", expected["database_identity"])
        or not isinstance(settings, dict)
        or set(settings) != {"lc_messages", "lc_monetary", "lc_numeric", "lc_time", "default_text_search_config", "timezone"}
        or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./@-]+", value) for value in settings.values())
    ):
        raise ValueError("Baseline needs a synthetic database and current initialization bindings")
    return expected


def producer_input(inputs: Path, *, binding: Path, user: str, password: str) -> dict[str, Any]:
    stage = read_binding(binding)
    if json.loads((inputs / "binding.json").read_bytes()) != stage:
        raise ValueError("Producer binding differs from current inputs")
    filestore = inputs / "filestore"
    if filestore.is_symlink() or not filestore.is_dir():
        raise ValueError("Producer needs a regular filestore")
    inventory = []
    for path in sorted(filestore.rglob("*")):
        relative = path.relative_to(inputs).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()) or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
            raise ValueError("Producer contains an unqualified filestore member")
        if path.is_file():
            inventory.append(f"{file_sha256(path)}  {relative}\n")
    filestore_sha256 = hashlib.sha256("".join(sorted(inventory)).encode()).hexdigest()
    dump = inputs / "database.dump"
    if (
        dump.is_symlink()
        or not dump.is_file()
        or (
            file_sha256(dump) != stage["initialization"].get("logical_dump")
            or filestore_sha256 != stage["initialization"].get("filestore_inventory")
        )
    ):
        raise ValueError("Producer dump or complete filestore differs from the qualified inputs")
    return {
        "name": "database",
        "image": stage["postgres_image"],
        "command": ["bash", "/workspace/baseline/producer.sh"],
        "environment": {
            "POSTGRES_USER": user,
            "POSTGRES_DB": "postgres",
            "POSTGRES_PASSWORD": password,
            "PGPASSWORD": password,
            "BASELINE_DATABASE": stage["database"],
            "BASELINE_POSTGRES_VERSION": stage["postgres_version"],
            "BASELINE_ARCHITECTURE": stage["architecture"],
            "BASELINE_DATABASE_IDENTITY": stage["database_identity"],
            "BASELINE_SETTINGS": ";".join(
                f"{key}|{value}"
                for key, value in sorted(
                    {"TimeZone" if key == "timezone" else key: value for key, value in stage["settings"].items()}.items()
                )
            ),
            "BASELINE_BINDING_SHA256": file_sha256(inputs / "binding.json"),
            "BASELINE_DUMP_SHA256": stage["initialization"]["logical_dump"],
            "BASELINE_FILESTORE_SHA256": filestore_sha256,
        },
        "copies": [
            {"source": str(inputs.resolve()), "destination": "/workspace/baseline/input"},
            {
                "source": str(Path(__file__).resolve().parents[1] / "docker/scripts/odoo-baseline-producer.sh"),
                "destination": "/workspace/baseline/producer.sh",
            },
        ],
    }


def verify_baseline(archive: Path, *, sha256: str, binding: Path, max_expanded_bytes: int) -> dict[str, Any]:
    if max_expanded_bytes <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("A positive expansion budget and qualified archive SHA256 are required")
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Baseline requires a regular archive")
    expected = read_binding(binding)
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != sha256:
            raise ValueError("Baseline archive does not match its qualified original")
        handle.seek(0)
        return _verify_members(handle, expected, max_expanded_bytes)


def _verify_members(handle: BinaryIO, expected: dict[str, Any], max_expanded_bytes: int) -> dict[str, Any]:
    required = {"binding.json", "pgdata/PG_VERSION", "pgdata/global/pg_control", "filestore"}
    names: set[str] = set()
    directories: set[str] = set()
    expanded = 0
    with tarfile.open(fileobj=handle, mode="r:") as source:
        for member in source:
            path = PurePosixPath(member.name)
            name = member.name.rstrip("/")
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                or any(part in {".", ".."} for part in path.parts)
                or name in names
                or not (member.isfile() or member.isdir())
                or member.mode & 0o7000
                or member.pax_headers
                or member.sparse is not None
                or (path.parts[0] not in {"pgdata", "filestore"} and name != "binding.json")
            ):
                raise ValueError(f"Unadmitted baseline archive member: {member.name}")
            if name.startswith("pgdata/pg_tblspc/") or path.name in {
                "postmaster.pid",
                "standby.signal",
                "recovery.signal",
                "backup_label",
                "tablespace_map",
                "recovery.conf",
            }:
                raise ValueError(f"External or unfinished PostgreSQL state: {member.name}")
            if any(parent.as_posix() in names and parent.as_posix() not in directories for parent in path.parents):
                raise ValueError(f"Non-directory baseline parent: {member.name}")
            names.add(name)
            if member.isdir():
                directories.add(name)
            expanded += member.size
            if expanded > max_expanded_bytes:
                raise ValueError("Baseline exceeds the expanded-byte budget")
            if name == "binding.json":
                stream = source.extractfile(member)
                if stream is None or member.size > 1024 * 1024 or json.load(stream) != expected:
                    raise ValueError("Baseline stage bindings differ from the current inputs")
            if name == "pgdata/PG_VERSION":
                stream = source.extractfile(member)
                major = expected["postgres_version"].split()[2].split(".")[0]
                if stream is None or member.size > 32 or stream.read().decode().strip() != major:
                    raise ValueError("Baseline PostgreSQL format differs from its runtime")
            if name == "pgdata/postgresql.auto.conf":
                stream = source.extractfile(member)
                if stream is None or member.size > 1024 * 1024:
                    raise ValueError("Unqualified PostgreSQL automatic configuration")
                if any(line.strip() and not line.lstrip().startswith(b"#") for line in stream):
                    raise ValueError("Baseline contains automatic PostgreSQL settings")
    if not required <= names or "filestore" not in directories:
        raise ValueError("Baseline is missing its database, filestore or binding")
    if any(parent.as_posix() in names - directories for name in names for parent in PurePosixPath(name).parents):
        raise ValueError("Baseline has a file in place of a parent directory")
    return {"expanded_bytes": expanded, "binding": expected}


def database_input(archive: Path, *, sha256: str, binding: Path, max_expanded_bytes: int) -> dict[str, Any]:
    verified = verify_baseline(archive, sha256=sha256, binding=binding, max_expanded_bytes=max_expanded_bytes)
    stage = verified["binding"]
    archive_path = "/workspace/baseline/stage.tar"
    script = "\n".join(
        [
            "set -eu",
            "mkdir /workspace/baseline/results",
            "date +%s%N > /workspace/baseline/results/start-nanoseconds.txt",
            f"printf '%s  %s\\n' {shlex.quote(sha256)} {archive_path} | sha256sum --check --status",
            f'test "$(postgres --version)" = {shlex.quote(stage["postgres_version"])}',
            f'test "$(uname -m)" = {shlex.quote(stage["architecture"])}',
            ': "${PGDATA:?}"',
            'test -z "$(find "$PGDATA" -mindepth 1 -print -quit)"',
            "date +%s%N > /workspace/baseline/results/verified-nanoseconds.txt",
            f'tar --extract --file={archive_path} --directory="$PGDATA" --strip-components=1 --no-same-owner pgdata',
            "date +%s%N > /workspace/baseline/results/extracted-nanoseconds.txt",
            'test "$(LC_ALL=C pg_controldata "$PGDATA" | sed -n \'s/^Database cluster state: *//p\')" = "shut down"',
            'test ! -e "$PGDATA/postmaster.pid"',
            'chown -R postgres:postgres "$PGDATA"',
            'chmod 700 "$PGDATA"',
            "printf '%s\\n' "
            + shlex.quote("listen_addresses = '*'\n" + "\n".join(f"{key} = '{value}'" for key, value in stage["settings"].items()))
            + " > /workspace/baseline/postgresql.conf",
            "date +%s%N > /workspace/baseline/results/preexec-nanoseconds.txt",
            "exec docker-entrypoint.sh postgres -c config_file=/workspace/baseline/postgresql.conf",
        ]
    )
    return {
        "name": "database",
        "image": stage["postgres_image"],
        "command": ["bash", "-c", script],
        "copies": [{"source": str(archive.resolve()), "destination": archive_path}],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--max-expanded-bytes", type=int, required=True)
    arguments = parser.parse_args()
    try:
        container = database_input(**vars(arguments))
    except (ValueError, OSError, tarfile.TarError) as error:
        parser.error(str(error))
    else:
        print(json.dumps(container, indent=2))


if __name__ == "__main__":
    main()
