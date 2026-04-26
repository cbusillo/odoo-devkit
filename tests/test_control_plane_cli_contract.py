from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from odoo_devkit import local_runtime


class ControlPlaneCliContractTests(unittest.TestCase):
    def test_environment_resolution_uses_launchplane_cli(self) -> None:
        completed_process = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "environment": {
                        "ODOO_MASTER_PASSWORD": "control-plane-master",
                    }
                }
            ),
            stderr="",
        )

        with mock.patch(
            "odoo_devkit.local_runtime.subprocess.run",
            return_value=completed_process,
        ) as run_mock:
            loaded_environment = local_runtime.load_environment_from_control_plane(
                control_plane_root=Path("/opt/launchplane"),
                context_name="cm",
                instance_name="testing",
            )

        command = run_mock.call_args.args[0]
        self.assertEqual(
            command[:5],
            ["uv", "--directory", "/opt/launchplane", "run", "launchplane"],
        )
        self.assertIn("environments", command)
        self.assertEqual(
            loaded_environment.merged_values["ODOO_MASTER_PASSWORD"],
            "control-plane-master",
        )


if __name__ == "__main__":
    unittest.main()
