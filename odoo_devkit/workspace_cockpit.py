from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceCockpitRepoDefinition:
    label: str
    path: str
    repo_name: str
    group: str
    role: str | None = None


@dataclass(frozen=True)
class WorkspaceCockpitManifest:
    schema_version: int
    manifest_path: Path
    repos: tuple[WorkspaceCockpitRepoDefinition, ...]
    plans_directory: str = "~/.codex/plans"

    @property
    def manifest_directory(self) -> Path:
        return self.manifest_path.parent


@dataclass(frozen=True)
class WorkspaceCockpitSyncResult:
    output_directory: Path
    manifest_path: Path
    written_paths: tuple[Path, ...]


def load_workspace_cockpit_manifest(manifest_path: Path) -> WorkspaceCockpitManifest:
    manifest_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    schema_version = int(manifest_data.get("schema_version", 0))
    if schema_version != 1:
        raise ValueError(f"Unsupported workspace-cockpit schema_version: {schema_version}")

    repos_value = manifest_data.get("repos")
    if not isinstance(repos_value, list) or not repos_value:
        raise ValueError("Expected [[repos]] entries in workspace-cockpit manifest")

    repos = tuple(_parse_repo_definition(entry) for entry in repos_value)
    _validate_repo_definitions(repos)
    return WorkspaceCockpitManifest(
        schema_version=schema_version,
        manifest_path=manifest_path.resolve(),
        repos=repos,
        plans_directory=_read_optional_string(manifest_data, "plans_directory") or "~/.codex/plans",
    )


