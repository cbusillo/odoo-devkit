from __future__ import annotations

import copy
import hashlib
import re
import runpy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "docker/scripts"
PAYMENT = runpy.run_path(str(SCRIPTS / "payment_20.py"))


class PaymentAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.filestore = Path(directory.name)
        self.version = "19.0.source"
        self.witness: dict[str, Any] = {
            "source_version": self.version,
            "attachment_defaults": {"public": False, "url": None, "access_token": None},
            "methods": {},
            "providers": {"payment.provider_fixture": {"module": "base.module_fixture", "methods": []}},
        }
        company_id = 31
        self.current: dict[str, Any] = {
            "methods": [],
            "providers": [
                {
                    "id": 41,
                    "company_id": company_id,
                    "module_id": 51,
                    "code": "none",
                    "state": "disabled",
                    "retained_message": "Retain all provider configuration",
                }
            ],
            "method_aliases": [],
            "provider_aliases": [self.alias(81, 41, "provider_fixture", "payment.provider")],
            "attachments": [],
            "attachment_aliases": [],
            "modules": {51: {"xmlid": "base.module_fixture", "state": "uninstalled"}},
            "main_company": company_id,
            "company_ids": [company_id],
            "tokens": False,
            "transactions": False,
            "provider_links": [],
            "countries": {},
            "currencies": {},
            "relation_columns": {
                table: sorted(("payment_method_id", column)) for table, column, _model in PAYMENT["RELATIONS"].values()
            },
        }
        for index, alias in enumerate(("payment.primary_fixture", "payment.brand_fixture"), 1):
            identity = 100 + index
            parent = None if index == 1 else self.current["methods"][0]["id"]
            values = {"name": {"en_US": alias}, "active": False, "primary_payment_method_id": parent}
            self.current["methods"].append({"id": identity, **values})
            self.current["method_aliases"].append(self.alias(200 + index, identity, alias.split(".")[1], "payment.method"))
            self.current["countries"][identity] = ["base.fixture_country"]
            self.current["currencies"][identity] = []
            expected = dict(values, primary_payment_method_id=None if parent is None else "payment.primary_fixture")
            expected.update(supported_country_ids=["base.fixture_country"], supported_currency_ids=[])
            self.witness["methods"][alias] = {"values": expected, "images": {}}
            for offset, field in enumerate(sorted(PAYMENT["IMAGE_FIELDS"]), 1):
                body = (alias + ":" + field + ":qualified immutable representation").encode()
                checksum = hashlib.sha1(body).hexdigest()
                key = checksum[:2] + "/" + checksum
                path = self.filestore / key
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(body)
                self.witness["methods"][alias]["images"][field] = {
                    "sha1": checksum,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                }
                self.current["attachments"].append(
                    {
                        "id": identity * 10 + offset,
                        "res_model": self.current["method_aliases"][-1]["model"],
                        "res_id": identity,
                        "res_field": field,
                        "name": field,
                        "type": "binary",
                        "mimetype": "image/png",
                        "company_id": self.current["main_company"],
                        "checksum": checksum,
                        "file_size": len(body),
                        "store_fname": key,
                        "has_db_datas": False,
                        "public": None,
                        "url": None,
                        "access_token": None,
                    }
                )
            self.current["provider_links"].append((41, identity))
            self.witness["providers"]["payment.provider_fixture"]["methods"].append(alias)
        self.profile = {"payment": self.witness}

    @staticmethod
    def alias(identity: int, record: int, name: str, model: str) -> dict[str, Any]:
        return {"id": identity, "module": "payment", "name": name, "res_id": record, "model": model, "noupdate": True}

    def admit(self) -> Any:
        witness = PAYMENT["source_witness"](self.profile, self.version)
        return PAYMENT["admit"](self.current, witness, self.filestore)

    def collector_cursor(self, *, allow_lock: bool = False) -> Mock:
        current = self.current
        country_id = 61
        country_alias = next(iter(current["countries"].values()))[0]
        rows = {
            "pg_trigger": [],
            "payment_method": [(row,) for row in current["methods"]],
            "payment_provider": [(row,) for row in current["providers"]],
            "ir_attachment": [(row,) for row in current["attachments"]],
            "ir_module_module": [(identifier, row["state"], row["xmlid"]) for identifier, row in current["modules"].items()],
            "information_schema.columns": list(current["relation_columns"].items()),
            "res_company": [(identifier,) for identifier in current["company_ids"]],
            "payment_token": [],
            "payment_transaction": [],
            "payment_method_payment_provider_rel": current["provider_links"],
            "payment_method_res_country_rel": [(row["id"], country_id) for row in current["methods"]],
            "payment_method_res_currency_rel": [],
        }
        cursor = Mock()

        def execute(query: str, parameters: tuple = ()) -> None:
            normalized = " ".join(query.upper().split())
            if allow_lock and normalized.startswith("LOCK TABLE "):
                return
            if not normalized.startswith(("SELECT ", "WITH ")) or re.search(
                r"\b(INSERT|UPDATE|DELETE|MERGE|LOCK|SHARE|CREATE|ALTER|DROP|TRUNCATE)\b", normalized
            ):
                raise RuntimeError("Read-only cursor refuses mutation or locking")
            source = re.search(r"\bFROM\s+([\w.]+)", query, re.IGNORECASE)
            if source is None:
                raise AssertionError("Collector query has no source")
            table = source[1]
            if table != "ir_model_data":
                cursor.fetchall.return_value = rows[table]
            elif "main_company" in query:
                cursor.fetchall.return_value = [(current["main_company"],)]
            elif parameters and parameters[0] == "res.country":
                cursor.fetchall.return_value = [(country_id, country_alias)]
            elif parameters and parameters[0] == "res.currency":
                cursor.fetchall.return_value = []
            elif "ir.attachment" in query:
                cursor.fetchall.return_value = current["attachment_aliases"]
            else:
                cursor.fetchall.return_value = [(row,) for row in current["method_aliases"] + current["provider_aliases"]]

        cursor.execute.side_effect = execute
        return cursor

    def test_observe_collects_the_qualified_snapshot_through_a_select_only_cursor_without_odoo(self) -> None:
        with patch.dict(
            PAYMENT["observe"].__globals__, {"import_module": Mock(side_effect=AssertionError("Collector imports Odoo"))}
        ):
            snapshot = PAYMENT["observe"](self.collector_cursor())
        self.assertEqual(snapshot, self.current)

    def test_capture_locks_before_collecting_the_same_qualified_snapshot(self) -> None:
        cursor = self.collector_cursor(allow_lock=True)
        snapshot = PAYMENT["capture"](cursor)
        statements = [" ".join(call.args[0].upper().split()) for call in cursor.execute.call_args_list]
        self.assertTrue(statements[0].startswith("LOCK TABLE "))
        self.assertTrue(all(statement.startswith(("SELECT ", "WITH ")) for statement in statements[1:]))
        self.assertEqual(snapshot, self.current)

    def add_native_method(self) -> None:
        self.current["methods"].append({"id": 301, "code": "cash_on_delivery", "active": True})
        method = self.alias(401, 301, "payment_method_cash_on_delivery", "payment.method")
        method["module"] = "delivery"
        self.current["method_aliases"].append(method)
        self.current["providers"].append(
            {"id": 302, "code": "custom", "custom_mode": "cash_on_delivery", "state": "enabled", "module_id": 303}
        )
        provider = self.alias(402, 302, "payment_provider_cod", "payment.provider")
        provider["module"] = "delivery"
        self.current["provider_aliases"].append(provider)
        self.current["modules"][303] = {"xmlid": "base.module_delivery", "state": "installed"}
        self.current["provider_links"].append((302, 301))
        self.current["countries"][301] = []
        self.current["currencies"][301] = []
        self.current["attachments"].append({"id": 403, "res_id": 301, "res_field": "image"})

    def test_native_method_and_enabled_provider_are_preserved_outside_stock_retirement(self) -> None:
        self.add_native_method()
        before = copy.deepcopy(self.current)
        plan = self.admit()
        self.assertEqual(plan.preserved_methods, [(301, 302)])
        self.assertNotIn(301, plan.methods)
        self.assertNotIn(403, plan.attachments)
        self.assertNotIn(401, plan.aliases)
        self.assertEqual(self.current, before)

    def test_native_ownership_requires_the_exact_existing_connector_and_exclusive_link(self) -> None:
        self.add_native_method()
        before = copy.deepcopy(self.current)
        for mutate in (
            lambda: self.current["provider_links"].append((41, 301)),
            lambda: self.current["provider_links"].append((302, 101)),
            lambda: self.current["provider_links"].remove((302, 301)),
            lambda: self.current["modules"][303].update(state="uninstalled"),
            lambda: self.current["providers"][-1].update(custom_mode="wire_transfer"),
            lambda: self.current["method_aliases"][-1].update(noupdate=False),
        ):
            self.current = copy.deepcopy(before)
            mutate()
            with self.assertRaises(RuntimeError):
                self.admit()

    def test_native_owner_assignment_waits_for_all_incoming_reference_checks(self) -> None:
        self.add_native_method()
        function = PAYMENT["retire"]
        cursor = Mock()
        cursor.fetchall.return_value = [(301,)]
        checker = Mock(side_effect=RuntimeError("outside reference"))
        with patch.dict(function.__globals__, {"capture": lambda _cursor: self.current, "_delete_exact": Mock()}):
            with self.assertRaises(RuntimeError):
                function(cursor, self.profile, self.version, self.filestore, checker)
            cursor.execute.assert_not_called()
            checker.side_effect = None
            function(cursor, self.profile, self.version, self.filestore, checker)
        self.assertEqual(cursor.execute.call_args.args[1], (302, 301))

    def test_recorded_source_image_absence_refuses_an_added_image_and_an_unrecorded_loss(self) -> None:
        alias = next(iter(self.witness["methods"]))
        attachment = self.current["attachments"][0]
        self.witness["methods"][alias]["images"][attachment["res_field"]] = None
        with self.assertRaisesRegex(RuntimeError, "recorded source absence"):
            self.admit()
        self.current["attachments"].remove(attachment)
        self.admit()
        self.current["attachments"].pop()
        with self.assertRaisesRegex(RuntimeError, "missing stock image"):
            self.admit()

    def test_whole_family_admits_with_semantic_nullable_boolean_and_real_original_file_hashes(self) -> None:
        plan = self.admit()
        self.assertEqual(set(plan.methods), {row["id"] for row in self.current["methods"]})
        self.assertEqual(set(plan.attachments), {row["id"] for row in self.current["attachments"]})
        self.assertEqual(set(plan.aliases), {row["id"] for row in self.current["method_aliases"]})
        self.current["methods"][0]["active"] = None
        self.admit()

    def test_retained_profile_qualification_preserves_existing_components_and_copies_the_witness(self) -> None:
        retained = {"reference": {"database_uuid": "independent", "modules": {"mail": self.version}}, "history": {"kept": True}}
        before = copy.deepcopy(retained)
        original_witness = copy.deepcopy(self.witness)
        qualified = PAYMENT["qualify_payment_source"](retained, self.witness, self.version)
        self.assertEqual({key: value for key, value in qualified.items() if key != "payment"}, before)
        self.assertEqual(qualified["payment"], self.witness)
        qualified["payment"]["methods"].clear()
        qualified["reference"]["modules"].clear()
        self.assertEqual(retained, before)
        self.assertEqual(self.witness, original_witness)
        with self.assertRaises(RuntimeError):
            PAYMENT["qualify_payment_source"](retained, self.witness, self.version + ".wrong")
        self.assertEqual(retained, before)

    def test_material_and_ownership_changes_refuse_with_unchanged_positive_control(self) -> None:
        changes = {
            "translation": lambda current: current["methods"][0]["name"].update(fr_FR="Changed"),
            "missing_boolean": lambda current: current["methods"][0].pop("active"),
            "unknown_field": lambda current: current["methods"][0].update(custom_value="Keep"),
            "outside_parent": lambda current: current["methods"][1].update(primary_payment_method_id=999),
            "outside_brand": lambda current: current["methods"].append(dict(current["methods"][1], id=999)),
            "country": lambda current: current["countries"][current["methods"][0]["id"]].clear(),
            "provider_link": lambda current: current["provider_links"].pop(),
            "relation_payload": lambda current: next(iter(current["relation_columns"].values())).append("custom_payload"),
            "provider_state": lambda current: current["providers"][0].update(state="enabled"),
            "connector": lambda current: current["modules"][51].update(state="installed"),
            "tokens": lambda current: current.update(tokens=True),
            "transactions": lambda current: current.update(transactions=True),
            "extra_company": lambda current: current["company_ids"].append(999),
            "alias_protection": lambda current: current["method_aliases"][0].update(noupdate=False),
            "alias_material": lambda current: current["method_aliases"][0].update(custom_value="Keep this alias metadata"),
            "additional_alias": lambda current: current["method_aliases"].append(dict(current["method_aliases"][0], id=999)),
            "asset_alias": lambda current: current["attachment_aliases"].append((999,)),
            "asset_company": lambda current: current["attachments"][0].update(company_id=None),
            "asset_metadata": lambda current: current["attachments"][0].update(access_token="keep"),
            "asset_unknown": lambda current: current["attachments"][0].update(extra_value="keep"),
            "user_attachment": lambda current: current["attachments"].append(
                dict(current["attachments"][0], id=999, res_field=None)
            ),
            "missing_image": lambda current: current["attachments"].pop(),
        }
        baseline = copy.deepcopy(self.current)
        for subject, change in changes.items():
            with self.subTest(subject=subject):
                self.current = copy.deepcopy(baseline)
                self.admit()
                change(self.current)
                with self.assertRaises(RuntimeError):
                    self.admit()

    def test_new_representation_cannot_replace_the_qualified_original_even_with_consistent_database_checksum(self) -> None:
        self.admit()
        attachment = self.current["attachments"][0]
        body = b"different representation"
        checksum = hashlib.sha1(body).hexdigest()
        key = checksum[:2] + "/" + checksum
        path = self.filestore / key
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(body)
        attachment.update(checksum=checksum, store_fname=key, file_size=len(body))
        with self.assertRaises(RuntimeError):
            self.admit()

    def test_original_corruption_and_symlink_storage_refuse(self) -> None:
        self.admit()
        attachment = self.current["attachments"][0]
        path = self.filestore / attachment["store_fname"]
        body = path.read_bytes()
        path.write_bytes(body + b"changed")
        with self.assertRaises(RuntimeError):
            self.admit()
        path.write_bytes(body)
        self.admit()
        other = self.filestore / "other"
        other.write_bytes(body)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(RuntimeError):
            self.admit()

    def test_missing_wrong_version_or_incomplete_source_witness_refuses(self) -> None:
        self.admit()
        for change in ("missing", "version", "family", "defaults"):
            with self.subTest(change=change):
                profile = copy.deepcopy(self.profile)
                if change == "missing":
                    profile.clear()
                elif change == "version":
                    profile["payment"]["source_version"] += ".different"
                elif change == "family":
                    profile["payment"]["providers"].clear()
                else:
                    profile["payment"].pop("attachment_defaults")
                with self.assertRaises(RuntimeError):
                    PAYMENT["source_witness"](profile, self.version)

    def test_native_stage_fresh_and_completed_paths_do_not_load_a_reference_or_odoo(self) -> None:
        stage = runpy.run_path(str(SCRIPTS / "nativeupgrade/payment/20.0.2.0/pre-global-methods.py"))
        target_version = ".".join(("20", *self.version.split(".")[1:]))
        for version in (None, "", target_version):
            with self.subTest(version=version):
                stage["migrate"](Mock(), version)
        loader = Mock(side_effect=RuntimeError("native import selected"))
        with patch.dict(stage["migrate"].__globals__, {"import_module": loader}), self.assertRaises(RuntimeError):
            stage["migrate"](Mock(), self.version)
        loader.assert_called_once()

    def test_reference_refusal_precedes_every_effect_and_valid_plan_deletes_exact_cohorts(self) -> None:
        self.admit()
        function = PAYMENT["retire"]
        effects = []
        cursor = Mock()
        checker = Mock(side_effect=RuntimeError("outside incoming reference"))
        with patch.dict(
            function.__globals__,
            {
                "capture": lambda _cursor: copy.deepcopy(self.current),
                "_delete_exact": lambda _cursor, table, identifiers: effects.append((table, set(identifiers))),
            },
        ):
            with self.assertRaises(RuntimeError):
                function(cursor, self.profile, self.version, self.filestore, checker)
            self.assertFalse(effects)
            checker.side_effect = None
            plan = function(cursor, self.profile, self.version, self.filestore, checker)
        self.assertEqual(
            dict(effects),
            {
                "payment_method": set(plan.methods),
                "ir_attachment": set(plan.attachments),
                "ir_model_data": set(plan.aliases),
            },
        )


if __name__ == "__main__":
    unittest.main()
