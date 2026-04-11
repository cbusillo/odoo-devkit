from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TenantOverlayScaffoldResult:
    output_directory: Path
    written_paths: tuple[Path, ...]


def scaffold_tenant_overlay(*, repo_root: Path, output_directory: Path, tenant: str, force: bool) -> TenantOverlayScaffoldResult:
    template_root = repo_root / "templates" / "tenant-overlay"
    if not template_root.exists():
        raise ValueError(f"Tenant overlay templates not found: {template_root}")

    output_directory.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []
    for template_path in sorted(template_root.rglob("*")):
        if template_path.is_dir():
            continue
        relative_path = template_path.relative_to(template_root)
        destination_path = output_directory / relative_path
        if destination_path.exists() and not force:
            raise ValueError(f"Refusing to overwrite existing file without --force: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        rendered_text = _render_template(template_path.read_text(encoding="utf-8"), tenant=tenant)
        destination_path.write_text(rendered_text, encoding="utf-8")
        written_paths.append(destination_path)

    return TenantOverlayScaffoldResult(output_directory=output_directory, written_paths=tuple(written_paths))


def _render_template(template_text: str, *, tenant: str) -> str:
    return template_text.replace("replace-me", tenant)
