import ast
import csv
import json
import logging
from datetime import datetime
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


class PolicyAudit(NamedTuple):
    create_uid: int | None
    create_date: datetime | str | None
    write_uid: int | None
    write_date: datetime | str | None


class PolicyAlias(NamedTuple):
    identifier: int
    module: str
    name: str
    noupdate: bool | None
    audit: PolicyAudit


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
    audit: PolicyAudit | None = None
    alias_records: tuple[PolicyAlias, ...] = ()
    global_rule: bool | None = None


class NativeAccess(NamedTuple):
    source: AccessPolicy
    group_id: int | None
    operation: str
    domain: str
    dependencies: tuple[AccessPolicy, ...] = ()


class AccessPlan(NamedTuple):
    converted: tuple[NativeAccess, ...]
    source_owned: tuple[AccessPolicy, ...]


def ensure_supported_policy_columns(cr: Any) -> None:
    audit = {"create_uid", "create_date", "write_uid", "write_date"}
    policy = {"id", "model_id", "name", "active", "perm_create", "perm_read", "perm_write", "perm_unlink"} | audit
    supported = {
        "ir_model_data": {"id", "module", "name", "model", "res_id", "noupdate"} | audit,
        "ir_model_access": policy | {"group_id"},
        "ir_rule": policy | {"domain_force", "global"},
        "rule_group_rel": {"rule_group_id", "group_id"},
    }
    cr.execute(
        "SELECT relation.relname, attribute.attname FROM pg_attribute attribute "
        "JOIN pg_class relation ON relation.oid = attribute.attrelid "
        "WHERE attribute.attrelid IN (SELECT to_regclass(name) FROM unnest(%s::text[]) AS name) "
        "AND attribute.attnum > 0 AND NOT attribute.attisdropped ORDER BY relation.relname, attribute.attname",
        (list(supported),),
    )
    unsupported = [f"{table}.{column}" for table, column in cr.fetchall() if column not in supported[table]]
    if unsupported:
        raise RuntimeError("Unsupported stored access metadata: " + ", ".join(unsupported))


def read_access_policies(cr: Any) -> tuple[AccessPolicy, ...]:
    ensure_supported_policy_columns(cr)
    cr.execute("SELECT name FROM ir_module_module WHERE state IN ('installed', 'to upgrade')")
    source_modules = {row[0] for row in cr.fetchall()} - {"__export__", "__custom__", "studio_customization"}
    cr.execute(
        "SELECT model, res_id, id, module, name, noupdate, create_uid, create_date, write_uid, write_date FROM ir_model_data "
        "WHERE model IN ('ir.model.access', 'ir.rule') ORDER BY module, name"
    )
    aliases: dict[tuple[str, int], list[PolicyAlias]] = {}
    for model, identifier, alias_id, module, name, protected, *audit in cr.fetchall():
        aliases.setdefault((model, identifier), []).append(PolicyAlias(alias_id, module, name, protected, PolicyAudit(*audit)))
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
        "access.perm_write, access.perm_unlink, access.group_id, "
        "access.create_uid, access.create_date, access.write_uid, access.write_date, NULL FROM ir_model_access access "
        "LEFT JOIN ir_model model ON model.id = access.model_id ORDER BY access.id"
    )
    access_rows = cr.fetchall()
    cr.execute(
        "SELECT rule.id, rule.name, model.model, rule.active, rule.perm_create, rule.perm_read, "
        "rule.perm_write, rule.perm_unlink, rule.domain_force, "
        "rule.create_uid, rule.create_date, rule.write_uid, rule.write_date, rule.global FROM ir_rule rule "
        "LEFT JOIN ir_model model ON model.id = rule.model_id ORDER BY rule.id"
    )
    rule_rows = cr.fetchall()
    policies = []
    for source_model, rows in (("ir.model.access", access_rows), ("ir.rule", rule_rows)):
        for identifier, name, model, active, create, read, write, unlink, extra, *metadata in rows:
            *audit, global_rule = metadata
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
                    tuple(f"{alias.module}.{alias.name}" for alias in references),
                    tuple(f"{alias.module}.{alias.name}" for alias in references if alias.noupdate),
                    any(alias.module in source_modules for alias in references),
                    tuple(delegated_parents.get(model, ())),
                    PolicyAudit(*audit),
                    tuple(references),
                    global_rule,
                )
            )
    if aliases:
        dangling = sorted(f"{alias.module}.{alias.name}" for references in aliases.values() for alias in references)
        raise RuntimeError(f"Dangling source access references: {', '.join(dangling)}")
    if groups:
        raise RuntimeError(f"Dangling source rule-group references: {sorted(groups)}")
    return tuple(policies)


