from __future__ import annotations

import copy
import importlib
import unittest
from typing import Any

from test_structural_census import inventory, target, view

census = importlib.import_module("odoo_structural_census")


def context_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    selected = target()
    selected["models"]["res.users"]["fields"]["producer_only"] = {}
    selected["models"]["res.partner"]["fields"]["consumer_only"] = {}
    selected["models"]["res.users"]["methods"] = ["open_wizard"]
    selected["xmlids"] = {
        "custom.open": {"model": "ir.actions.act_window", "key": "901"},
        "custom.alias": {"model": "ir.actions.act_window", "key": "901"},
    }
    action = {
        "key": "action:17",
        "xmlids": ["custom.open", "custom.alias"],
        "type": "ir.actions.act_window",
        "model": "res.partner",
        "noupdate": True,
        "search_view_id": 29,
    }
    search = view("view:29", '<search><filter name="named_filter" domain="[]"/></search>', model="res.partner", type="search")
    return inventory(actions=[action], views=[search]), selected


def findings(packet: dict[str, Any], selected: dict[str, Any], architecture: str, **values: Any) -> list[dict[str, str]]:
    packet = copy.deepcopy(packet)
    packet["records"]["views"].append(view("producer", architecture, **values))
    packet["totals"]["views"] += 1
    report = census.collect_static(packet, selected)
    return next(row["findings"] for row in report["components"] if row["key"] == "producer")


