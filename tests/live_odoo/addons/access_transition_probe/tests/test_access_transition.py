from __future__ import annotations

from typing import Any

import prepare_core_references_20 as bridge
from odoo import SUPERUSER_ID, Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

MODEL = "access.transition.probe"
MODES = {"c": "create", "r": "read", "u": "write", "d": "unlink"}


class RevertOperation(Exception):
    pass


@tagged("post_install", "-at_install")
class TestAccessTransition(TransactionCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.env.registry.ready)
        internal = self.env.ref("base.group_user")
        self.groups = {
            name: self.env["res.groups"].create({"name": name, "implied_ids": [Command.link(internal.id)]})
            for name in ("reader", "writer")
        }
        self.groups.update(portal=self.env.ref("base.group_portal"), public=self.env.ref("base.group_public"))
        self.companies = self.env.company | self.env["res.company"].create({"name": "Access second company"})
        group_field = "group_ids" if "group_ids" in self.env["res.users"]._fields else "groups_id"
        memberships = self.groups | {"combined": self.groups["reader"] | self.groups["writer"], "ungranted": internal}
        self.users = {
            role: self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": role,
                    "login": "access-transition-" + role,
                    group_field: [Command.set(groups.ids)],
                    "company_id": self.companies[0].id,
                    "company_ids": [Command.set(self.companies.ids)],
                }
            )
            for role, groups in memberships.items()
        }
        self.source = tuple(
            bridge.AccessPolicy("ir.model.access", index, role, MODEL, (self.groups[role].id,), operations, "", True, (), (), False)
            for index, (role, operations) in enumerate((("reader", "r"), ("writer", "crud"), ("portal", "r"), ("public", "r")), 1)
        ) + (
            bridge.AccessPolicy(
                "ir.rule", 1, "Company limit", MODEL, (), "crud", "[('company_id','in',company_ids)]", True, (), (), False
            ),
            bridge.AccessPolicy(
                "ir.rule", 2, "Blocked marker", MODEL, (), "crud", "[('marker','!=','blocked')]", True, (), (), False
            ),
        )
        self.records = {
            "allowed": self.env[MODEL].create(
                {"name": "Allowed", "user_id": self.users["writer"].id, "company_id": self.companies[0].id}
            ),
            "other_company": self.env[MODEL].create(
                {"name": "Second", "user_id": self.users["writer"].id, "company_id": self.companies[1].id}
            ),
            "blocked": self.env[MODEL].create(
                {"name": "Blocked", "user_id": self.users["writer"].id, "company_id": self.companies[0].id, "marker": "blocked"}
            ),
        }

    def install_policies(self, policies: tuple[bridge.AccessPolicy, ...]) -> dict[tuple[str, int], Any]:
        model_id = self.env["ir.model"]._get(MODEL).id
        records = {}
        if "ir.access" in self.env.registry:
            plan = bridge.plan_access_transition(policies)
            self.assertFalse(plan.source_owned)
            for target in plan.converted:
                records[(target.source.source_model, target.source.identifier)] = self.env["ir.access"].create(
                    {
                        "name": target.source.name,
                        "model_id": model_id,
                        "active": target.source.active,
                        "operation": target.operation,
                        "group_id": target.group_id or False,
                        "domain": target.domain,
                    }
                )
            return records
        for policy in policies:
            values = {"name": policy.name, "model_id": model_id, "active": policy.active}
            values.update({"perm_" + mode: operation in policy.operation for operation, mode in MODES.items()})
            if policy.source_model == "ir.model.access":
                values["group_id"] = policy.groups[0] if policy.groups else False
            else:
                values.update(groups=[Command.set(policy.groups)], domain_force=policy.domain)
            records[(policy.source_model, policy.identifier)] = self.env[policy.source_model].create(values)
        return records

    def decide(self, role: str, record_name: str, operation: str, *, both_companies: bool = False) -> bool:
        user = self.users[role]
        model = (
            self.env[MODEL]
            .with_user(user)
            .with_context(allowed_company_ids=self.companies.ids if both_companies else self.companies[0].ids)
        )
        self.assertFalse(model.env.su)
        self.assertNotEqual(model.env.uid, SUPERUSER_ID)
        target = model.browse(self.records[record_name].id)
        try:
            with self.env.cr.savepoint():
                if operation == "read":
                    allowed = bool(target.read(["name"]))
                elif operation == "search":
                    allowed = target.id in model.search([("id", "=", target.id)]).ids
                elif operation == "write":
                    allowed = target.write({"name": "Changed"})
                elif operation == "unlink":
                    allowed = target.unlink()
                else:
                    allowed = bool(
                        model.create(
                            {
                                "name": "Created",
                                "user_id": user.id,
                                "company_id": self.records[record_name].company_id.id,
                                "marker": self.records[record_name].marker,
                            }
                        )
                    )
                raise RevertOperation()
        except RevertOperation:
            return bool(allowed)
        except AccessError:
            return False

    def test_native_plain_permissions_and_global_restrictions(self) -> None:
        self.install_policies(self.source)
        for role in self.users:
            for operation in ("read", "search", "write", "unlink", "create"):
                permitted = role in {"writer", "combined"} or (role != "ungranted" and operation in {"read", "search"})
                with self.subTest(role=role, operation=operation):
                    self.assertEqual(self.decide(role, "allowed", operation), permitted)
                    self.assertFalse(self.decide(role, "blocked", operation))
                    self.assertFalse(self.decide(role, "other_company", operation))
                    self.assertEqual(self.decide(role, "other_company", operation, both_companies=True), permitted)

    def test_inactive_acl_activation_and_group_membership_invalidate_cached_decisions(self) -> None:
        inactive = bridge.AccessPolicy(
            "ir.model.access", 17, "Inactive write", MODEL, (self.groups["reader"].id,), "u", "", False, (), (), False
        )
        records = self.install_policies(self.source + (inactive,))
        self.assertFalse(self.decide("reader", "allowed", "write"))
        records[(inactive.source_model, inactive.identifier)].write({"active": True})
        self.assertTrue(self.decide("reader", "allowed", "write"))
        user = self.users["ungranted"]
        group_field = "group_ids" if "group_ids" in user._fields else "groups_id"
        self.assertFalse(self.decide("ungranted", "allowed", "read"))
        user.write({group_field: [Command.link(self.groups["reader"].id)]})
        self.assertTrue(self.decide("ungranted", "allowed", "read"))
        user.write({group_field: [Command.unlink(self.groups["reader"].id)]})
        self.assertFalse(self.decide("ungranted", "allowed", "read"))

    def test_data_only_zero_acls_leave_native_record_and_model_decisions_unchanged(self) -> None:
        self.install_policies(self.source)

        def observations() -> dict[tuple, bool]:
            result = {
                (role, record, operation, both): self.decide(role, record, operation, both_companies=both)
                for role in self.users
                for record in self.records
                for operation in (*MODES.values(), "search")
                for both in (False, True)
            }
            for role, user in self.users.items():
                model = self.env[MODEL].with_user(user)
                self.assertFalse(model.env.su)
                self.assertNotEqual(model.env.uid, SUPERUSER_ID)
                for operation in MODES.values():
                    result[role, "model", operation] = model.has_access(operation)
            return result

        baseline = observations()
        start = max(policy.identifier for policy in self.source) + 1
        zeros = tuple(
            bridge.AccessPolicy("ir.model.access", index, "Retained zero permission", MODEL, groups, "", "", True, (), (), False)
            for index, groups in enumerate(((), *((group.id,) for group in self.groups.values())), start)
        )
        records = self.install_policies(zeros)
        self.assertEqual(observations(), baseline)
        for record in records.values():
            record.active = False
        self.assertEqual(observations(), baseline)
        for record in records.values():
            record.active = True
        self.assertEqual(observations(), baseline)

    def test_inactive_global_rule_activation(self) -> None:
        inactive = self.source[-1]._replace(active=False)
        records = self.install_policies(self.source[:-1] + (inactive,))
        self.assertTrue(self.decide("writer", "blocked", "read"))
        records[(inactive.source_model, inactive.identifier)].write({"active": True})
        self.assertFalse(self.decide("writer", "blocked", "read"))
        self.assertTrue(self.decide("writer", "allowed", "read"))

    def test_compiled_grouped_union_preserves_ordinary_rights_and_cached_membership(self) -> None:
        reader = bridge.AccessPolicy(
            "ir.rule",
            31,
            "Reader own records",
            MODEL,
            (self.groups["reader"].id,),
            "crud",
            "[('user_id','=',user.id)]",
            True,
            (),
            (),
            False,
        )
        writer = reader._replace(
            identifier=32,
            name="Writer open records",
            groups=(self.groups["writer"].id,),
            domain="\n  [('marker','!=','reader_only'), ('marker','!=','blocked')]\n",
        )
        for name, role in (("reader_own", "reader"), ("combined_own", "combined")):
            self.records[name] = self.env[MODEL].create(
                {"name": name, "user_id": self.users[role].id, "company_id": self.companies[0].id, "marker": "reader_only"}
            )
        for unrestricted in (False, True):
            with self.env.cr.savepoint() as scenario:
                sibling = writer._replace(domain="[]") if unrestricted else writer
                self.install_policies(
                    (self.source[0]._replace(operation="crud"), self.source[1], *self.source[-2:], reader, sibling)
                )
                visible = {"reader": {"reader_own"}, "writer": {"allowed"}, "combined": {"allowed", "combined_own"}}
                if unrestricted:
                    for role in ("writer", "combined"):
                        visible[role] = {"allowed", "reader_own", "combined_own"}
                for role in self.users:
                    for name in self.records:
                        for operation in ("read", "search", "write", "unlink", "create"):
                            expected = name in visible.get(role, set())
                            if operation == "create":
                                expected = role in {"reader", "writer", "combined"} and name not in {"blocked", "other_company"}
                                expected = expected and (role != "writer" or unrestricted or name == "allowed")
                            with self.subTest(unrestricted=unrestricted, role=role, record=name, operation=operation):
                                self.assertEqual(self.decide(role, name, operation), expected)
                group_field = "group_ids" if "group_ids" in self.users["combined"]._fields else "groups_id"
                self.users["combined"].write({group_field: [Command.unlink(self.groups["reader"].id)]})
                self.assertTrue(self.decide("combined", "allowed", "read"))
                self.assertEqual(self.decide("combined", "combined_own", "read"), unrestricted)
                self.users["combined"].write({group_field: [Command.link(self.groups["reader"].id)]})
                self.assertTrue(self.decide("combined", "combined_own", "read"))
                scenario.rollback()

    def test_read_grouped_union_does_not_restrict_reactivated_write_acl(self) -> None:
        reader = bridge.AccessPolicy(
            "ir.rule",
            31,
            "Reader own records",
            MODEL,
            (self.groups["reader"].id,),
            "r",
            "[('user_id','=',user.id)]",
            True,
            (),
            (),
            False,
        )
        writer = reader._replace(identifier=32, groups=(self.groups["writer"].id,), domain="[('marker','!=','blocked')]")
        inactive = self.source[0]._replace(identifier=33, name="Retained inactive update", operation="u", active=False)
        records = self.install_policies(
            (self.source[0], self.source[1]._replace(operation="r"), *self.source[-2:], reader, writer, inactive)
        )
        self.assertFalse(self.decide("reader", "allowed", "read"))
        self.assertFalse(self.decide("reader", "allowed", "write"))
        records[(inactive.source_model, inactive.identifier)].write({"active": True})
        self.assertTrue(self.decide("reader", "allowed", "write"))
        self.assertFalse(self.decide("reader", "allowed", "read"))
        self.assertFalse(self.decide("reader", "blocked", "write"))
        self.assertFalse(self.decide("reader", "other_company", "write"))
        self.assertFalse(self.decide("writer", "allowed", "write"))

    def test_legacy_hook_refuses_grouped_policy_before_bridge_writes(self) -> None:
        if "ir.access" in self.env.registry:
            self.skipTest("Legacy SQL refusal is exercised by native 19")
        grouped = self.source[-1]._replace(groups=(self.groups["reader"].id,))
        sibling = grouped._replace(identifier=grouped.identifier + 1, groups=(self.groups["writer"].id,))
        empty = self.source[0]._replace(operation="")
        records = self.install_policies((grouped, sibling, empty))
        for name, policy, module in (
            ("retained_rule", grouped, "access_transition_fixture"),
            ("source_sibling", sibling, "access_transition_probe"),
            ("source_no_operations", empty, "access_transition_probe"),
        ):
            self.env["ir.model.data"].create(
                {
                    "module": module,
                    "name": name,
                    "model": policy.source_model,
                    "res_id": records[(policy.source_model, policy.identifier)].id,
                    "noupdate": True,
                }
            )
        self.env.flush_all()
        self.env.cr.execute("SELECT id,module,name,model,res_id,noupdate FROM ir_model_data ORDER BY id")
        aliases = self.env.cr.fetchall()
        self.env.cr.execute("SELECT id,arch_db,arch_prev,arch_updated FROM ir_ui_view ORDER BY id")
        views = self.env.cr.fetchall()
        self.env.cr.execute("SELECT * FROM ir_model_access ORDER BY id")
        access = self.env.cr.fetchall()
        with self.assertRaises(RuntimeError) as refusal:
            bridge.migrate(self.env.cr, "19.0.1.0")
        self.assertIn("access_transition_fixture.retained_rule", str(refusal.exception))
        self.assertIn("access_transition_probe.source_no_operations", str(refusal.exception))
        self.env.cr.execute("SELECT id,module,name,model,res_id,noupdate FROM ir_model_data ORDER BY id")
        self.assertEqual(self.env.cr.fetchall(), aliases)
        self.env.cr.execute("SELECT id,arch_db,arch_prev,arch_updated FROM ir_ui_view ORDER BY id")
        self.assertEqual(self.env.cr.fetchall(), views)
        self.env.cr.execute("SELECT * FROM ir_model_access ORDER BY id")
        self.assertEqual(self.env.cr.fetchall(), access)

    def test_custom_acl_cannot_bypass_module_grouped_rule(self) -> None:
        reader_acl = self.source[0]._replace(source_owned=True)
        grouped = bridge.AccessPolicy(
            "ir.rule",
            5,
            "Source-owned own records",
            MODEL,
            (self.groups["reader"].id,),
            "r",
            "[('user_id','=',user.id)]",
            True,
            (),
            (),
            True,
        )
        custom_acl = self.source[1]._replace(operation="r")
        if "ir.access" in self.env.registry:
            self.env["ir.access"].create(
                {
                    "name": grouped.name,
                    "model_id": self.env["ir.model"]._get(MODEL).id,
                    "group_id": self.groups["reader"].id,
                    "operation": "r",
                    "domain": grouped.domain,
                }
            )
        else:
            self.install_policies((reader_acl, grouped, custom_acl))
        self.records["own"] = self.env[MODEL].create(
            {"name": "Combined own", "user_id": self.users["combined"].id, "company_id": self.companies[0].id}
        )
        self.assertTrue(self.decide("combined", "own", "read"))
        self.assertFalse(self.decide("combined", "allowed", "read"))
        if "ir.access" in self.env.registry:
            try:
                with self.env.cr.savepoint():
                    self.env["ir.access"].create(
                        {
                            "name": "Unsafe plain custom ACL",
                            "model_id": self.env["ir.model"]._get(MODEL).id,
                            "group_id": self.groups["writer"].id,
                            "operation": "r",
                        }
                    )
                    self.assertTrue(self.decide("combined", "allowed", "read"))
                    raise RevertOperation()
            except RevertOperation:
                self.assertFalse(self.decide("combined", "allowed", "read"))
        for active in (True, False):
            with self.subTest(active=active), self.assertRaises(RuntimeError):
                bridge.plan_access_transition((reader_acl, grouped._replace(active=active), custom_acl))

        custom_rule = grouped._replace(identifier=6, name="Data-only union", source_owned=False, domain="[]")
        if "ir.access" in self.env.registry:
            self.env["ir.access"].create(
                {
                    "name": custom_rule.name,
                    "model_id": self.env["ir.model"]._get(MODEL).id,
                    "operation": custom_rule.operation,
                    "domain": "[]",
                }
            )
            self.assertFalse(self.decide("combined", "allowed", "read"))
        else:
            self.install_policies((custom_rule,))
            self.assertTrue(self.decide("combined", "allowed", "read"))
        with self.assertRaises(RuntimeError):
            bridge.plan_access_transition((reader_acl, grouped, custom_rule))

    def test_global_parent_rule_restricts_delegated_children(self) -> None:
        self.install_policies(self.source)
        model = self.env["access.transition.child"]
        model_id = self.env["ir.model"]._get(model._name).id
        values = {"name": "Source-owned child permission", "model_id": model_id, "group_id": self.groups["writer"].id}
        if "ir.access" in self.env.registry:
            self.env["ir.access"].create(values | {"operation": "crud"})
        else:
            self.env["ir.model.access"].create(values | {"perm_" + mode: True for mode in MODES.values()})
        children = {name: model.create({"parent_id": self.records[name].id}) for name in ("allowed", "blocked")}
        ordinary = model.with_user(self.users["writer"]).with_context(allowed_company_ids=self.companies[0].ids)
        self.assertFalse(ordinary.env.su)
        self.assertNotEqual(ordinary.env.uid, SUPERUSER_ID)
        self.assertTrue(ordinary.browse(children["allowed"].id).read(["name"]))
        self.assertTrue(ordinary.search([("id", "=", children["allowed"].id)]))
        with self.assertRaises(AccessError):
            ordinary.browse(children["blocked"].id).read(["name"])
        self.assertFalse(ordinary.search([("id", "=", children["blocked"].id)]))