def _policy_label(policy: AccessPolicy) -> str:
    aliases = ", ".join(policy.aliases) if policy.aliases else "unowned"
    return f"{policy.source_model}:{policy.identifier} ({aliases}; {policy.name})"


def ensure_native_access_replay(cr: Any) -> None:
    cr.execute("SELECT to_regclass('ir_module_module')")
    if cr.fetchone()[0]:
        cr.execute("SELECT latest_version FROM ir_module_module WHERE name = 'base'")
        installed = cr.fetchone()
        if installed and installed[0] and installed[0].split(".", 1)[0] != "20":
            return
    ensure_no_legacy_access_residue(cr)


def read_legacy_cleanup_consumers(cr: Any) -> tuple[str, ...]:
    retired = ("ir.model.access", "ir.rule")
    queries = (
        (
            "ir_model_fields",
            "SELECT id, relation FROM ir_model_fields WHERE relation IN (%s, %s) "
            "UNION SELECT id, model FROM ir_model_fields WHERE model IN (%s, %s) AND state = 'manual'",
            retired + retired,
        ),
        ("ir_attachment", "SELECT id, res_model FROM ir_attachment WHERE res_model IN (%s, %s)", retired),
        ("mail_activity_type", "SELECT id, res_model FROM mail_activity_type WHERE res_model IN (%s, %s)", retired),
        ("mail_followers", "SELECT id, res_model FROM mail_followers WHERE res_model IN (%s, %s)", retired),
        ("mail_message", "SELECT id, model FROM mail_message WHERE model IN (%s, %s)", retired),
        (
            "mail_activity",
            "SELECT activity.id, model.model FROM mail_activity activity "
            "JOIN ir_model model ON model.id = activity.res_model_id WHERE model.model IN (%s, %s)",
            retired,
        ),
        (
            "ir_act_server",
            "SELECT action.id, model.model FROM ir_act_server action "
            "JOIN ir_model model ON model.id = action.model_id WHERE model.model IN (%s, %s)",
            retired,
        ),
        (
            "ir_cron",
            "SELECT cron.id, model.model FROM ir_cron cron "
            "JOIN ir_act_server action ON action.id = cron.ir_actions_server_id "
            "JOIN ir_model model ON model.id = action.model_id WHERE model.model IN (%s, %s)",
            retired,
        ),
        (
            "ir_model_data",
            "SELECT data.id, data.module || '.' || data.name FROM ir_model_data data "
            "JOIN ir_model model ON model.id = data.res_id "
            "WHERE data.model = 'ir.model' AND model.model IN (%s, %s)",
            retired,
        ),
    )
    consumers = []
    for table, query, parameters in queries:
        cr.execute("SELECT to_regclass(%s)", (table,))
        if not cr.fetchone()[0]:
            continue
        cr.execute(query, parameters)
        consumers.extend(f"{table}:{identifier} ({model})" for identifier, model in cr.fetchall())
    return tuple(consumers)