def sync_workspace_cockpit(
    *,
    manifest: WorkspaceCockpitManifest,
    output_directory: Path | None = None,
    overwrite_existing: bool,
) -> WorkspaceCockpitSyncResult:
    output_directory = output_directory or manifest.manifest_directory
    output_directory.mkdir(parents=True, exist_ok=True)

    file_contents = {
        output_directory / "AGENTS.md": _render_workspace_agents(manifest),
        output_directory / "docs" / "README.md": _render_workspace_docs_index(manifest),
        output_directory / "docs" / "session-prompt.md": _render_workspace_session_prompt(manifest),
    }
    written_paths: list[Path] = []
    for path, contents in file_contents.items():
        if path.exists() and not overwrite_existing:
            raise ValueError(f"Refusing to overwrite existing file without --force: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        written_paths.append(path)

    return WorkspaceCockpitSyncResult(
        output_directory=output_directory,
        manifest_path=manifest.manifest_path,
        written_paths=tuple(written_paths),
    )


def _parse_repo_definition(entry: object) -> WorkspaceCockpitRepoDefinition:
    if not isinstance(entry, dict):
        raise ValueError("Expected [[repos]] entries to be tables")
    group = _read_required_string(entry, "group")
    if group not in {"primary", "upstream_image"}:
        raise ValueError(f"Unsupported workspace-cockpit repo group: {group}")
    path = _read_required_string(entry, "path")
    if Path(path).is_absolute():
        raise ValueError(f"Workspace cockpit repo paths must be relative: {path}")
    return WorkspaceCockpitRepoDefinition(
        label=_read_required_string(entry, "label"),
        path=path,
        repo_name=_read_required_string(entry, "repo_name"),
        group=group,
        role=_read_optional_string(entry, "role"),
    )


def _validate_repo_definitions(repos: tuple[WorkspaceCockpitRepoDefinition, ...]) -> None:
    seen_paths: set[str] = set()
    for repo in repos:
        if repo.path in seen_paths:
            raise ValueError(f"Duplicate workspace-cockpit repo path: {repo.path}")
        seen_paths.add(repo.path)

    _require_single_role(repos, role="devkit")
    _require_single_role(repos, role="control_plane")


def _require_single_role(repos: tuple[WorkspaceCockpitRepoDefinition, ...], *, role: str) -> None:
    matching_repos = [repo for repo in repos if repo.role == role]
    if len(matching_repos) != 1:
        raise ValueError(f"Expected exactly one workspace-cockpit repo with role {role!r}")


def _render_workspace_agents(manifest: WorkspaceCockpitManifest) -> str:
    primary_repos = _repos_for_group(manifest, "primary")
    upstream_repos = _repos_for_group(manifest, "upstream_image")
    devkit_repo = _repo_for_role(manifest, "devkit")
    sync_command = f"uv --directory {devkit_repo.path} run platform workspace sync-cockpit-root --config workspace-cockpit.toml"
    repo_map_lines = "\n".join(_format_repo_map_line(repo) for repo in primary_repos)
    upstream_lines = "\n".join(_format_repo_map_line(repo) for repo in upstream_repos)
    return (
        "# Workspace Cockpit\n\n"
        "This workspace is the shared Every Code cockpit for multi-repo Odoo work.\n\n"
        "- Start Every Code from this workspace root when the task spans multiple durable\n"
        "  repos.\n"
        "- Treat the repos under `sources/` as the primary system under construction.\n\n"
        "## Repo map\n\n"
        f"{repo_map_lines}\n\n"
        "## Upstream image repos\n\n"
        f"{upstream_lines}\n\n"
        "These are upstream runtime-contract repos, not new primary work centers.\n"
        "Bring them into scope when a slice touches base image behavior, enterprise\n"
        "layering, `/venv` ownership, addon path shaping, browser/devtools tooling, or\n"
        "image publish/promotion mechanics.\n\n"
        "## First reads\n\n"
        "- Open [docs/README.md](docs/README.md) in this workspace root first.\n"
        f"- Use [{devkit_repo.path}/AGENTS.md]({devkit_repo.path}/AGENTS.md) for the canonical\n"
        "  shared operating guide.\n"
        f"- Use [{devkit_repo.path}/docs/README.md]({devkit_repo.path}/docs/README.md) for the\n"
        "  canonical shared docs index.\n"
        f"- Refresh this cockpit with `{sync_command}`.\n"
        "- Use the tenant-specific `workspace.toml` manifests when you need to run\n"
        "  current local runtime commands through `odoo-devkit`.\n\n"
        "## Ownership split\n\n"
        "- `odoo-devkit` owns shared DX/runtime/workspace behavior plus local runtime\n"
        "  and explicit data workflows.\n"
        "- `odoo-control-plane` owns remote release actions, deployment truth, release\n"
        "  tuples, and promotion evidence.\n"
        "- Stable remote lanes are `testing` and `prod`.\n"
        "- Harbor PR previews replace any durable shared `dev` lane.\n\n"
        "## Notes\n\n"
        "- This cockpit root is regenerated from `workspace-cockpit.toml` through\n"
        "  `odoo-devkit`; keep the repo map and root guidance in that config instead\n"
        "  of hand-editing markdown entrypoints.\n"
        "- This is still a manual multi-repo cockpit root, not a tenant\n"
        "  `platform workspace sync` surface with runtime materialization.\n"
        "- Do not bring `odoo-ai` into the normal workspace context. If an explicit\n"
        "  archaeology task still needs it, treat that as an external reference rather\n"
        "  than part of the active repo map.\n"
        "- Commit as you go when a coherent slice is verified; prefer small,\n"
        "  reviewable commits over batching unrelated work until the end of the\n"
        "  session.\n"
    )


def _render_workspace_docs_index(manifest: WorkspaceCockpitManifest) -> str:
    primary_repos = _repos_for_group(manifest, "primary")
    upstream_repos = _repos_for_group(manifest, "upstream_image")
    devkit_repo = _repo_for_role(manifest, "devkit")
    primary_repo_lines = "\n".join(
        f"- {repo.label}: [{_docs_link_target(repo.path)}]({_docs_link_target(repo.path)})" for repo in primary_repos
    )
    upstream_lines = "\n".join(
        f"- {repo.label}: [{_docs_link_target(repo.path)}]({_docs_link_target(repo.path)})" for repo in upstream_repos
    )
    sync_command = f"uv --directory {devkit_repo.path} run platform workspace sync-cockpit-root --config workspace-cockpit.toml"
    return (
        "# Workspace Docs\n\n"
        "Use this workspace root when the session needs to reason about or edit\n"
        "multiple durable repos at once.\n\n"
        "## Primary repos\n\n"
        f"{primary_repo_lines}\n\n"
        "## Shared guides\n\n"
        f"- Shared operating guide: [{_docs_link_target(devkit_repo.path + '/AGENTS.md')}]({_docs_link_target(devkit_repo.path + '/AGENTS.md')})\n"
        f"- Shared docs index: [{_docs_link_target(devkit_repo.path + '/docs/README.md')}]({_docs_link_target(devkit_repo.path + '/docs/README.md')})\n"
        f"- Shared workspace CLI guide: [{_docs_link_target(devkit_repo.path + '/docs/tooling/workspace-cli.md')}]({_docs_link_target(devkit_repo.path + '/docs/tooling/workspace-cli.md')})\n"
        f"- Shared command patterns: [{_docs_link_target(devkit_repo.path + '/docs/tooling/command-patterns.md')}]({_docs_link_target(devkit_repo.path + '/docs/tooling/command-patterns.md')})\n\n"
        "## Upstream image repos\n\n"
        f"{upstream_lines}\n\n"
        "Use these when a slice touches image contracts, enterprise layering, `/venv`\n"
        "ownership, addon path shaping, browser/devtools tooling, or image publish and\n"
        "promotion behavior. They support the main system, but they are not the center\n"
        "of gravity for normal tenant/control-plane/devkit work.\n\n"
        "## External reference boundary\n\n"
        "- Keep the main working set in the repos above.\n"
        "- Do not include `odoo-ai` in the normal workspace flow. If an explicit\n"
        "  archaeology task still needs it, treat that checkout as an external\n"
        "  reference rather than part of the live repo map.\n\n"
        "## Working split\n\n"
        "- Use `odoo-devkit` for shared DX/runtime/workspace behavior and for local\n"
        "  runtime plus explicit data workflows.\n"
        "- Use `odoo-control-plane` for remote release actions, deployment truth,\n"
        "  release tuples, and promotion evidence.\n"
        "- Stable remote lanes are `testing` and `prod`.\n"
        "- Harbor PR previews replace any durable shared `dev` lane.\n\n"
        "## Operational notes\n\n"
        f"- This cockpit root is regenerated from `workspace-cockpit.toml` via `{sync_command}`.\n"
        "- Historical plans remain available under `/Users/cbusillo/.codex/plans/`\n"
        "  when you need rationale or prior sequencing.\n\n"
        "## Session prompt helper\n\n"
        "- Use [session-prompt.md](session-prompt.md) as the starting prompt template\n"
        "  for a new multi-repo Every Code session.\n"
    )


def _render_workspace_session_prompt(manifest: WorkspaceCockpitManifest) -> str:
    primary_repos = _repos_for_group(manifest, "primary")
    repo_map_lines = "\n".join(f"- {repo.path} -> {repo.repo_name}" for repo in primary_repos)
    return (
        "# Session Prompt Template\n\n"
        "Use this as a starting prompt for a new multi-repo Every Code session from the\n"
        "workspace root.\n\n"
        "```text\n"
        "You are working in the shared Odoo cockpit at the workspace root.\n\n"
        "Start by reading:\n"
        "- AGENTS.md in the workspace root\n"
        "- docs/README.md in the workspace root\n\n"
        "Repo map:\n"
        f"{repo_map_lines}\n\n"
        "Working rules:\n"
        "- Treat repos under sources/ as the primary system under construction.\n"
        "- Use odoo-devkit for shared DX/runtime/workspace behavior and local/data\n"
        "  workflows.\n"
        "- Use odoo-control-plane for remote release actions, deployment truth,\n"
        "  release tuples, and promotion evidence.\n"
        "- Stable remote lanes are testing and prod.\n"
        "- Harbor PR previews replace any durable shared dev lane.\n"
        "- Do not bring odoo-ai into the normal workspace context unless the task is\n"
        "  explicit archaeology.\n"
        "- Keep tenant repos thin and tenant-specific; fix shared behavior in devkit.\n"
        "- When `workspace-cockpit.toml`, the workspace root, and source repos\n"
        "  disagree, treat the source repos as the source of truth, then regenerate\n"
        "  the cockpit.\n\n"
        "When you change behavior, update the relevant source-repo docs in the same\n"
        "slice.\n"
        "```\n"
    )


def _repos_for_group(manifest: WorkspaceCockpitManifest, group: str) -> tuple[WorkspaceCockpitRepoDefinition, ...]:
    return tuple(repo for repo in manifest.repos if repo.group == group)


def _repo_for_role(manifest: WorkspaceCockpitManifest, role: str) -> WorkspaceCockpitRepoDefinition:
    for repo in manifest.repos:
        if repo.role == role:
            return repo
    raise ValueError(f"Expected workspace-cockpit repo with role {role!r}")


def _format_repo_map_line(repo: WorkspaceCockpitRepoDefinition) -> str:
    return f"- `{repo.path}` -> `{repo.repo_name}`"


def _docs_link_target(path: str) -> str:
    return (Path("..") / Path(path)).as_posix()


def _read_required_string(source: dict[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected {key} to be a non-empty string")
    return value


def _read_optional_string(source: dict[str, object], key: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected {key} to be a string when present")
    return value
