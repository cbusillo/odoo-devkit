from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any

IMAGE_BATCH_SIZE = 32


@contextmanager
def scope_native_template_search(owner: Any, selected: list[int]) -> Iterator[None]:
    from odoo.fields import Domain

    original_search = owner.search
    inherited = "search" not in owner.__dict__
    first_search = True

    @wraps(original_search)
    def search(records: Any, domain: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal first_search
        if first_search:
            assert any(term.field_expr == "image_1920" for term in Domain(domain).iter_conditions())
            first_search = False
            domain = Domain(domain) & Domain("id", "in", selected)
        return original_search(records, domain, *args, **kwargs)

    owner.search = search
    try:
        yield
    finally:
        if inherited:
            del owner.search
        else:
            owner.search = original_search


def bounded_image_conversion(native: Callable[[Any], None]) -> Callable[[Any], None]:
    @wraps(native)
    def convert(templates: Any) -> None:
        identifiers = templates.ids or templates.with_context(active_test=False).search([]).ids
        for offset in range(0, len(identifiers), IMAGE_BATCH_SIZE):
            selected = identifiers[offset : offset + IMAGE_BATCH_SIZE]
            with scope_native_template_search(type(templates), selected):
                native(templates.browse(selected))
            templates.env.flush_all()
            templates.env.invalidate_all(flush=False)

    return convert


def install_bounded_image_conversion() -> None:
    from odoo.addons.website_sale.models.product_template import ProductTemplate

    native = getattr(ProductTemplate, "_create_extra_variant_images", None)
    if not callable(native):
        raise RuntimeError("The selected Website Sale has no native extra-image conversion")
    ProductTemplate._create_extra_variant_images = bounded_image_conversion(native)


def native_image_copies(cursor: Any, source_maximum: int) -> set[tuple[str, int]]:
    cursor.execute(
        "SELECT image.id "
        "FROM product_image image "
        "JOIN ir_attachment target ON target.res_model='product.image' AND target.res_id=image.id AND target.res_field='image_1920' "
        "JOIN LATERAL (SELECT original.id FROM ir_attachment original "
        "LEFT JOIN product_product variant ON original.res_model='product.product' AND variant.id=original.res_id "
        "WHERE original.checksum=target.checksum AND original.store_fname=target.store_fname AND ((original.res_model='product.template' "
        "AND original.res_field='image_1920' AND original.res_id=image.product_tmpl_id) OR "
        "(original.res_model='product.product' AND original.res_field='image_variant_1920' "
        "AND variant.product_tmpl_id=image.product_tmpl_id)) ORDER BY original.id LIMIT 1) source ON TRUE "
        "WHERE image.id>%s ORDER BY image.id",
        (source_maximum,),
    )
    rows = cursor.fetchall()
    identifiers = {row[0] for row in rows}
    if len(identifiers) != len(rows):
        raise ValueError("Native image copies have ambiguous main attachments")
    return {("product.image", identifier) for identifier in identifiers}
