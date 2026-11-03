# Product-form view source records

These selected public Odoo records are the canonical source architectures for
the guarded Stock identity preparation. Runtime admission and fixture setup
read these same files. XML comments, attributes and nonblank text are retained;
the existing shared XML normalizer handles formatting alone.

| File relative to `legacy/` | Public source |
| --- | --- |
| `stock/product_views_18.xml` | [Stock18](https://github.com/odoo/odoo/blob/68d580d3d4703e7190055f7040682942d5f1a24f/addons/stock/views/product_views.xml) |
| `stock/product_views_19_early.xml` | [Earlier Stock19](https://github.com/odoo/odoo/blob/66d2fa625c093fe985ceb6474ba4ec0b841681af/addons/stock/views/product_views.xml) |
| `stock/product_views_19.xml` | [Stock19](https://github.com/odoo/odoo/blob/a789a218fff7aeb5ce97be2b170333a026804f63/addons/stock/views/product_views.xml) |
| `stock_account/product_views_18.xml` | [Stock Account18](https://github.com/odoo/odoo/blob/68d580d3d4703e7190055f7040682942d5f1a24f/addons/stock_account/views/product_views.xml) |
| `stock_account/product_views_19.xml` | [Stock Account19](https://github.com/odoo/odoo/blob/a789a218fff7aeb5ce97be2b170333a026804f63/addons/stock_account/views/product_views.xml) |
| `stock_delivery/product_views_19.xml` | [Stock Delivery19](https://github.com/odoo/odoo/blob/a789a218fff7aeb5ce97be2b170333a026804f63/addons/stock_delivery/views/product_template_view.xml) |

Stock current architecture admits the two retained19 variants. Previous
architecture is qualified independently against either19 variant or the
selected18 record. Stock Account likewise admits its19 current and independent
18 history. The selected Stock Delivery19 architecture also matches its
public18 history; it keeps its existing XMLID and receives no preparation
write. Only the typed, existing `stock.action_open_routes` reference is
interpolated into the Stock architecture.

The native20 fixture in `tests/fixtures/stock_product_views_20.xml` contains
the corresponding three selected target declarations from the individually
qualified OPW runtime source recorded on devkit380. Production preparation
reads the running target's actual XML files, not this fixture. Source fixture
tests do not establish native loader, cleanup or copied-data results.
