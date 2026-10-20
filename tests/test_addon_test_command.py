import unittest

from odoo_devkit.addon_test_command import check_command


class AddonTestCommandTests(unittest.TestCase):
    def test_accepts_tenant_and_shared_entrypoints_with_quoted_paths(self) -> None:
        prefix = 'uv --directory "${ODOO_DEVKIT_REPO:-../odoo-devkit}" run --locked python -m odoo_devkit.addon_ci'
        for source in ('--tenant "$PWD" --support-root "/path with spaces/shared"', "--image example/devtools:test"):
            with self.subTest(source=source):
                check_command(f'{prefix} {source} --addons-root "$PWD/addons" --output "${{TEST_OUTPUT:?Set output}}"')
        check_command(f"{prefix} --tenant=. --addons-root=addons --output=out")

    def test_rejects_commands_that_do_not_reach_the_test_runner(self) -> None:
        for command in (
            "uv --directory ../odoo-devkit run platform runtime workflow --workflow update",
            "odoo-bin --test-enable --stop-after-init -i example",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --help",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --tenant . --addons-root addons",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --addons-root addons --output out",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --tenant . --addons-root addons --output out || true",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --tenant . --addons-root addons --output out||true",
            "uv --directory ../odoo-devkit run python -m odoo_devkit.addon_ci --tenant . --addons-root addons --output out;true",
            "uv --directory $(choose-devkit) run python -m odoo_devkit.addon_ci --tenant . --addons-root addons --output out",
            "uv --directory `choose-devkit` run python -m odoo_devkit.addon_ci --tenant . --addons-root addons --output out",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                check_command(command)
