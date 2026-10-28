import csv
import json
import logging
from pathlib import Path
from typing import Any, NamedTuple

SOURCE_STATE = "state_in_or"
TARGET_STATE = "state_in_od"
STATE_DATA_FILE = Path("/odoo/odoo/addons/base/data/res.country.state.csv")
BANK_SELECTOR_19 = "expr=\"//field[@name='acc_number']\""
BANK_SELECTOR_20 = "expr=\"//field[@name='account_number']\""
_logger = logging.getLogger(__name__)


class Reference(NamedTuple):
    model: str
    record_id: int
    noupdate: bool


class AccessPolicy(NamedTuple):
    source_model: str
    identifier: int
    name: str | None
    model: str | None
    groups: tuple[int, ...]
    operation: str
    domain: str
    active: bool
    aliases: tuple[str, ...]
    protected_aliases: tuple[str, ...]
    source_owned: bool
    delegated_parents: tuple[str, ...] = ()


class NativeAccess(NamedTuple):
    source: AccessPolicy
    group_id: int | None
    operation: str
    domain: str


class AccessPlan(NamedTuple):
    converted: tuple[NativeAccess, ...]
    source_owned: tuple[AccessPolicy, ...]


def read_access_policies(cr: Any) -> tuple[AccessPolicy, ...]:
    cr.execute("SELECT name FROM ir_module_module WHERE state IN ('installed', 'to upgrade')")
    source_modules = {row[0] for row in cr.fetchall()} - {"__export__", "__custom__", "studio_customization"}
    cr.execute(
        "SELECT model, res_id, module, name, noupdate FROM ir_model_data "
        "WHERE model IN ('ir.model.access', 'ir.rule') ORDER BY module, name"
    )
    aliases: dict[tuple[str, int], list[tuple[str, str, bool]]] = {}
    for model, identifier, module, name, protected in cr.fetchall():
        aliases.setdefault((model, identifier), []).append((module, name, protected))
    cr.execute("SELECT id FROM res_groups")
    existing_groups = {row[0] for row in cr.fetchall()}
    cr.execute("SELECT rule_group_id, group_id FROM rule_group_rel ORDER BY rule_group_id, group_id")
    groups: dict[int, list[int]] = {}
    for identifier, group in cr.fetchall():
        groups.setdefault(identifier, []).append(group)
    cr.execute(
        "SELECT child.model, parent.model FROM ir_model_inherit inheritance "
        "LEFT JOIN ir_model child ON child.id = inheritance.model_id "
        "LEFT JOIN ir_model parent ON parent.id = inheritance.parent_id "
        "WHERE inheritance.parent_field_id IS NOT NULL ORDER BY child.model, parent.model"
    )
    delegated_parents: dict[str, list[str]] = {}
    for child, parent in cr.fetchall():
        if not child or not parent:
            raise RuntimeError("Source delegated inheritance has a missing model identity")
        delegated_parents.setdefault(child, []).append(parent)
    cr.execute(
        "SELECT access.id, access.name, model.model, access.active, access.perm_create, access.perm_read, "
        "access.perm_write, access.perm_unlink, access.group_id FROM ir_model_access access "
        "LEFT JOIN ir_model model ON model.id = access.model_id ORDER BY access.id"
    )
    access_rows = cr.fetchall()
    cr.execute(
        "SELECT rule.id, rule.name, model.model, rule.active, rule.perm_create, rule.perm_read, "
        "rule.perm_write, rule.perm_unlink, rule.domain_force FROM ir_rule rule "
        "LEFT JOIN ir_model model ON model.id = rule.model_id ORDER BY rule.id"
    )
    rule_rows = cr.fetchall()
    policies = []
    for source_model, rows in (("ir.model.access", access_rows), ("ir.rule", rule_rows)):
        for identifier, name, model, active, create, read, write, unlink, extra in rows:
            references = aliases.pop((source_model, identifier), [])
            policy_groups = tuple(groups.pop(identifier, ())) if source_model == "ir.rule" else (() if extra is None else (extra,))
            missing_groups = set(policy_groups) - existing_groups
            if missing_groups:
                raise RuntimeError(f"Source {source_model}:{identifier} references missing groups: {sorted(missing_groups)}")
            policies.append(
                AccessPolicy(
                    source_model,
                    identifier,
                    name,
                    model,
                    policy_groups,
                    "".join(operation for operation, enabled in zip("crud", (create, read, write, unlink), strict=True) if enabled),
                    (extra or "") if source_model == "ir.rule" else "",
                    bool(active),
                    tuple(f"{module}.{alias}" for module, alias, _ in references),
                    tuple(f"{module}.{alias}" for module, alias, protected in references if protected),
                    any(module in source_modules for module, _, _ in references),
                    tuple(delegated_parents.get(model, ())),
                )
            )
    if aliases:
        dangling = sorted(f"{module}.{name}" for references in aliases.values() for module, name, _ in references)
        raise RuntimeError(f"Dangling source access references: {', '.join(dangling)}")
    if groups:
        raise RuntimeError(f"Dangling source rule-group references: {sorted(groups)}")
    return tuple(policies)


