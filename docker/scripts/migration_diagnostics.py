from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
_logger = logging.getLogger(__name__)


class MigrationDiagnostics:
    def __init__(self) -> None:
        report = os.environ.get("ODOO_MIGRATION_DIAGNOSTIC_REPORT")
        self.path = Path(report) if report else None
        self.failed: set[str] = set()
        if self.path is not None and (
            os.environ.get("ODOO_COPY_PURPOSE") != "migration"
            or os.environ.get("PLATFORM_INSTANCE") != "rehearsal"
            or os.environ.get("OPENUPGRADE_TARGET_VERSION") != "20.0"
        ):
            raise RuntimeError("Migration diagnostics require the disposable 19-to20 rehearsal")

    def record(self, name: str, status: str, detail: str = "") -> None:
        if self.path is None:
            return
        if status == "failed":
            self.failed.add(name)
        event = {"check": name, "status": status, "detail": detail}
        with self.path.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if status != "passed":
            _logger.error("Migration diagnostic %s: %s: %s", status, name, detail)

    def check(self, name: str, action: Callable[[], T], fallback: T, *, cursor: Any = None) -> T:
        if self.path is None:
            return action()
        if cursor is not None:
            cursor.execute("SAVEPOINT migration_diagnostic_check")
        try:
            result = action()
        except Exception as error:
            if cursor is not None:
                cursor.execute("ROLLBACK TO SAVEPOINT migration_diagnostic_check")
            self.record(name, "failed", f"{type(error).__name__}: {error}")
            self.record(
                name + ": remaining guards", "blocked", "Coverage beyond the reported error is unproved; changes were skipped"
            )
            return fallback
        finally:
            if cursor is not None:
                cursor.execute("RELEASE SAVEPOINT migration_diagnostic_check")
        self.record(name, "passed")
        return result