def ensure_no_legacy_access_residue(cr: Any) -> None:
    cr.execute(
        "SELECT to_regclass('ir_model_data'), to_regclass('ir_model_access'), to_regclass('ir_rule'), to_regclass('rule_group_rel')"
    )
    alias_table, access_table, rule_table, group_table = cr.fetchone()
    if access_table or rule_table or group_table:
        ensure_supported_policy_columns(cr)
    references: dict[tuple[str, int | None], list[str]] = {}
    if alias_table:
        cr.execute(
            "SELECT model, res_id, module, name FROM ir_model_data "
            "WHERE model IN ('ir.model.access', 'ir.rule') ORDER BY module, name"
        )
        for model, identifier, module, name in cr.fetchall():
            references.setdefault((model, identifier), []).append(f"{module}.{name}")
    queries = (
        (access_table, "ir.model.access", "SELECT id, name FROM ir_model_access ORDER BY id"),
        (rule_table, "ir.rule", "SELECT id, name FROM ir_rule ORDER BY id"),
    )
    names = {}
    for exists, model, query in queries:
        if exists:
            cr.execute(query)
            for identifier, name in cr.fetchall():
                names[model, identifier] = name
                references.setdefault((model, identifier), [])
    if group_table:
        cr.execute("SELECT rule_group_id, group_id FROM rule_group_rel ORDER BY rule_group_id, group_id")
        for identifier, group in cr.fetchall():
            references.setdefault(("ir.rule", identifier), []).append(f"group:{group}")
    pending = [
        f"{model}:{identifier} ({', '.join(aliases) or 'unowned'}; {names.get((model, identifier))})"
        for (model, identifier), aliases in sorted(references.items(), key=lambda item: (item[0][0], item[0][1] is None, item[0][1]))
    ]
    cr.execute("SELECT to_regclass('ir_model')")
    if cr.fetchone()[0]:
        pending.extend(read_legacy_cleanup_consumers(cr))
    if pending:
        raise RuntimeError("Legacy access policies or references must not reach native cleanup:\n" + "\n".join(pending))


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


def _membership(groups: tuple[int, ...]) -> str:
    if not groups or any(type(group) is not int or group <= 0 for group in groups):
        raise ValueError("grouped policy has an unsupported group identity")
    return "(" + " or ".join(f"{group} in user.all_group_ids.ids" for group in sorted(set(groups))) + ")"


def _domain_operand(policy: AccessPolicy) -> str:
    expression = ast.parse(policy.domain.strip() or "[]", mode="eval").body
    if not isinstance(expression, (ast.List, ast.Tuple)):
        raise ValueError(f"domain requires a literal sequence: {_policy_label(policy)}")
    if not expression.elts:
        return "[(1, '=', 1)]"
    tokens = []
    expected = 1
    for item in expression.elts:
        if expected == 0:
            tokens.insert(0, ast.Constant(value="&"))
            expected = 1
        tokens.append(item)
        if isinstance(item, ast.Constant) and item.value in ("|", "&", "!"):
            expected += 0 if item.value == "!" else 1
            continue
        if (
            not isinstance(item, (ast.Tuple, ast.List))
            or len(item.elts) != 3
            or not isinstance(item.elts[0], ast.Constant)
            or not isinstance(item.elts[1], ast.Constant)
            or not isinstance(item.elts[1].value, str)
            or not isinstance(item.elts[0].value, (str, int))
        ):
            raise ValueError(f"domain has an unsupported branch: {_policy_label(policy)}")
        expected -= 1
    if expected:
        raise ValueError(f"domain has missing operands: {_policy_label(policy)}")
    return ast.unparse(ast.List(elts=tokens, ctx=ast.Load()))