class ContextAttributionTests(unittest.TestCase):
    def test_action_buttons_separate_producer_values_and_destination_keys(self) -> None:
        packet, selected = context_fixture()
        packet["records"]["fields"] = [{"key": "removed", "model": "res.users", "name": "removed", "ttype": "char"}]
        packet["totals"]["fields"] = 1
        for tag, name in (("button", "%(custom.open)d"), ("button", "custom.open"), ("button", "17"), ("a", "custom.open")):
            with self.subTest(name=name, tag=tag):
                results = findings(
                    packet,
                    selected,
                    f'<form><{tag} name="{name}" type="action" context="{{\'default_consumer_only\': producer_only, \'default_producer_only\': removed}}"/></form>',
                    action_bindings={"17": ["custom.open", "custom.alias"]},
                )
                self.assertEqual(
                    {item["reference"] for item in results if item["code"] == "missing-field"},
                    {"res.partner.producer_only", "res.users.removed"},
                )
                self.assertNotIn("context-consumer-uncertain", {item["code"] for item in results})
                self.assertIn("data-dependent-expression", {item["code"] for item in results})

    def test_object_button_and_inherited_attribute_keep_unknown_consumer_visible(self) -> None:
        packet, selected = context_fixture()
        for architecture in (
            '<form><button name="open_wizard" type="object" context="{\'default_consumer_only\': producer_only}"/></form>',
            '<kanban><a name="open_wizard" type="object" context="{\'default_consumer_only\': producer_only}"/></kanban>',
            '<kanban><a name="17" type="action" context="{\'default_consumer_only\': producer_only}"/></kanban>',
            '<xpath expr="//button" position="attributes"><attribute name="context">{\'default_consumer_only\': producer_only}</attribute></xpath>',
            '<button name="custom.open" type="action" position="attributes" context="{\'default_consumer_only\': producer_only}"/>',
        ):
            with self.subTest(architecture=architecture):
                results = findings(packet, selected, architecture)
                self.assertIn("context-consumer-uncertain", {item["code"] for item in results})
                self.assertFalse(any(item["code"] == "missing-field" for item in results))
                self.assertIn("data-dependent/unreached", {item["state"] for item in results})

    def test_missing_ambiguous_and_cross_database_bindings_cannot_select_consumer(self) -> None:
        packet, selected = context_fixture()
        for change in (
            "missing-binding",
            "missing-source",
            "ambiguous-source",
            "ambiguous-alias-source",
            "ambiguous-target",
            "missing-target",
            "missing-model",
            "wrong-class",
            "withheld",
            "minimized",
            "replacement",
            "stock-disposition",
        ):
            with self.subTest(change=change):
                source, destination = copy.deepcopy(packet), copy.deepcopy(selected)
                bindings = {"17": ["custom.open", "custom.alias"]}
                action = source["records"]["actions"][0]
                if change == "missing-binding":
                    bindings = {}
                    destination["xmlids"]["custom.open"]["key"] = "17"
                elif change == "missing-source":
                    source["records"]["actions"] = []
                elif change == "ambiguous-source":
                    source["records"]["actions"].append({**action, "key": "action:18", "model": "res.users"})
                elif change == "ambiguous-alias-source":
                    action["xmlids"] = ["custom.open"]
                    source["records"]["actions"].append({**action, "key": "action:18", "xmlids": ["custom.alias"]})
                elif change == "ambiguous-target":
                    destination["xmlids"]["custom.alias"]["key"] = "902"
                elif change == "missing-target":
                    destination["xmlids"].pop("custom.open")
                elif change == "missing-model":
                    action.pop("model")
                elif change == "wrong-class":
                    destination["xmlids"]["custom.alias"]["model"] = "ir.actions.server"
                elif change in {"withheld", "minimized"}:
                    action[change] = ["model"]
                elif change == "stock-disposition":
                    action["noupdate"] = False
                else:
                    action["candidate_coverage"] = [
                        {
                            "candidate": destination["binding"],
                            "record_sha256": census.digest(action),
                            "source_sha256": next(iter(destination["migration_sources"].values())),
                            "guard_receipt": "qualified-stock",
                            "code": "native20-stock-replacement",
                            "reference": "custom.open",
                        }
                    ]
                source["totals"]["actions"] = len(source["records"]["actions"])
                results = findings(
                    source,
                    destination,
                    '<form><button name="17" type="action" context="{\'default_consumer_only\': False}"/></form>',
                    action_bindings=bindings,
                )
                self.assertIn("context-consumer-uncertain", {item["code"] for item in results})
                self.assertFalse(any(item["code"] == "missing-field" for item in results))

    def test_search_filter_names_and_nested_payloads_are_not_producer_fields(self) -> None:
        packet, selected = context_fixture()
        results = findings(
            packet,
            selected,
            "<form><button name=\"custom.open\" type=\"action\" context=\"{'search_default_named_filter': 1, 'search_default_consumer_only': 1, 'group_by': 'consumer_only', 'payload': {'default_unrelated': producer_only}}\"/></form>",
        )
        self.assertFalse(any(item["code"] in {"missing-field", "search-filter-unresolved"} for item in results))
        self.assertIn("context-consumer-uncertain", {item["code"] for item in results})
        for change in ("changed_languages", "withheld", "minimized", "inheritance", "missing", "stock-disposition"):
            with self.subTest(change=change):
                source = copy.deepcopy(packet)
                if change == "missing":
                    source["records"]["views"] = []
                elif change == "inheritance":
                    source["records"]["views"].append(view("extension", "<data/>", parent="view:29"))
                elif change == "stock-disposition":
                    source["records"]["views"][0]["xmlids"] = ["custom.search"]
                    selected["xmlids"]["custom.search"] = {"model": "ir.ui.view", "key": "999"}
                else:
                    source["records"]["views"][0][change] = ["en_US"]
                source["totals"]["views"] = len(source["records"]["views"])
                results = findings(
                    source,
                    selected,
                    '<form><button name="custom.open" type="action" context="{\'search_default_named_filter\': 1}"/></form>',
                )
                self.assertIn("search-filter-unresolved", {item["code"] for item in results})
                self.assertFalse(any(item["code"] == "missing-field" for item in results))

    def test_relational_and_record_contexts_keep_destination_and_producer_roles(self) -> None:
        packet, selected = context_fixture()
        packet["records"]["filters"] = [
            {
                "key": "saved",
                "model": "res.partner",
                "context": "{'default_producer_only': False, 'payload': {'default_unrelated': False}, 'group_by': ['producer_only']}",
            }
        ]
        packet["totals"]["filters"] = 1
        results = findings(
            packet,
            selected,
            "<form><field name=\"partner_id\" context=\"{'default_consumer_only': producer_only, 'default_producer_only': False}\"/></form>",
        )
        self.assertEqual({item["reference"] for item in results if item["code"] == "missing-field"}, {"res.partner.producer_only"})
        report = census.collect_static(packet, selected)
        saved = next(row for row in report["components"] if row["key"] == "saved")
        self.assertIn("res.partner.producer_only", {item["reference"] for item in saved["findings"]})
        self.assertNotIn("res.partner.unrelated", {item["reference"] for item in saved["findings"]})
        self.assertIn("context-consumer-uncertain", {item["code"] for item in saved["findings"]})
        declared = {"model": "res.users", "relation": "res.partner", "context": "{'default_consumer_only': producer_only}"}
        self.assertFalse(
            any(item["code"] == "missing-field" for item in census.reference_findings(declared, selected, field_declaration=True))
        )
        packet["records"]["fields"] = [{"key": "old.global", "model": "res.partner", "name": "active_id"}]
        packet["totals"]["fields"] = 1
        packet["records"]["actions"][0]["context"] = "{'default_consumer_only': active_id}"
        packet["records"]["filters"][0]["context"] = "{'default_consumer_only': active_id}"
        report = census.collect_static(packet, selected)
        for component in report["components"]:
            if component["bucket"] in {"actions", "filters"}:
                self.assertFalse(any(item["code"] == "missing-field" for item in component["findings"]))


if __name__ == "__main__":
    unittest.main()
