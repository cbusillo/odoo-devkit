from collections.abc import Iterable
from pathlib import Path


def addon_group_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir()
        and not child.name.startswith((".", "_"))
        and not (child / "__manifest__.py").is_file()
        and not (child / "__openerp__.py").is_file()
        and any(child.glob("*/__manifest__.py"))
    ]


def expand_addon_paths(paths: Iterable[Path]) -> list[Path]:
    resolved = list(dict.fromkeys(paths))
    for root in tuple(resolved):
        for group in addon_group_dirs(root):
            if group not in resolved:
                resolved.append(group)
    return resolved