def _policy_label(policy: AccessPolicy) -> str:
    aliases = ", ".join(policy.aliases) if policy.aliases else "unowned"
    return f"{policy.source_model}:{policy.identifier} ({aliases}; {policy.name})"


def _grouped_siblings(policy: AccessPolicy, policies: tuple[AccessPolicy, ...]) -> tuple[AccessPolicy, ...]:
    return tuple(
        sibling
        for sibling in policies
        if sibling.source_model == "ir.rule"
        and (sibling.source_model, sibling.identifier) != (policy.source_model, policy.identifier)
        and sibling.groups
        and sibling.model == policy.model
        and set(sibling.operation) & set(policy.operation)
    )


def plan_access_transition(policies: tuple[AccessPolicy, ...]) -> AccessPlan:
    converted = []
    source_owned = []
    refusals = []
    for policy in policies:
        label = _policy_label(policy)
        if not policy.model:
            refusals.append(f"{label}: target model identity is missing")
            continue
        if not policy.operation:
            refusals.append(f"{label}: policy with no operations has no qualified native representation")
            continue
        if not policy.name:
            refusals.append(f"{label}: nameless policy has no qualified native name mapping")
            continue
        if policy.delegated_parents:
            refusals.append(f"{label}: delegated parents require native permission/rule mapping: {policy.delegated_parents}")
            continue
        if policy.model in {"ir.model.access", "ir.rule", "ir.access"}:
            refusals.append(f"{label}: merged security-model policy requires an explicit reference mapping")
            continue
        if policy.source_model == "ir.model.access" and not policy.groups:
            refusals.append(f"{label}: groupless permission requires verified native everyone-group coverage")
            continue
        if policy.source_owned:
            source_owned.append(policy)
            continue
        if policy.source_model == "ir.model.access":
            siblings = _grouped_siblings(policy, policies)
            if siblings:
                related = "; ".join(_policy_label(sibling) for sibling in siblings)
                refusals.append(f"{label}: plain permission would bypass grouped policies: {related}")
                continue
            converted.append(NativeAccess(policy, policy.groups[0], policy.operation, ""))
            continue
        if not policy.groups:
            converted.append(NativeAccess(policy, None, policy.operation, policy.domain))
            continue
        siblings = _grouped_siblings(policy, policies)
        related = "; ".join(_policy_label(sibling) for sibling in siblings)
        reason = "grouped restriction requires a qualified native mapping"
        if siblings:
            reason = f"grouped restriction depends on sibling policies: {related}; stored sibling copies are not admitted"
        refusals.append(f"{label}: {reason}")
    if refusals:
        raise RuntimeError("Unsupported native access transition:\n" + "\n".join(refusals))
    return AccessPlan(tuple(converted), tuple(source_owned))


def _reference(cr: Any, module: str, name: str) -> Reference | None:
    cr.execute("SELECT model, res_id, noupdate FROM ir_model_data WHERE module = %s AND name = %s FOR UPDATE", (module, name))
    rows = cr.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"Ambiguous core reference {module}.{name}")
    return Reference(*rows[0]) if rows else None


def _require_source(cr: Any, module: str, name: str, model: str) -> Reference:
    reference = _reference(cr, module, name)
    if reference is None or reference.model != model:
        raise RuntimeError(f"Unsupported core reference {module}.{name}: expected {model}")
    return reference


def _check_target(cr: Any, name: str, source: Reference, *, protect: bool = False) -> Reference | None:
    target = _reference(cr, "base", name)
    if target is not None and (
        target.model != source.model or target.record_id != source.record_id or (protect and not target.noupdate)
    ):
        raise RuntimeError(f"Conflicting core reference base.{name}")
    return target


