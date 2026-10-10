{  # noqa: B018 - Odoo loads a dictionary expression as its manifest.
    "name": "Synthetic credential-capable install hooks",
    "version": "19.0.1.0.0",
    "depends": ["ci_probe"],
    "pre_init_hook": "pre_init_hook",
    "post_init_hook": "post_init_hook",
    "license": "LGPL-3",
}
