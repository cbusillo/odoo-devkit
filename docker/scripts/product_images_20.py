from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

IMAGE_BATCH_SIZE = 32


def create_extra_variant_images(templates: Any) -> None:
    from odoo import Command

    identifiers = templates.search([("image_1920", "!=", False)]).ids
    for offset in range(0, len(identifiers), IMAGE_BATCH_SIZE):
        values = []
        origins = []
        for template in templates.browse(identifiers[offset : offset + IMAGE_BATCH_SIZE]):
            template_image = template.image_1920
            first_image = template.product_template_image_ids.sorted("sequence")[:1]
            add_main = template_image.content != first_image.image_1920.content
            main_attributes = templates.env["product.template.attribute.value"]
            for product in template.product_variant_ids:
                variant_image = product.image_variant_1920
                first_variant_image = product.variant_image_ids.sorted("sequence")[:1]
                if add_main and not variant_image and template_image.content == product.image_1920.content:
                    main_attributes |= product.product_template_attribute_value_ids
                if not variant_image or variant_image.content == first_variant_image.image_1920.content:
                    continue
                values.append(
                    {
                        "name": product.display_name,
                        "product_tmpl_id": template.id,
                        "attribute_value_ids": [Command.set(product.product_template_attribute_value_ids.ids)],
                        "image_1920": variant_image,
                        "sequence": first_variant_image.sequence - 1,
                    }
                )
                origins.append(("product.product", product.id, "image_variant_1920", variant_image))
            if add_main:
                values.insert(
                    0,
                    {
                        "name": template.display_name,
                        "product_tmpl_id": template.id,
                        "attribute_value_ids": [Command.set(main_attributes.ids)]
                        if main_attributes and len(template.product_variant_ids) > 1
                        else False,
                        "image_1920": template_image,
                        "sequence": first_image.sequence - 1,
                    },
                )
                origins.insert(0, ("product.template", template.id, "image_1920", template_image))
        expected = [
            (model, identifier, field, hashlib.sha1(image.content).hexdigest()) for model, identifier, field, image in origins
        ]
        created = templates.env["product.image"].with_context(skip_update_main_image=True).create(values)
        created_ids = created.ids
        templates.env.flush_all()
        templates.env.invalidate_all(flush=False)
        copies = []
        for image, (model, identifier, field, checksum) in zip(
            templates.env["product.image"].browse(created_ids), expected, strict=True
        ):
            if hashlib.sha1(image.image_1920.content).hexdigest() != checksum:
                raise RuntimeError("A native extra image differs from its original product image")
            copies.append(
                {"image_id": image.id, "source_model": model, "source_id": identifier, "source_field": field, "sha1": checksum}
            )
        report = os.environ.get("ODOO_MIGRATION_DIAGNOSTIC_REPORT")
        if report and copies:
            with Path(report).with_name("product-image-copies.jsonl").open("a") as stream:
                stream.writelines(json.dumps(copy) + "\n" for copy in copies)
                stream.flush()
                os.fsync(stream.fileno())
        templates.env.invalidate_all()


def install_bounded_image_conversion() -> None:
    from odoo import api
    from odoo.addons.website_sale.models.product_template import ProductTemplate

    if not callable(getattr(ProductTemplate, "_create_extra_variant_images", None)):
        raise RuntimeError("The selected Website Sale has no native extra-image conversion")
    ProductTemplate._create_extra_variant_images = api.model(create_extra_variant_images)
