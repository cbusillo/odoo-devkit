"""Read the producer's baked Python catalog from an immutable published image."""

from __future__ import annotations

import platform as host_platform
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .runtime_environment import sanitized_subprocess_environment

BASE_PYTHON_CATALOG_PATH = "/opt/launchplane/base-python-constraints.txt"
_EXACT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+_-]*)")
_IMMUTABLE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
PLATFORM_MACHINES = {"linux/amd64": ("x86_64", "AMD64"), "linux/arm64": ("aarch64", "arm64")}


@dataclass(frozen=True)
class BasePythonCatalog:
    image: str
    platform: str
    requirements: tuple[str, ...]


def native_base_platform() -> str:
    machine = host_platform.machine()
    for platform, machines in PLATFORM_MACHINES.items():
        if machine in machines:
            return platform
    raise ValueError(f"Unsupported base Python platform for host architecture {machine}")


def parse_base_python_catalog(text: str, *, image: str, platform: str) -> BasePythonCatalog:
    versions: dict[str, str] = {}
    if _IMMUTABLE.fullmatch(image) is None or platform not in PLATFORM_MACHINES:
        raise ValueError(f"Base Python catalog requires an immutable image and supported platform: {image} ({platform})")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT.fullmatch(line)
        if match is None:
            raise ValueError(f"Malformed base Python catalog entry {line!r}: {image} ({platform})")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in versions:
            raise ValueError(f"Duplicate base Python catalog package {name}: {image} ({platform})")
        versions[name] = match[2]
    if not versions:
        raise ValueError(f"Missing or empty base Python catalog: {image} ({platform})")
    return BasePythonCatalog(image, platform, tuple(f"{name}=={version}" for name, version in sorted(versions.items())))


def read_base_python_catalogs(*, image: str, platforms: tuple[str, ...]) -> tuple[BasePythonCatalog, ...]:
    """BuildKit extracts files without running a foreign-platform interpreter."""
    if _IMMUTABLE.fullmatch(image) is None:
        raise ValueError(f"Cannot read base Python catalog from unresolved image: {image}")
    catalogs = []
    for platform in platforms:
        if platform not in PLATFORM_MACHINES:
            raise ValueError(f"Unsupported base Python catalog platform: {image} ({platform})")
        with tempfile.TemporaryDirectory(prefix="odoo-base-python-") as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text(
                f"FROM {image} AS base\nFROM scratch\nCOPY --from=base {BASE_PYTHON_CATALOG_PATH} /catalog.txt\n",
                encoding="utf-8",
            )
            environment = sanitized_subprocess_environment()
            environment.pop("ADDON_CI_SOURCE_TOKEN", None)
            result = subprocess.run(
                ["docker", "buildx", "build", "--platform", platform, "--output", f"type=local,dest={root / 'out'}", str(root)],
                capture_output=True,
                text=True,
                env=environment,
                timeout=600,
            )
            if result.returncode:
                raise ValueError(f"Cannot read base Python catalog: {image} ({platform}): {result.stderr.strip()[-2000:]}")
            try:
                content = (root / "out" / "catalog.txt").read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise ValueError(f"Missing or malformed base Python catalog: {image} ({platform})") from error
            catalogs.append(parse_base_python_catalog(content, image=image, platform=platform))
    if not catalogs:
        raise ValueError(f"Base Python catalog requires at least one platform: {image}")
    return tuple(catalogs)
