from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "docker/scripts/product_images_20.py"
conversion = runpy.run_path(str(SCRIPT))


class Image:
    def __init__(self, content: bytes, environment: Environment) -> None:
        self.value = content
        self.environment = environment

    def __bool__(self) -> bool:
        return bool(self.value)

    @property
    def content(self) -> bytes:
        if self.value:
            self.environment.loaded.add(id(self))
            if len(self.environment.loaded) > self.environment.capacity:
                raise MemoryError("image content accumulated beyond the batch")
        return self.value


class Records:
    def __init__(self, records: list[Any], empty_image: Image) -> None:
        self.records = records
        self.empty_image = empty_image

    def __iter__(self) -> Any:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, selection: slice) -> Records:
        return Records(self.records[selection], self.empty_image)

    def __or__(self, other: Records) -> Records:
        combined = {record.id: record for record in self.records + other.records}
        return Records(list(combined.values()), self.empty_image)

    @property
    def ids(self) -> list[int]:
        return [record.id for record in self.records]

    @property
    def image_1920(self) -> Image:
        return self.records[0].image_1920 if self.records else self.empty_image

    @property
    def sequence(self) -> int:
        return self.records[0].sequence if self.records else 0

    def sorted(self, field: str) -> Records:
        return Records(sorted(self.records, key=lambda record: getattr(record, field)), self.empty_image)


class Environment:
    def __init__(self, count: int, capacity: int) -> None:
        self.capacity = capacity
        self.loaded: set[int] = set()
        self.empty = Image(b"", self)
        self.templates = {}
        self.images = {}
        self.created_values = []
        self.invalidations = 0
        self.flushes = 0
        self.model = Mock(env=self)
        self.model.search.side_effect = lambda _domain: Records(list(self.templates.values()), self.empty)
        self.model.browse.side_effect = lambda ids: Records([self.templates[identifier] for identifier in ids], self.empty)
        self.image_model = Mock()
        self.image_model.with_context.return_value = self.image_model
        self.image_model.create.side_effect = self.create
        self.image_model.browse.side_effect = lambda ids: Records([self.images[identifier] for identifier in ids], self.empty)
        for identifier in range(1, count + 1):
            image = Image(f"original image {identifier}".encode(), self)
            product = SimpleNamespace(
                id=identifier,
                display_name=f"Variant {identifier}",
                image_1920=image,
                image_variant_1920=self.empty,
                variant_image_ids=Records([], self.empty),
                product_template_attribute_value_ids=Records([], self.empty),
            )
            self.templates[identifier] = SimpleNamespace(
                id=identifier,
                display_name=f"Template {identifier}",
                image_1920=image,
                product_template_image_ids=Records([], self.empty),
                product_variant_ids=Records([product], self.empty),
            )

    def __getitem__(self, model: str) -> Any:
        return self.image_model if model == "product.image" else Records([], self.empty)

    def create(self, values: list[dict[str, Any]]) -> Records:
        records = []
        for value in values:
            stored = {**value, "image_1920": value["image_1920"].content}
            self.created_values.append(stored)
            identifier = len(self.images) + 1
            image = SimpleNamespace(id=identifier, image_1920=Image(stored["image_1920"], self))
            self.images[identifier] = image
            records.append(image)
        return Records(records, self.empty)

    def flush_all(self) -> None:
        self.flushes += 1

    def invalidate_all(self, *, flush: bool = True) -> None:
        if flush:
            self.flush_all()
        self.loaded.clear()
        self.invalidations += 1


class ProductImageConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        module = ModuleType("odoo")
        module.Command = SimpleNamespace(set=lambda ids: (6, 0, ids))
        self.modules = patch.dict(sys.modules, {"odoo": module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_large_catalog_keeps_original_image_bytes_with_bounded_cache(self) -> None:
        batch_size = conversion["IMAGE_BATCH_SIZE"]
        count = batch_size * 2 + 1
        bounded = Environment(count, batch_size)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "diagnostic.jsonl"
            with patch.dict(os.environ, {"ODOO_MIGRATION_DIAGNOSTIC_REPORT": str(report)}):
                conversion["create_extra_variant_images"](bounded.model)
            copies = [json.loads(line) for line in report.with_name("product-image-copies.jsonl").read_text().splitlines()]
        self.assertEqual(len(copies), count)
        self.assertEqual({row["source_id"] for row in copies}, set(bounded.templates))
        self.assertEqual(len(bounded.created_values), count)
        for row in bounded.created_values:
            identifier = row["product_tmpl_id"]
            self.assertEqual(row["image_1920"], f"original image {identifier}".encode())
            self.assertEqual(row["attribute_value_ids"], False)
            self.assertEqual(row["sequence"], -1)
        self.assertGreater(bounded.invalidations, 1)
        self.assertGreater(bounded.flushes, 1)
        globals_ = conversion["create_extra_variant_images"].__globals__
        with patch.dict(globals_, {"IMAGE_BATCH_SIZE": count}), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MemoryError):
                conversion["create_extra_variant_images"](Environment(count, batch_size).model)

    def test_existing_extra_images_are_not_duplicated_and_variant_attributes_survive(self) -> None:
        environment = Environment(2, 20)
        first = environment.templates[1]
        first.product_template_image_ids = Records([SimpleNamespace(image_1920=first.image_1920, sequence=7)], environment.empty)
        second = environment.templates[2]
        default_variant = second.product_variant_ids.records[0]
        default_variant.product_template_attribute_value_ids = Records([SimpleNamespace(id=11)], environment.empty)
        variant_image = Image(b"variant-specific original", environment)
        variant = SimpleNamespace(
            id=8,
            display_name="Blue variant",
            image_1920=variant_image,
            image_variant_1920=variant_image,
            variant_image_ids=Records([], environment.empty),
            product_template_attribute_value_ids=Records([SimpleNamespace(id=12)], environment.empty),
        )
        second.product_variant_ids = Records([default_variant, variant], environment.empty)
        with patch.dict(os.environ, {}, clear=True):
            conversion["create_extra_variant_images"](environment.model)
        self.assertEqual(len(environment.created_values), 2)
        main, extra = environment.created_values
        self.assertEqual(main["product_tmpl_id"], second.id)
        self.assertEqual(main["attribute_value_ids"], [(6, 0, [11])])
        self.assertEqual(extra["attribute_value_ids"], [(6, 0, [12])])
        self.assertEqual(extra["image_1920"], b"variant-specific original")
        environment.image_model.with_context.assert_called_with(skip_update_main_image=True)

    def test_changed_persisted_copy_fails_without_publishing_preservation_evidence(self) -> None:
        environment = Environment(1, 20)
        create = environment.create

        def corrupt(values: list[dict[str, Any]]) -> Records:
            records = create(values)
            records.records[0].image_1920 = Image(b"changed image", environment)
            return records

        environment.image_model.create.side_effect = corrupt
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "diagnostic.jsonl"
            with patch.dict(os.environ, {"ODOO_MIGRATION_DIAGNOSTIC_REPORT": str(report)}):
                with self.assertRaises(RuntimeError):
                    conversion["create_extra_variant_images"](environment.model)
            self.assertFalse(report.with_name("product-image-copies.jsonl").exists())
