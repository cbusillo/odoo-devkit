from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from .manifest import WorkspaceManifest
from .runtime_environment import sanitized_subprocess_environment


def prepare_odoo_sources(
    *, manifest: WorkspaceManifest, source_path: Path, expected_commit: str, expected_series: str
) -> dict[str, object]:
    """Prepare local project content roots before opening the exact tenant worktree."""
    project_path = manifest.tenant_repo.resolve_path(manifest_directory=manifest.manifest_directory)
    if manifest.ide.mode != "tenant_repo" or project_path != manifest.manifest_directory:
        raise ValueError("IDE preparation requires tenant_repo mode and a manifest in the exact tenant checkout")
    if Path(_git(project_path, "rev-parse", "--show-toplevel")).resolve() != project_path:
        raise ValueError("The tenant project must be a Git worktree root")
    source_path = source_path.expanduser().resolve()
    if source_path.is_relative_to(project_path) or project_path.is_relative_to(source_path):
        raise ValueError("Keep the Odoo dependency checkout outside the tenant worktree")
    source_commit, source_series = _verify_source(source_path, expected_commit, expected_series)

    idea_path = project_path / ".idea"
    if idea_path.is_symlink():
        raise ValueError("IDE preparation cannot follow a symlinked .idea directory")
    modules_path = idea_path / "modules.xml"
    module_path, module_root, modules_root = _project_module(project_path, modules_path)
    manager = module_root.find("./component[@name='NewModuleRootManager']")
    if manager is None:
        raise ValueError(f"Missing NewModuleRootManager in {module_path.name}")
    attached = False
    for content in manager.findall("content"):
        content_path = _idea_path(content.get("url", ""), project_path, _module_directory(module_path)).resolve()
        if content_path == source_path:
            attached = True
        elif (content_path / "odoo" / "release.py").is_file():
            raise ValueError("A different Odoo source root is already attached; reconcile it in Project Structure first")

    writes: dict[Path, bytes] = {}
    if not attached:
        ElementTree.SubElement(manager, "content", {"url": _idea_url(source_path)})
        writes[module_path] = _xml_bytes(module_root)
    if modules_root is not None:
        writes[modules_path] = _xml_bytes(modules_root)
    # Preflight every destination before writing any project metadata. Tracked
    # configuration is policy, not a place to persist a machine-local source path.
    for path in writes:
        _require_local_ignored_file(project_path, path)
    written_paths = []
    try:
        for path, content_bytes in writes.items():
            _write_atomic(path, content_bytes)
            written_paths.append(path)
    except OSError:
        if modules_root is not None:
            # Both files are newly owned when creating a project. Do not leave
            # an orphan module if publishing its modules.xml fails.
            for path in written_paths:
                if path.read_bytes() == writes[path]:
                    path.unlink()
        raise
    return {
        "project_path": str(project_path),
        "module_path": str(module_path),
        "odoo_source_path": str(source_path),
        "odoo_commit": source_commit,
        "odoo_series": source_series,
        "changed": bool(writes),
        "written_paths": [str(path) for path in writes],
    }


def _verify_source(source_path: Path, expected_commit: str, expected_series: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_commit):
        raise ValueError("--odoo-commit must be a full 40-character Git commit SHA")
    if Path(_git(source_path, "rev-parse", "--show-toplevel")).resolve() != source_path:
        raise ValueError("--odoo-source must name the Odoo Git checkout root")
    commit = _git(source_path, "rev-parse", "HEAD")
    if commit != expected_commit.lower():
        raise ValueError(f"Odoo source commit mismatch: expected {expected_commit}, found {commit}")
    if _git(source_path, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Odoo source checkout has local changes; use a clean pinned dependency checkout")
    if (
        not (source_path / "odoo" / "addons" / "base" / "__manifest__.py").is_file()
        or not (source_path / "addons" / "web" / "__manifest__.py").is_file()
    ):
        raise ValueError("Odoo source checkout must contain the core package and community addons")
    # Read release metadata without importing or executing dependency code.
    release_tree = ast.parse((source_path / "odoo" / "release.py").read_text(encoding="utf-8"))
    series = None
    for statement in release_tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "version_info" for target in statement.targets
        ):
            if isinstance(statement.value, ast.Tuple) and len(statement.value.elts) >= 2:
                series = ".".join(str(ast.literal_eval(part)) for part in statement.value.elts[:2])
    if series != expected_series:
        raise ValueError(f"Odoo source series mismatch: expected {expected_series}, found {series}")
    return commit, series


