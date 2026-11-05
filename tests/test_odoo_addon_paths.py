import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_odoo_data_workflows
from test_release_maintenance import inventory


class PublisherWarrantySelectionTests(unittest.TestCase):
    def test_shared_and_external_copies_cannot_silently_shadow_each_other(self) -> None:
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/odoo_addon_paths.py"))
        expand = namespace["expand_addon_paths"]
        with TemporaryDirectory() as directory:
            project = Path(directory) / "addons"
            shared = project / "shared"
            external = Path(directory) / "external"
            for root in (shared, external):
                addon = root / "disable_odoo_online"
                addon.mkdir(parents=True)
                (addon / "__manifest__.py").write_text("{'depends': ['mail']}")
                (addon / "__init__.py").touch()
            with patch.dict(expand.__globals__, PROJECT_ADDONS_ROOT=project):
                settings = test_odoo_data_workflows.OdooDataWorkflowShellEnvironmentTests._local_settings()
                settings.addons_path = str(shared)
                settings.local_addons_dirs = str(external)
                runner = test_odoo_data_workflows.odoo_data_workflows.OdooDataWorkflowRunner(settings, upstream=None, env_file=None)
                for operation in (runner.run_restore, lambda: runner.run_bootstrap(do_sanitize=True)):
                    with self.subTest(operation=operation):
                        with patch.object(runner, "drop_database") as drop_database:
                            with self.assertRaises(ValueError):
                                operation()
                            drop_database.assert_not_called()
                with self.assertRaises(ValueError):
                    inventory.module_paths([shared, external])
                self.assertEqual(expand([shared]), [shared])
                self.assertEqual(expand([external]), [external])
                for paths in ([project, external], [external, project]):
                    with self.subTest(paths=paths):
                        with self.assertRaises(ValueError) as failure:
                            expand(paths)
                        self.assertIn(str(shared), str(failure.exception))
                        self.assertIn(str(external), str(failure.exception))

    def test_source_root_checks_host_copies_for_container_paths(self) -> None:
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/scripts/odoo_addon_paths.py"))
        expand = namespace["expand_addon_paths"]
        with TemporaryDirectory() as directory:
            source = Path(directory)
            for group in ("shared", "other"):
                addon = source / group / "disable_odoo_online"
                addon.mkdir(parents=True)
                (addon / "__manifest__.py").write_text("{'depends': ['mail']}")
                (addon / "__init__.py").touch()
            with self.assertRaises(ValueError):
                expand([namespace["PROJECT_ADDONS_ROOT"]], source_root=source)
