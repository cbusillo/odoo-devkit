from collections.abc import Iterable
from pathlib import Path

PROJECT_ADDONS_ROOT = Path("/opt/project/addons")


def addon_group_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir()
        and not child.name.startswith((".", "__"))
        and not (child / "__manifest__.py").is_file()
        and not (child / "__openerp__.py").is_file()
        and any(
            (addon / "__init__.py").is_file() and ((addon / "__manifest__.py").is_file() or (addon / "__openerp__.py").is_file())
            for addon in child.iterdir()
            if addon.is_dir()
        )
    ]


def expand_addon_paths(paths: Iterable[Path], *, source_root: Path | None = None) -> list[Path]:
    groups = addon_group_dirs(source_root if source_root is not None else PROJECT_ADDONS_ROOT)
    resolved: list[Path] = []
    for path in paths:
        additions = [path]
        if path == PROJECT_ADDONS_ROOT:
            additions.extend(PROJECT_ADDONS_ROOT / group.name for group in groups)
        for addition in additions:
            if addition not in resolved:
                resolved.append(addition)
    return resolved
