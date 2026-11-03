from collections.abc import Iterable
from pathlib import Path

UPGRADE_UTIL_SOURCE = "odoo/upgrade-util@05fba727e9fd89dbff593df359ee6d3dde45f7b0"


def resolve_upgrade_util(addon_roots: Iterable[Path]) -> Path:
    sources = set()
    for root in addon_roots:
        for marker in (root / "_checkouts").glob("*.odoo-source"):
            if marker.is_symlink() or marker.read_text().strip() != UPGRADE_UTIL_SOURCE:
                continue
            checkout = marker.with_suffix("")
            source = checkout / "src"
            if checkout.is_symlink() or not (source / "util/__init__.py").is_file():
                raise ValueError("Incomplete official Odoo upgrade source")
            if not (source / "util/__init__.py").resolve().is_relative_to(checkout.resolve()):
                raise ValueError("Official Odoo upgrade source escapes its checkout")
            sources.add(source.resolve())
    if len(sources) != 1:
        raise ValueError("Missing or ambiguous exact official Odoo upgrade source")
    return sources.pop()