def _compile_grouped_rule(policy: AccessPolicy, policies: tuple[AccessPolicy, ...]) -> NativeAccess:
    siblings = _grouped_siblings(policy, policies)
    dependencies = tuple(sorted((policy, *siblings), key=lambda item: item.identifier))
    permissions = tuple(item for item in policies if item.source_model == "ir.model.access" and item.model == policy.model)
    plain = tuple(
        item
        for item in permissions
        if not item.source_owned
        and item.active
        and item.name
        and not item.delegated_parents
        and set(item.groups) & set(policy.groups)
        and set(item.operation) & set(policy.operation)
    )
    uncovered = [
        (group, operation)
        for group in policy.groups
        for operation in policy.operation
        if not any(group in item.groups and operation in item.operation for item in plain)
    ]
    if uncovered:
        related = "; ".join(_policy_label(item) for item in permissions)
        raise ValueError(
            f"grouped restriction lacks plain data-only permissions for {uncovered}; source permissions: {related or 'none'}"
        )
    if siblings and any(not item.active for item in dependencies):
        raise ValueError("grouped policy activation interacts with a sibling")
    domains = set()
    for operation in policy.operation:
        branches = [
            f"({_domain_operand(item)} if {_membership(item.groups)} else [(0, '=', 1)])"
            for item in dependencies
            if operation in item.operation
        ]
        expression = repr(["|"] * (len(branches) - 1)) + " + " + " + ".join(branches)
        domains.add(f"(({expression}) if {_membership(policy.groups)} else [])")
    if len(domains) != 1:
        raise ValueError("grouped operations require different destination domains and cannot retain one policy row")
    return NativeAccess(policy, None, policy.operation, domains.pop(), dependencies + plain)


def plan_access_transition(policies: tuple[AccessPolicy, ...]) -> AccessPlan:
    converted: dict[tuple[str, int], NativeAccess] = {}
    permissions = []
    source_owned = []
    refusals = []
    for policy in policies:
        label = _policy_label(policy)
        if not policy.model:
            refusals.append(f"{label}: target model identity is missing")
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
        if not policy.operation:
            if policy.source_model == "ir.model.access" and not policy.source_owned:
                group = policy.groups[0] if policy.groups else None
                converted[policy.source_model, policy.identifier] = NativeAccess(
                    policy, group, "r", "[(0, '=', 1)]" if group is not None else ""
                )
            else:
                refusals.append(f"{label}: zero-operation policy requires qualified source-owner data or a rule mapping")
            continue
        if policy.source_model == "ir.model.access" and not policy.groups:
            refusals.append(f"{label}: groupless permission requires verified native everyone-group coverage")
            continue
        if policy.source_owned:
            source_owned.append(policy)
            continue
        if policy.source_model == "ir.model.access":
            permissions.append(policy)
            continue
        if not policy.groups:
            converted[policy.source_model, policy.identifier] = NativeAccess(policy, None, policy.operation, policy.domain)
            continue
        siblings = _grouped_siblings(policy, policies)
        related = "; ".join(_policy_label(sibling) for sibling in siblings)
        try:
            converted[policy.source_model, policy.identifier] = _compile_grouped_rule(policy, policies)
        except (ValueError, SyntaxError) as error:
            refusals.append(f"{label}: {error}; sibling policies: {related or 'none'}")
    restrictions = tuple(converted.values())
    for policy in permissions:
        siblings = _grouped_siblings(policy, policies)
        covering = tuple(
            item.source
            for item in restrictions
            if item.source.source_model == "ir.rule"
            and item.source.active
            and item.source.model == policy.model
            and policy.groups[0] in item.source.groups
        )
        uncovered = {
            operation
            for sibling in siblings
            for operation in set(sibling.operation) & set(policy.operation)
            if not any(operation in rule.operation for rule in covering)
        }
        if uncovered:
            related = "; ".join(_policy_label(sibling) for sibling in siblings)
            refusals.append(f"{_policy_label(policy)}: plain permission would bypass grouped policies: {related}")
            continue
        converted[policy.source_model, policy.identifier] = NativeAccess(policy, policy.groups[0], policy.operation, "", covering)
    if refusals:
        raise RuntimeError("Unsupported native access transition:\n" + "\n".join(refusals))
    return AccessPlan(
        tuple(
            converted[policy.source_model, policy.identifier]
            for policy in policies
            if (policy.source_model, policy.identifier) in converted
        ),
        tuple(source_owned),
    )


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
        ensure_no_legacy_access_residue(cr)
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
