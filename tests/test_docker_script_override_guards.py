import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerScriptOverrideGuardTests(unittest.TestCase):
    def test_data_workflow_fails_when_typed_override_payload_has_no_consumer(self) -> None:
        script = (REPO_ROOT / "docker/scripts/run_odoo_data_workflows.py").read_text(encoding="utf-8")

        self.assertIn("typed_override_payload_present", script)
        self.assertIn("payload_has_launchplane_settings", script)
        self.assertIn("require_launchplane_payloads_if_configured", script)
        self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", script)
        self.assertIn("launchplane.settings", script)
        self.assertIn("but launchplane.settings is not installed", script)
        self.assertIn("apply_website_bootstrap", script)
        self.assertIn("from odoo_website_bootstrap import", script)
        self.assertNotIn("environment.overrides", script)
        self.assertNotIn("authentik.sso.config", script)

    def test_startup_fails_when_typed_override_payload_has_no_consumer(self) -> None:
        script = (REPO_ROOT / "docker/scripts/run_odoo_startup.py").read_text(encoding="utf-8")

        self.assertIn("typed_override_payload_present", script)
        self.assertIn("payload_has_launchplane_settings", script)
        self.assertIn("require_launchplane_payloads_if_configured", script)
        self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", script)
        self.assertIn("launchplane.settings", script)
        self.assertIn("but launchplane.settings is not installed", script)
        self.assertIn("apply_website_bootstrap", script)
        self.assertIn("from odoo_website_bootstrap import", script)
        self.assertNotIn("environment.overrides", script)
        self.assertNotIn("authentik.sso.config", script)

    def test_website_bootstrap_helper_is_part_of_docker_payload(self) -> None:
        dockerfile = (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
        helper = (REPO_ROOT / "docker/scripts/odoo_website_bootstrap.py").read_text(encoding="utf-8")

        self.assertIn("COPY /docker/scripts /payload/volumes/scripts", dockerfile)
        self.assertIn("def apply_website_bootstrap", helper)
        self.assertIn("LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED", helper)
        self.assertIn("LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED", helper)
        self.assertIn("def require_launchplane_payloads_if_configured", helper)
        self.assertIn("website_bootstrap_applied=true", helper)


if __name__ == "__main__":
    unittest.main()