def _state_source(cr: Any) -> Reference:
    with STATE_DATA_FILE.open(newline="") as stream:
        targets = [row for row in csv.DictReader(stream) if row["id"] == TARGET_STATE]
    if len(targets) != 1:
        raise RuntimeError("Target state data does not identify one supported reference")
    target = targets[0]
    country_name = target["country_id:id"].removeprefix("base.")
    country = _require_source(cr, "base", country_name, "res.country")
    source = _require_source(cr, "base", SOURCE_STATE, "res.country.state")
    cr.execute(
        "SELECT state.name FROM res_country_state state JOIN res_country country ON country.id = state.country_id "
        "WHERE state.id = %s AND country.id = %s AND country.code = %s AND state.code = %s FOR UPDATE",
        (source.record_id, country.record_id, country_name.upper(), target["code"]),
    )
    rows = cr.fetchall()
    if len(rows) != 1 or (rows[0][0] != target["name"] and not source.noupdate):
        raise RuntimeError("Unsupported or customized source state; native data must not overwrite it")
    return source


def _website_source(cr: Any) -> Reference | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'website'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    source = _require_source(cr, "website", "default_website", "website")
    cr.execute("SELECT id FROM website WHERE id = %s FOR UPDATE", (source.record_id,))
    if cr.fetchone() is None or not source.noupdate:
        raise RuntimeError("Unsupported source website or metadata; native defaults must not overwrite it")
    return source


def _bind_target(cr: Any, name: str, source: Reference) -> None:
    cr.execute(
        "INSERT INTO ir_model_data (module, name, model, res_id, noupdate) VALUES ('base', %s, %s, %s, %s)",
        (name, source.model, source.record_id, source.noupdate),
    )
    _logger.info("Bound core reference base.%s to preserved %s row %s", name, source.model, source.record_id)


def _bank_form_source(cr: Any) -> tuple[int, dict[str, str]] | None:
    cr.execute("SELECT state FROM ir_module_module WHERE name = 'account'")
    installed = cr.fetchone()
    if installed is None or installed[0] not in {"installed", "to upgrade"}:
        return None
    source = _require_source(cr, "account", "view_partner_bank_form_inherit_account", "ir.ui.view")
    parent = _require_source(cr, "base", "view_partner_bank_form", "ir.ui.view")
    cr.execute("SELECT model, inherit_id, mode, arch_db FROM ir_ui_view WHERE id = %s FOR UPDATE", (source.record_id,))
    rows = cr.fetchall()
    if source.noupdate or parent.noupdate or len(rows) != 1 or rows[0][:3] != ("res.partner.bank", parent.record_id, "primary"):
        raise RuntimeError("Unsupported account bank form ownership or inheritance")
    architectures = rows[0][3]
    if not architectures or not isinstance(architectures, dict) or any(not isinstance(arch, str) for arch in architectures.values()):
        raise RuntimeError("Unsupported account bank form architecture")
    counts = {(arch.count(BANK_SELECTOR_19), arch.count(BANK_SELECTOR_20)) for arch in architectures.values()}
    if counts == {(0, 1)}:
        return None
    if counts != {(1, 0)}:
        raise RuntimeError("Ambiguous account bank form selectors or mixed language transition")
    return source.record_id, {
        language: arch.replace(BANK_SELECTOR_19, BANK_SELECTOR_20, 1) for language, arch in architectures.items()
    }


def migrate(cr: Any, version: str | None) -> None:
    major = (version or "").split(".", 1)[0]
    if major == "20":
        return
    if major != "19":
        raise RuntimeError("Core reference preparation requires a native 19-to-20 crossing")
    plan = plan_access_transition(read_access_policies(cr))
    pending = tuple(target.source for target in plan.converted) + plan.source_owned
    if pending:
        raise RuntimeError(
            "Native access policy application is not qualified:\n" + "\n".join(_policy_label(policy) for policy in pending)
        )
    state = _state_source(cr)
    website = _website_source(cr)
    state_target = _check_target(cr, TARGET_STATE, state, protect=state.noupdate)
    website_target = _check_target(cr, "default_website", website, protect=True) if website is not None else None
    bank_form = _bank_form_source(cr)
    if state_target is None:
        _bind_target(cr, TARGET_STATE, state)
    if website is not None and website_target is None:
        _bind_target(cr, "default_website", website)
    if bank_form is not None:
        record_id, architectures = bank_form
        cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", (json.dumps(architectures), record_id))
        _logger.info("Retargeted account.view_partner_bank_form_inherit_account on preserved ir.ui.view row %s", record_id)
