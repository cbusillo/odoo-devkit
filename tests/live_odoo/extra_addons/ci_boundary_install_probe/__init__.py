from typing import Any

from odoo.addons.ci_probe.credential_probe import observe_boundary


def pre_init_hook(env: Any) -> None:
    observe_boundary(env.cr.dbname, "pre_init_hook")


def post_init_hook(env: Any) -> None:
    observe_boundary(env.cr.dbname, "post_init_hook")
