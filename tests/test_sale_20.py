from __future__ import annotations

import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SALE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "docker/scripts/nativeupgrade/sale/20.0.1.0/pre-company-lead-time.py")
)


class SaleLeadTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cr = Mock()

    def test_defaults_and_unknown_source_shapes_refuse_conversion(self) -> None:
        for readbacks in (
            [[]],
            [[(9, "float", True, False)]],
            [[(9, "integer", False, False)]],
            [[(9, "integer", True, False)], [("text", None)]],
            [[(9, "integer", True, True)], [("integer", None)]],
            [[(9, "integer", True, False)], [("integer", "0")]],
            [[(9, "integer", True, False)], [("integer", None)], [(1,)]],
            [[(9, "integer", True, False)], [("integer", None)], [], []],
        ):
            with self.subTest(readbacks=readbacks):
                self.cr.fetchall.side_effect = readbacks
                with self.assertRaises(RuntimeError):
                    SALE["preserve_lead_time"](self.cr)

    def test_already_converted_field_keeps_its_defaults(self) -> None:
        self.cr.fetchall.side_effect = [[(9, "integer", True, True)], [("jsonb", None)]]
        SALE["preserve_lead_time"](self.cr)
        self.assertEqual(self.cr.fetchall.call_count, 2)

    def test_other_crossings_do_not_change_data(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for version in (None, "18.0.1.0", "20.0.1.0"):
                SALE["migrate"](self.cr, version)
        self.cr.execute.assert_not_called()
