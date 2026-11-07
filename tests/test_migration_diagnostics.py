from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

Diagnostics = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/migration_diagnostics.py"))[
    "MigrationDiagnostics"
]


class MigrationDiagnosticsTests(unittest.TestCase):
    def test_mail_and_payment_report_missing_reference_inputs_and_keep_ordinary_failures(self) -> None:
        root = Path(__file__).resolve().parents[1] / "docker/scripts/nativeupgrade"
        scripts = (root / "mail/20.0.1.19/pre-server-action-view.py", root / "payment/20.0.2.0/pre-global-methods.py")
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.jsonl"
            module = ModuleType("odoo.modules.module")
            module.get_module_path = lambda _name: directory
            tools = ModuleType("odoo.tools")
            tools.config = SimpleNamespace(filestore=lambda _name: directory)
            original_errors = []
            with (
                patch.dict(sys.modules, {"odoo.modules.module": module, "odoo.tools": tools}),
                patch.dict(
                    os.environ,
                    {
                        "ODOO_MIGRATION_DIAGNOSTIC_REPORT": str(report),
                        "ODOO_COPY_PURPOSE": "migration",
                        "PLATFORM_INSTANCE": "rehearsal",
                        "OPENUPGRADE_TARGET_VERSION": "20.0",
                    },
                    clear=True,
                ),
            ):
                for source in scripts:
                    migrate = runpy.run_path(str(source))["migrate"]
                    migrate(Mock(dbname="copy"), "19.0.1.0")
                    with (
                        patch.dict(os.environ, {"ODOO_MIGRATION_DIAGNOSTIC_REPORT": ""}),
                        self.assertRaises(RuntimeError) as original,
                    ):
                        migrate(Mock(dbname="copy"), "19.0.1.0")
                    original_errors.append(f"{type(original.exception).__name__}: {original.exception}")
            failures = [json.loads(line) for line in report.read_text().splitlines() if json.loads(line)["status"] == "failed"]
            self.assertEqual(len(failures), len(scripts))
            self.assertEqual([event["detail"] for event in failures], original_errors)

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
