from __future__ import annotations

import os
import runpy
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

SALE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "docker/scripts/nativeupgrade/sale/20.0.1.0/pre-company-lead-time.py")
)


class SaleLeadTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.util = ModuleType("odoo.upgrade.util")
        self.util.make_field_company_dependent = Mock()
        self.cr = Mock()

    def test_global_lead_times_use_all_existing_companies_without_discarding_zero(self) -> None:
        self.cr.fetchall.side_effect = [[(9, "integer", True, False)], [("integer",)], [], [(1,)]]
        with patch.dict(sys.modules, {"odoo.upgrade.util": self.util}):
            SALE["preserve_lead_time"](self.cr)
        self.util.make_field_company_dependent.assert_called_once_with(
            self.cr, "product.template", "sale_delay", "integer", company_field=None
        )

    def test_defaults_and_unknown_source_shapes_never_reach_the_destructive_vendor_helper(self) -> None:
        for readbacks in (
            [[]],
            [[(9, "float", True, False)]],
            [[(9, "integer", False, False)]],
            [[(9, "integer", True, False)], [("text",)]],
            [[(9, "integer", True, True)], [("integer",)]],
            [[(9, "integer", True, False)], [("integer",)], [(1,)]],
            [[(9, "integer", True, False)], [("integer",)], [], []],
        ):
            with self.subTest(readbacks=readbacks), patch.dict(sys.modules, {"odoo.upgrade.util": self.util}):
                self.cr.fetchall.side_effect = readbacks
                with self.assertRaises(RuntimeError):
                    SALE["preserve_lead_time"](self.cr)
                self.util.make_field_company_dependent.assert_not_called()

    def test_already_converted_field_keeps_its_defaults(self) -> None:
        self.cr.fetchall.side_effect = [[(9, "integer", True, True)], [("jsonb",)]]
        with patch.dict(sys.modules, {"odoo.upgrade.util": self.util}):
            SALE["preserve_lead_time"](self.cr)
        self.util.make_field_company_dependent.assert_not_called()
        self.assertEqual(self.cr.fetchall.call_count, 2)

    def test_other_crossings_do_not_change_data(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for version in (None, "18.0.1.0", "20.0.1.0"):
                SALE["migrate"](self.cr, version)
        self.cr.execute.assert_not_called()
