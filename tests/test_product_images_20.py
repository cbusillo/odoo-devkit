from __future__ import annotations

import runpy
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

conversion = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/product_images_20.py"))


class Domain(list):
    def __init__(self, *values: Any) -> None:
        super().__init__(values[0] if len(values) == 1 else [values])

    def __and__(self, other: Domain) -> Domain:
        return Domain([*self, *other])


class Environment:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.loaded: set[int] = set()

    def flush_all(self) -> None:
        pass

    def invalidate_all(self, *, flush: bool) -> None:
        self.loaded.clear()


class Model:
    rows: list[dict[str, Any]] = []

    def __init__(self, environment: Environment, ids: list[int] | None = None, *, active_test: bool = True) -> None:
        self.env = environment
        self.ids = ids or []
        self.active_test = active_test

    def browse(self, ids: list[int]) -> Model:
        return type(self)(self.env, ids, active_test=self.active_test)

    def with_context(self, *, active_test: bool) -> Model:
        return type(self)(self.env, self.ids, active_test=active_test)

    def search(self, domain: list[tuple]) -> Model:
        selected = []
        for row in self.rows:
            if self.active_test and not row["active"]:
                continue
            if all(row[field] in value if operator == "in" else row[field] == value for field, operator, value in domain):
                selected.append(row["id"])
        return self.browse(selected)


class ProductImageConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        fields = ModuleType("odoo.fields")
        fields.Domain = Domain
        module = ModuleType("odoo")
        module.fields = fields
        self.modules = patch.dict(sys.modules, {"odoo": module, "odoo.fields": fields})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.capacity = conversion["IMAGE_BATCH_SIZE"]
        self.count = self.capacity * 2 + 1
        Model.rows = [
            {"id": identifier, "active": identifier != 3, "eligible": identifier != 7} for identifier in range(1, self.count + 1)
        ]

    def test_native_selection_is_bounded_without_restricting_nested_searches(self) -> None:
        seen = []
        nested = []

        def native(model: Model) -> None:
            selected = model.search([("eligible", "=", True)]).ids
            model.env.loaded.update(selected)
            if len(model.env.loaded) > model.env.capacity:
                raise MemoryError("native image cache exceeded its capacity")
            seen.extend(selected)
            nested.append(model.search([("id", "=", self.count)]).ids)

        bounded = conversion["bounded_image_conversion"](native)
        bounded(Model(Environment(self.capacity)))
        expected = [row["id"] for row in Model.rows if row["active"] and row["eligible"]]
        self.assertEqual(seen, expected)
        self.assertTrue(all(ids == [self.count] for ids in nested))
        globals_ = conversion["bounded_image_conversion"].__globals__
        with patch.dict(globals_, {"scope_native_template_search": lambda *_: nullcontext()}):
            with self.assertRaises(MemoryError):
                bounded(Model(Environment(self.capacity)))

    def test_native_inactive_context_and_explicit_record_subset_survive(self) -> None:
        seen = []
        bounded = conversion["bounded_image_conversion"](lambda model: seen.extend(model.search([("eligible", "=", True)]).ids))
        bounded(Model(Environment(self.capacity), active_test=False))
        self.assertEqual(seen, [row["id"] for row in Model.rows if row["eligible"]])
        seen.clear()
        bounded(Model(Environment(self.capacity), [2, 3, 7], active_test=False))
        self.assertEqual(seen, [2, 3])

    def test_native_failure_restores_local_and_inherited_search_methods(self) -> None:
        class Inherited(Model):
            pass

        def fail(_model: Model) -> None:
            raise RuntimeError("native failure before its first search")

        for owner in (Model, Inherited):
            original = owner.search
            with self.assertRaises(RuntimeError):
                conversion["bounded_image_conversion"](fail)(owner(Environment(self.capacity)))
            self.assertIs(owner.search, original)
            self.assertEqual(owner(Environment(self.capacity)).search([]).ids, [row["id"] for row in Model.rows if row["active"]])
