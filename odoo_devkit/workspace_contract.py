from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceSource:
    role: str
    name: str
    workspace_relative_path: Path
    resolved_path: Path
    declared_path: str | None
    declared_url: str | None
    declared_ref: str | None
    materialization: str
    editable: bool
