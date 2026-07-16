from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from odoo_devkit import local_runtime
from odoo_devkit.runtime_environment import sanitized_subprocess_environment


class RuntimeEnvironmentPayloadContractTests(unittest.TestCase):
    def test_environment_resolution_accepts_explicit_runtime_payload(self) -> None:
        loaded_environment = local_runtime.load_environment_from_explicit_payload(
            raw_payload=json.dumps(
                {
                    "context": "cm",
                    "instance": "testing",
                    "environment": {
                        "ODOO_MASTER_PASSWORD": "test-master-value",
                    },
                }
            ),
            context_name="cm",
            instance_name="testing",
        )

        self.assertEqual(
            loaded_environment.merged_values["ODOO_MASTER_PASSWORD"],
            "test-master-value",
        )

    def test_environment_resolution_rejects_mismatched_explicit_payload(self) -> None:
        for payload_context, payload_instance in (("opw", "testing"), ("cm", "prod")):
            with self.subTest(context=payload_context, instance=payload_instance):
                with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "does not match the selected runtime"):
                    local_runtime.load_environment_from_explicit_payload(
                        raw_payload=json.dumps(
                            {
                                "context": payload_context,
                                "instance": payload_instance,
                                "environment": {"ODOO_MASTER_PASSWORD": "test-only"},
                            }
                        ),
                        context_name="cm",
                        instance_name="testing",
                    )

    def test_environment_resolution_rejects_non_string_values(self) -> None:
        with self.assertRaisesRegex(
            local_runtime.RuntimeCommandError,
            "environment keys and values must be strings",
        ):
            local_runtime.load_environment_from_explicit_payload(
                raw_payload=json.dumps(
                    {
                        "context": "cm",
                        "instance": "testing",
                        "environment": {"ODOO_VERSION": None},
                    }
                ),
                context_name="cm",
                instance_name="testing",
            )

    def test_environment_resolution_rejects_empty_context_or_instance(self) -> None:
        for context_name, instance_name in (("", "testing"), ("cm", "")):
            with self.subTest(context=context_name, instance=instance_name):
                with self.assertRaisesRegex(
                    local_runtime.RuntimeCommandError,
                    "context and instance must be non-empty strings",
                ):
                    local_runtime.load_environment_from_explicit_payload(
                        raw_payload=json.dumps(
                            {
                                "context": context_name,
                                "instance": instance_name,
                                "environment": {"ODOO_MASTER_PASSWORD": "test-only"},
                            }
                        ),
                        context_name="cm",
                        instance_name="testing",
                    )

    def test_environment_resolution_rejects_empty_environment(self) -> None:
        with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "environment object must not be empty"):
            local_runtime.load_environment_from_explicit_payload(
                raw_payload=json.dumps(
                    {
                        "context": "cm",
                        "instance": "testing",
                        "environment": {},
                    }
                ),
                context_name="cm",
                instance_name="testing",
            )

    def test_environment_resolution_rejects_unsafe_environment_keys(self) -> None:
        for environment_key in ("", "INVALID-NAME", "INJECTED=value", "LINE\nBREAK"):
            with self.subTest(environment_key=environment_key):
                with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "valid environment variable names"):
                    local_runtime.load_environment_from_explicit_payload(
                        raw_payload=json.dumps(
                            {
                                "context": "cm",
                                "instance": "testing",
                                "environment": {environment_key: "test-only"},
                            }
                        ),
                        context_name="cm",
                        instance_name="testing",
                    )

    def test_environment_resolution_rejects_unsafe_environment_values(self) -> None:
        for environment_value in (
            "line-one\nINJECTED=value",
            "line-one\rline-two",
            "line-one\x85INJECTED=value",
            "line-one\u2028INJECTED=value",
            "line-one\u2029INJECTED=value",
            "value\x00suffix",
        ):
            with self.subTest(environment_value=repr(environment_value)):
                with self.assertRaisesRegex(local_runtime.RuntimeCommandError, "must not contain"):
                    local_runtime.load_environment_from_explicit_payload(
                        raw_payload=json.dumps(
                            {
                                "context": "cm",
                                "instance": "testing",
                                "environment": {"ODOO_MASTER_PASSWORD": environment_value},
                            }
                        ),
                        context_name="cm",
                        instance_name="testing",
                    )

    def test_sanitized_subprocess_environment_excludes_runtime_payload(self) -> None:
        with mock.patch.dict(
            os.environ,
            {local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR: '{"environment":{"SECRET":"test-only"}}'},
        ):
            child_environment = sanitized_subprocess_environment()

        self.assertNotIn(local_runtime.RUNTIME_ENVIRONMENT_PAYLOAD_ENV_VAR, child_environment)


if __name__ == "__main__":
    unittest.main()
