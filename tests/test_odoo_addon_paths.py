import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


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
                self.assertIn(shared, expand([project]))
                self.assertEqual(expand([external]), [external])
                self.assertEqual(expand([project, shared, shared]), [project, shared])
                for paths in ([project, external], [external, project]):
                    with self.subTest(paths=paths):
                        with self.assertRaisesRegex(ValueError, "disable_odoo_online") as failure:
                            expand(paths)
                        self.assertIn(str(shared), str(failure.exception))
                        self.assertIn(str(external), str(failure.exception))
