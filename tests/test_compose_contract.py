from __future__ import annotations

import unittest
from pathlib import Path


class ComposeContractTests(unittest.TestCase):
    def test_base_compose_file_is_pull_only_for_web(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        base_compose_text = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertNotIn("x-odoo-build", base_compose_text)
        self.assertNotIn("<<: [*odoo-base, *odoo-build]", base_compose_text)
        self.assertIn("  web:\n    <<: *odoo-base\n", base_compose_text)

    def test_override_compose_file_owns_local_web_build(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        override_compose_path = repo_root / "docker-compose.override.yml"
        if not override_compose_path.exists():
            self.skipTest("Local docker-compose.override.yml is optional and not tracked in clean repo checkouts")
        override_compose_text = override_compose_path.read_text(encoding="utf-8")

        self.assertIn("  web:\n    <<: *common\n    build:\n", override_compose_text)
        self.assertIn("      dockerfile: docker/Dockerfile\n", override_compose_text)
        self.assertIn("      target: ${COMPOSE_BUILD_TARGET:-development}\n", override_compose_text)


if __name__ == "__main__":
    unittest.main()
