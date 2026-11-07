from __future__ import annotations

import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

Diagnostics = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/migration_diagnostics.py"))[
    "MigrationDiagnostics"
]


class MigrationDiagnosticsTests(unittest.TestCase):
    def test_ordinary_execution_keeps_the_original_failure(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            diagnostics = Diagnostics()
            with self.assertRaisesRegex(RuntimeError, "preserve"):
                diagnostics.check("cohort", lambda: self.reject(), None)

    def test_diagnostic_cannot_be_enabled_for_production_or_other_crossings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.jsonl"
            for purpose, instance, target in (
                ("migration", "prod", "20.0"),
                ("migration", "rehearsal", "19.0"),
                ("", "rehearsal", "20.0"),
            ):
                with (
                    self.subTest(purpose=purpose, instance=instance, target=target),
                    patch.dict(
                        os.environ,
                        {
                            "ODOO_MIGRATION_DIAGNOSTIC_REPORT": str(report),
                            "ODOO_COPY_PURPOSE": purpose,
                            "PLATFORM_INSTANCE": instance,
                            "OPENUPGRADE_TARGET_VERSION": target,
                        },
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    Diagnostics()
            self.assertFalse(report.exists())

    @staticmethod
    def reject() -> None:
        raise RuntimeError("preserve")
