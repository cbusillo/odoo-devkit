from __future__ import annotations

import importlib.machinery
import json
import os
import runpy
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest import TestCase, TestSuite


def select_worker_suite(module: ModuleType) -> None:
    original = module.make_suite

    def make_suite(module_names: Sequence[str], position: str = "at_install") -> TestSuite:
        part = os.environ["ADDON_TEST_PART"]
        if position != "post_install":
            return original(module_names if part == "seed" else [], position)
        directory = Path(os.environ["ADDON_TEST_WORK"])
        scope = directory / "modules.json"
        if part == "seed":
            scope.write_text(json.dumps(list(module_names)))
        suite = original(json.loads(scope.read_text()), position)
        tests = cast(list[TestCase], list(suite))
        ids = [test.id().removeprefix("odoo.addons.") for test in tests]
        groups = json.loads((directory / "groups.json").read_text())
        if Counter(ids) != Counter(name for group in groups for name in group):
            raise ValueError("Native post-install discovery differs from the serial reference")
        selected = set() if part == "seed" else set(groups[int(part)])
        return type(suite)([test for name, test in zip(ids, tests, strict=True) if name in selected])

    module.make_suite = make_suite


class ShardLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module: ModuleType) -> None:
        super().exec_module(module)
        select_worker_suite(module)


class SuiteFinder:
    @staticmethod
    def find_spec(
        fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "odoo.tests.loader":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if not spec or not spec.origin:
            raise ImportError(f"Cannot locate {fullname}")
        spec.loader = ShardLoader(fullname, spec.origin)
        return spec


if __name__ == "__main__":
    sys.meta_path.insert(0, SuiteFinder())
    sys.argv[0] = "/odoo/odoo-bin.source"
    runpy.run_path(sys.argv[0], run_name="__main__")
