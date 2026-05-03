from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path


def resolve_pycharm_addons_paths(*, repo_root: Path, addons_paths: tuple[str, ...]) -> list[str]:
    """Render PyCharm addons_path values without mirroring remote sources locally.

    The runtime stack uses container paths. Keep those values as-is, except map
    the project addons mount to the local workspace path so local addons stay
    editable.
    """

    resolved_paths: list[str] = []
    for addons_path in addons_paths:
        if addons_path == "/opt/project/addons":
            resolved_paths.append(str(repo_root / "addons"))
            continue
        if addons_path.startswith("/opt/project/addons/"):
            relative_path = addons_path.removeprefix("/opt/project/addons/")
            resolved_paths.append(str(repo_root / "addons" / relative_path))
            continue
        resolved_paths.append(addons_path)
    return resolved_paths


def write_pycharm_odoo_conf(
    *,
    repo_root: Path,
    context_name: str,
    instance_name: str,
    database_name: str,
    db_host_port: int,
    state_path: Path,
    addons_paths: tuple[str, ...],
    source_environment: Mapping[str, str],
    host_addons_paths: tuple[str, ...] | None = None,
) -> Path:
    """Write an IDE-oriented Odoo config for local tooling.

    This intentionally avoids copying Odoo core or enterprise sources into the
    repo. PyCharm remote interpreters should resolve those from remote sources
    managed by the IDE itself.
    """

    ide_directory = repo_root / ".platform" / "ide"
    ide_directory.mkdir(parents=True, exist_ok=True)
    ide_config_path = ide_directory / f"{context_name}.{instance_name}.odoo.conf"

    rendered_addons_paths = (
        list(host_addons_paths)
        if host_addons_paths is not None
        else resolve_pycharm_addons_paths(repo_root=repo_root, addons_paths=addons_paths)
    )
    host_data_directory = state_path / "data"

    lines = [
        "[options]",
        f"db_name = {database_name}",
        f"db_user = {source_environment.get('ODOO_DB_USER', 'odoo')}",
        f"db_password = {source_environment.get('ODOO_DB_PASSWORD', '')}",
        "db_host = 127.0.0.1",
        f"db_port = {db_host_port}",
        "list_db = False",
        f"addons_path = {','.join(rendered_addons_paths)}",
        f"data_dir = {host_data_directory}",
        "",
        f"; context={context_name}",
        f"; instance={instance_name}",
        "; generated_for=pycharm",
    ]
    # PyCharm Odoo tooling needs the local database password in the generated
    # config. The file lives under the tenant-local .platform tree and is
    # restricted to the current user when the platform permits chmod.
    # codeql[py/clear-text-storage-sensitive-data]
    ide_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ide_config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Best effort only: some filesystems and mounts do not support mode
        # changes, but the generated config is still usable.
        pass
    return ide_config_path
