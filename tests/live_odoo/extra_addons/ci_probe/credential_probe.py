"""Inert integration hook used only inside the internal-network devkit fixture."""

import os

import psycopg2
from odoo import models


def observe_boundary(database: str, hook: str) -> None:
    if not os.environ.get("DEVKIT_BOUNDARY_PROBE"):
        return
    with psycopg2.connect(host="database", user="odoo", dbname=database) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT key,value FROM ir_config_parameter WHERE key IN ('printnode.api_key','web_map.token_map_box')")
        parameters = dict(cursor.fetchall())
        cursor.execute("SELECT count(*) FROM ir_cron WHERE active")
        active_crons = cursor.fetchone()[0]
        cursor.execute("SELECT smtp_host FROM ir_mail_server WHERE active")
        mail_hosts = [row[0] for row in cursor.fetchall()]
        safe = (
            parameters.get("printnode.api_key") == os.environ["DEVKIT_LANE_KEY"]
            and "web_map.token_map_box" not in parameters
            and active_crons == 0
            and mail_hosts == ["invalid"]
        )
        # A stale credential would reach these outbound sinks. They record only the
        # path names and refuse before opening any socket or exposing any key.
        paths = (hook,) if safe else ("blocked_http", "blocked_mail", "blocked_integration", "blocked_cron")
    with psycopg2.connect(host="database", user="odoo", dbname="postgres") as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO devkit_boundary_events (database_name,event) VALUES (%s,%s)", [(database, path) for path in paths]
        )
    if not safe:
        raise RuntimeError("Credential-capable hook reached the blocked outbound sinks before the boundary.")


class CredentialProbe(models.AbstractModel):
    _name = "ci.credential.probe"
    _description = "Isolated committed credential boundary probe"

    def _register_hook(self) -> None:
        super()._register_hook()
        observe_boundary(self.env.cr.dbname, "register_hook")

    def check_update_boundary(self) -> None:
        observe_boundary(self.env.cr.dbname, "update_hook")