def _project_module(project_path: Path, modules_path: Path) -> tuple[Path, ElementTree.Element, ElementTree.Element | None]:
    if modules_path.is_symlink():
        raise ValueError("IDE preparation cannot follow a symlinked modules.xml")
    if not modules_path.exists():
        if list(modules_path.parent.glob("*.iml")):
            raise ValueError("Existing module files have no modules.xml; reconcile the project in PyCharm first")
        module_name = project_path.name
        attributes = {"type": "PYTHON_MODULE", "version": "4"}
        pyproject_path = project_path / "pyproject.toml"
        if pyproject_path.is_file():
            pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            project_table = pyproject.get("project", {})
            if not isinstance(project_table, dict):
                raise ValueError("Expected [project] to be a table in pyproject.toml")
            if "name" in project_table:
                module_name = project_table["name"]
                attributes["external.system.id"] = "pyproject.toml"
        if not isinstance(module_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", module_name):
            raise ValueError("Cannot derive a safe PyCharm module name from the tenant project")
        module_path = modules_path.parent / f"{module_name}.iml"
        module_root = ElementTree.Element("module", attributes)
        manager = ElementTree.SubElement(module_root, "component", {"name": "NewModuleRootManager"})
        content = ElementTree.SubElement(manager, "content", {"url": _idea_url(project_path)})
        ElementTree.SubElement(content, "excludeFolder", {"url": _idea_url(project_path / ".venv")})
        ElementTree.SubElement(manager, "orderEntry", {"type": "inheritedJdk"})
        ElementTree.SubElement(manager, "orderEntry", {"type": "sourceFolder", "forTests": "false"})
        modules_root = ElementTree.Element("project", {"version": "4"})
        component = ElementTree.SubElement(modules_root, "component", {"name": "ProjectModuleManager"})
        modules = ElementTree.SubElement(component, "modules")
        relative_path = f"$PROJECT_DIR$/.idea/{module_name}.iml"
        ElementTree.SubElement(modules, "module", {"fileurl": f"file://{relative_path}", "filepath": relative_path})
        return module_path, module_root, modules_root
    modules_root = _read_xml(modules_path)
    candidates = []
    for module in modules_root.findall("./component[@name='ProjectModuleManager']/modules/module"):
        module_path = _idea_path(module.get("filepath") or module.get("fileurl", ""), project_path, modules_path.parent)
        # Foreign projects in a multi-project window remain entirely untouched.
        if not module_path.is_relative_to(project_path) or module_path.suffix != ".iml":
            continue
        if module_path.is_symlink():
            raise ValueError("IDE preparation cannot follow a symlinked module")
        module_root = _read_xml(module_path)
        content_paths = [
            _idea_path(content.get("url", ""), project_path, _module_directory(module_path)).resolve()
            for content in module_root.findall("./component[@name='NewModuleRootManager']/content")
        ]
        if module_root.get("type") == "PYTHON_MODULE" and project_path in content_paths:
            candidates.append((module_path, module_root))
    if len(candidates) != 1:
        raise ValueError(
            "IDE preparation requires exactly one Python module rooted at the tenant; reconcile Project Structure first"
        )
    module_path, module_root = candidates[0]
    return module_path, module_root, None


def _idea_url(path: Path) -> str:
    # IntelliJ VFS URLs contain a raw path, not a percent-encoded URI.
    return "file://" + path.as_posix()


def _idea_path(value: str, project_path: Path, module_directory: Path) -> Path:
    value = value.removeprefix("file://")
    value = value.replace("$PROJECT_DIR$", str(project_path)).replace("$MODULE_DIR$", str(module_directory))
    value = value.replace("$USER_HOME$", str(Path.home()))
    if not value or "$" in value or not Path(value).is_absolute():
        raise ValueError("Cannot resolve an existing IDE path; reconcile Project Structure first")
    return Path(os.path.abspath(value))


def _module_directory(module_path: Path) -> Path:
    # PyCharm's directory-based project store expands MODULE_DIR relative to
    # the project, even though its module file lives directly under .idea.
    return module_path.parent.parent if module_path.parent.name == ".idea" else module_path.parent


def _read_xml(path: Path) -> ElementTree.Element:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    try:
        return ElementTree.parse(path, parser=parser).getroot()
    except ElementTree.ParseError as error:
        raise ValueError(f"Invalid IDE XML in {path.name}: {error}") from error


def _xml_bytes(root: ElementTree.Element) -> bytes:
    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _require_local_ignored_file(project_path: Path, path: Path) -> None:
    if path.is_symlink() or not path.resolve().is_relative_to(project_path):
        raise ValueError("IDE preparation cannot write through a symlink outside the project")
    relative_path = path.relative_to(project_path).as_posix()
    if _git(project_path, "ls-files", "--", relative_path):
        raise ValueError(f"Refusing to write machine-local Odoo source paths into tracked {relative_path}")
    ignored = subprocess.run(
        ["git", "-C", str(project_path), "check-ignore", "-q", "--", relative_path],
        env=sanitized_subprocess_environment(),
    )
    if ignored.returncode != 0:
        raise ValueError(f"{relative_path} must already be Git-ignored before IDE preparation")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
        env=sanitized_subprocess_environment(),
    )
    if result.returncode != 0:
        raise ValueError(f"Cannot read Git state in {directory}: {result.stderr.strip()}")
    return result.stdout.strip()
