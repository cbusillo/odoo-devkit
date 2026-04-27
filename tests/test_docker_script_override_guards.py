import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerScriptOverrideGuardTests(unittest.TestCase):
    def test_data_workflow_fails_when_typed_override_payload_has_no_consumer(self) -> None:
        script = (REPO_ROOT / "docker/scripts/run_odoo_data_workflows.py").read_text(encoding="utf-8")

        self.assertIn("typed_override_payload_present", script)
        self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", script)
        self.assertIn("environment.overrides addon is not installed", script)

    def test_startup_fails_when_typed_override_payload_has_no_consumer(self) -> None:
        script = (REPO_ROOT / "docker/scripts/run_odoo_startup.py").read_text(encoding="utf-8")

        self.assertIn("typed_override_payload_present", script)
        self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", script)
        self.assertIn("environment.overrides addon is not installed", script)


if __name__ == "__main__":
    unittest.main()
