from __future__ import annotations

import json
import shlex
import xml.etree.ElementTree as element_tree
from pathlib import Path

from .manifest import RunConfigurationDefinition, WorkspaceManifest


def write_pycharm_support_files(
    *,
    manifest: WorkspaceManifest,
    tenant_repo_path: Path,
    workspace_path: Path,
    generated_odoo_conf_path: Path,
    attached_paths: tuple[Path, ...],
) -> tuple[Path, tuple[Path, ...]]:
    generated_directory = workspace_path / ".generated" / "pycharm"
    generated_directory.mkdir(parents=True, exist_ok=True)
    metadata_path = generated_directory / "project-metadata.json"
    metadata_payload = {
        "tenant": manifest.tenant,
        "mode": manifest.ide.mode,
        "workspace_path": str(workspace_path),
        "tenant_repo_path": str(tenant_repo_path),
        "focus_paths": list(manifest.ide.focus_paths),
        "attached_paths": [str(path) for path in attached_paths],
        "generated_odoo_conf_path": str(generated_odoo_conf_path),
    }
    metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    run_configuration_paths = write_run_configurations(
        manifest=manifest,
        tenant_repo_path=tenant_repo_path,
    )
    return metadata_path, run_configuration_paths


def write_run_configurations(*, manifest: WorkspaceManifest, tenant_repo_path: Path) -> tuple[Path, ...]:
    run_directory = tenant_repo_path / ".run"
    run_directory.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []
    for run_configuration in manifest.ide.run_configurations:
        safe_name = _safe_run_configuration_name(run_configuration.name)
        run_configuration_path = run_directory / f"{safe_name}.run.xml"
        root = element_tree.Element("component", {"name": "ProjectRunConfigurationManager"})
        configuration = element_tree.SubElement(
            root,
            "configuration",
            {
                "default": "false",
                "name": run_configuration.name,
                "type": "ShConfigurationType",
            },
        )
        command_text = " ".join(shlex.quote(part) for part in run_configuration.command)
        option_values = {
            "SCRIPT_TEXT": command_text,
            "INDEPENDENT_SCRIPT_PATH": "true",
            "SCRIPT_PATH": "",
            "SCRIPT_OPTIONS": "",
            "INDEPENDENT_SCRIPT_WORKING_DIRECTORY": "true",
            "SCRIPT_WORKING_DIRECTORY": run_configuration.working_directory,
            "INDEPENDENT_INTERPRETER_PATH": "true",
            "INTERPRETER_PATH": run_configuration.shell_path,
            "INTERPRETER_OPTIONS": "",
            "EXECUTE_IN_TERMINAL": _xml_bool(run_configuration.execute_in_terminal),
            "EXECUTE_SCRIPT_FILE": "false",
        }
        for option_name, option_value in option_values.items():
            element_tree.SubElement(configuration, "option", {"name": option_name, "value": option_value})
        element_tree.SubElement(configuration, "envs")
        element_tree.SubElement(configuration, "method", {"v": "2"})
        _indent_xml(root)
        element_tree.ElementTree(root).write(run_configuration_path, encoding="utf-8", xml_declaration=False)
        run_configuration_path.write_text(run_configuration_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        written_paths.append(run_configuration_path)
    return tuple(written_paths)


def _safe_run_configuration_name(name: str) -> str:
    safe_name = name.replace("/", "-").replace(":", "-")
    return safe_name


def _xml_bool(value: bool) -> str:
    return "true" if value else "false"


def _indent_xml(element: element_tree.Element, level: int = 0) -> None:
    indentation = "  "
    child_indentation = "\n" + indentation * (level + 1)
    closing_indentation = "\n" + indentation * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = child_indentation
        for child in element:
            _indent_xml(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = closing_indentation
    elif level and (not element.tail or not element.tail.strip()):
        element.tail = closing_indentation
