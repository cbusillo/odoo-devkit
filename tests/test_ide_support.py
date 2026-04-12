from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from odoo_devkit import ide_support


class DevkitIdeSupportTests(unittest.TestCase):
    def test_write_pycharm_odoo_conf_maps_project_addons_path_locally(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)

            written_conf = ide_support.write_pycharm_odoo_conf(
                repo_root=repo_root,
                context_name="cm",
                instance_name="local",
                database_name="cm",
                db_host_port=5432,
                state_path=repo_root / ".platform" / "state" / "cm-local",
                addons_paths=(
                    "/odoo/addons",
                    "/odoo/odoo/addons",
                    "/opt/project/addons",
                    "/opt/extra_addons",
                    "/opt/enterprise",
                ),
                source_environment={"ODOO_DB_USER": "odoo", "ODOO_DB_PASSWORD": "pw"},
            )

            self.assertEqual(written_conf, repo_root / ".platform" / "ide" / "cm.local.odoo.conf")
            rendered_conf = written_conf.read_text(encoding="utf-8")
            self.assertIn(
                f"addons_path = /odoo/addons,/odoo/odoo/addons,{repo_root / 'addons'},/opt/extra_addons,/opt/enterprise",
                rendered_conf,
            )
            self.assertNotIn("/.platform/ide/", rendered_conf)
            self.assertIn("db_port = 5432", rendered_conf)

    def test_write_pycharm_odoo_conf_prefers_explicit_host_addons_paths(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)

            written_conf = ide_support.write_pycharm_odoo_conf(
                repo_root=repo_root,
                context_name="opw",
                instance_name="local",
                database_name="opw",
                db_host_port=15432,
                state_path=repo_root / ".platform" / "state" / "opw-local",
                addons_paths=("/opt/project/addons", "/opt/project/addons/shared"),
                source_environment={"ODOO_DB_USER": "odoo", "ODOO_DB_PASSWORD": "pw"},
                host_addons_paths=("/tmp/tenant/addons", "/tmp/shared-addons"),
            )

            rendered_conf = written_conf.read_text(encoding="utf-8")
            self.assertIn("addons_path = /tmp/tenant/addons,/tmp/shared-addons", rendered_conf)
            self.assertNotIn(str(repo_root / "addons"), rendered_conf)


if __name__ == "__main__":
    unittest.main()
