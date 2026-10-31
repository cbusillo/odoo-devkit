from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

ROOT_XMLID = "base.view_server_action_form"
VIEW_XMLID = "mail.view_server_action_form_template"
REFERENCE_ENV = "CM_WEBSITE_MAIL_REFERENCE"
REFERENCE_DIGEST_ENV = "CM_WEBSITE_MAIL_REFERENCE_SHA256"
AUDIT_FIELDS = {"id", "inherit_id", "create_uid", "write_uid", "create_date", "write_date", "arch_prev"}
MAIL_VIEW_FIELDS = {
    "name",
    "model",
    "key",
    "type",
    "priority",
    "mode",
    "active",
    "arch_fs",
    "arch_db",
    "arch_updated",
    "website_id",
    "xmlids",
    "parent_xmlids",
}


def _binding(cr: Any, xmlid: str, *, lock: bool) -> dict[str, Any]:
    module, name = xmlid.split(".", 1)
    statement = """
        SELECT d.id, d.model, d.res_id, d.noupdate, v.id
        FROM ir_model_data d LEFT JOIN ir_ui_view v ON v.id=d.res_id AND d.model='ir.ui.view'
        WHERE d.module=%s AND d.name=%s
    """
    if lock:
        statement += " FOR UPDATE OF d"
    cr.execute(statement, (module, name))
    rows = cr.fetchall()
    if len(rows) != 1 or rows[0][1] != "ir.ui.view" or rows[0][4] is None:
        raise RuntimeError(f"Deferred view conversion: invalid live binding {xmlid}")
    identity, model, record, noupdate, _ = rows[0]
    return {"xmlid_id": identity, "model": model, "record_id": record, "noupdate": noupdate}


def capture(cr: Any, *, lock: bool = False) -> dict[str, Any]:
    if lock:
        cr.execute("LOCK TABLE ir_ui_view, ir_ui_view_custom, ir_model_data IN SHARE ROW EXCLUSIVE MODE")
    root = _binding(cr, ROOT_XMLID, lock=lock)
    target = _binding(cr, VIEW_XMLID, lock=lock)
    cr.execute(
        """
        WITH RECURSIVE family AS (
            SELECT id FROM ir_ui_view WHERE id=%s
            UNION
            SELECT v.id FROM ir_ui_view v JOIN family f ON v.inherit_id=f.id
        ) SELECT to_jsonb(v) FROM ir_ui_view v JOIN family f ON f.id=v.id ORDER BY v.id
        """,
        (root["record_id"],),
    )
    records = [row[0] for row in cr.fetchall()]
    identities = [row["id"] for row in records]
    if target["record_id"] not in identities:
        raise RuntimeError("Deferred view conversion: target is outside the expected inheritance family")
    if lock:
        cr.execute("SELECT id FROM ir_ui_view WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (identities,))
        cr.fetchall()
    cr.execute(
        "SELECT res_id,module,name,model,noupdate FROM ir_model_data WHERE model='ir.ui.view' AND res_id=ANY(%s) "
        "ORDER BY res_id,module,name",
        (identities,),
    )
    aliases: dict[int, list[dict[str, Any]]] = {identity: [] for identity in identities}
    for record, module, name, model, noupdate in cr.fetchall():
        aliases[record].append({"xmlid": f"{module}.{name}", "model": model, "noupdate": noupdate})
    cr.execute("SELECT to_jsonb(c) FROM ir_ui_view_custom c WHERE ref_id=ANY(%s) ORDER BY c.id", (identities,))
    customizations = [row[0] for row in cr.fetchall()]
    cr.execute("SELECT 1 FROM ir_ui_view_group_rel WHERE view_id=ANY(%s) LIMIT 1", (identities,))
    if cr.fetchone():
        raise RuntimeError("Deferred view conversion: preserve restricted inheritance family")
    views = []
    for record in records:
        if not aliases[record["id"]]:
            raise RuntimeError(f"Deferred view conversion: unowned inherited view {record['id']}")
        values = {key: value for key, value in record.items() if key not in AUDIT_FIELDS}
        values["xmlids"] = aliases[record["id"]]
        values["parent_xmlids"] = aliases.get(record["inherit_id"], [])
        views.append(values)
    views.sort(key=lambda row: json.dumps(row["xmlids"], sort_keys=True))
    target_row = next(row for row in views if any(alias["xmlid"] == VIEW_XMLID for alias in row["xmlids"]))
    cr.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
    uuid = cr.fetchone()
    if not uuid or not uuid[0]:
        raise RuntimeError("Deferred view conversion: missing database identity")
    cr.execute("SELECT name,latest_version FROM ir_module_module WHERE state IN ('installed','to upgrade') ORDER BY name")
    return {
        "database_uuid": uuid[0],
        "modules": dict(cr.fetchall()),
        "views": views,
        "target": target_row,
        "binding": target,
        "customizations": customizations,
        "history": [{key: value for key, value in row.items() if key in AUDIT_FIELDS} for row in records],
        "identities": {str(identity): values for identity, values in aliases.items()},
    }


def canonical_architecture(path: Path) -> str:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    document = ElementTree.fromstring(path.read_bytes(), parser=parser)
    records = document.findall(".//record")
    if len(records) != 1 or records[0].get("id") != VIEW_XMLID.split(".", 1)[1]:
        raise RuntimeError("Unsupported target Mail view source")
    if records[0].get("model") != "ir.ui.view":
        raise RuntimeError("Unsupported target Mail view model")
    fields = {field.get("name"): field for field in records[0].findall("field")}
    if set(fields) != {"name", "model", "inherit_id", "arch"} or fields["inherit_id"].get("ref") != ROOT_XMLID:
        raise RuntimeError("Unsupported target Mail view fields or parent")
    if fields["model"].text != "ir.actions.server":
        raise RuntimeError("Unsupported target server-action model")
    return _render_architecture(fields["arch"])


def _render_architecture(arch: ElementTree.Element) -> str:
    children = list(arch)
    if not children:
        raise RuntimeError("Empty target Mail architecture")
    architecture = copy.deepcopy(children[0]) if len(children) == 1 else ElementTree.Element("data")
    if len(children) != 1:
        architecture.extend(copy.deepcopy(children))
    result = ElementTree.tostring(architecture, encoding="unicode")
    if "%(" in result or "ref(" in result:
        raise RuntimeError("Unsupported target Mail architecture interpolation")
    return result


def _normalized_architecture(architecture: str) -> str:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    root = ElementTree.fromstring(architecture, parser=parser)
    if root.tag == "data" and not root.attrib and len(root) == 1 and not (root.text or "").strip():
        root = root[0]
    for element in root.iter():
        if element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    return ElementTree.canonicalize(ElementTree.tostring(root, encoding="unicode"), with_comments=True)


def _common_view_defaults(view: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in view.items() if key not in {"name", "model", "arch_fs", "arch_db", "xmlids"}}


def _qualify_automation_defaults(reference: dict[str, Any], defaults: dict[str, Any]) -> None:
    baseline = _common_view_defaults(reference["target"])
    if defaults == baseline:
        return
    alias = [{"xmlid": "sms.ir_actions_server_view_form", "model": "ir.ui.view", "noupdate": False}]
    siblings = [view for view in reference["views"] if view["xmlids"] == alias]
    alternate = dict(baseline, customize_show=False)
    if (
        baseline.get("customize_show") is not None
        or len(siblings) != 1
        or siblings[0].get("model") != "ir.actions.server"
        or siblings[0].get("mode") != "extension"
        or siblings[0].get("type") != "form"
        or _common_view_defaults(siblings[0]) != alternate
        or defaults != alternate
    ):
        raise RuntimeError("Unqualified Base Automation defaults")


def _automation_view(reference: dict[str, Any], source_xml: str, defaults: dict[str, Any]) -> dict[str, Any]:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    document = ElementTree.fromstring(source_xml, parser=parser)
    records = document.findall(".//record")
    if len(records) != 1 or records[0].attrib != {"id": "view_server_action_form", "model": "ir.ui.view"}:
        raise RuntimeError("Unsupported source Base Automation view")
    fields = {field.get("name"): field for field in records[0].findall("field")}
    if (
        len(records[0]) != len(fields)
        or set(fields) != {"name", "model", "inherit_id", "arch"}
        or fields["name"].attrib != {"name": "name"}
        or fields["model"].attrib != {"name": "model"}
        or fields["inherit_id"].attrib != {"name": "inherit_id", "ref": ROOT_XMLID}
        or fields["arch"].attrib != {"name": "arch", "type": "xml"}
        or fields["model"].text != "ir.actions.server"
    ):
        raise RuntimeError("Unsupported source Base Automation fields or parent")
    view = copy.deepcopy(reference["target"])
    view.update(defaults)
    view.update(
        name=fields["name"].text,
        model=fields["model"].text,
        arch_fs="base_automation/views/ir_actions_server_views.xml",
        arch_db={"en_US": _normalized_architecture(_render_architecture(fields["arch"]))},
        xmlids=[{"xmlid": "base_automation.view_server_action_form", "model": "ir.ui.view", "noupdate": False}],
    )
    return view


def _qualified_family(profile: dict[str, Any]) -> dict[str, Any]:
    reference = profile["reference"]
    binding = profile["source_family"]
    payload = binding["manifest"].encode()
    if hashlib.sha256(payload).hexdigest() != binding["manifest_sha256"]:
        raise RuntimeError("Source closure manifest changed")
    source = json.loads(payload)
    modules = source["modules"]
    if (
        source["source_image"] != profile["source_image"]
        or "@sha256:" not in source["source_image"]
        or not modules.get("base_automation")
        or not modules.get("base", "").startswith("19.")
        or any(version is not None and (not isinstance(version, str) or not version) for version in modules.values())
        or any(modules.get(name) != version for name, version in reference["modules"].items())
        or any(
            alias["xmlid"] == "base_automation.view_server_action_form" for view in reference["views"] for alias in view["xmlids"]
        )
    ):
        raise RuntimeError("Unsupported Base Automation source closure")
    source_xml = binding["base_automation_xml"]
    if hashlib.sha256(source_xml.encode()).hexdigest() != source["base_automation_xml_sha256"]:
        raise RuntimeError("Source Base Automation XML changed")
    defaults = source["base_automation_defaults"]
    _qualify_automation_defaults(reference, defaults)
    expected = copy.deepcopy(reference)
    expected["modules"] = modules
    expected["views"].append(_automation_view(reference, source_xml, defaults))
    expected["views"].sort(key=lambda row: json.dumps(row["xmlids"], sort_keys=True))
    return expected


def qualify_source_family(
    profile: dict[str, Any], source_manifest: Path, manifest_sha256: str, automation_source: Path
) -> dict[str, Any]:
    qualified = copy.deepcopy(profile)
    qualified["source_family"] = {
        "manifest": source_manifest.read_bytes().decode(),
        "manifest_sha256": manifest_sha256,
        "base_automation_xml": automation_source.read_bytes().decode(),
    }
    _qualified_family(qualified)
    return qualified


def admit_profile(current: dict[str, Any], profile: dict[str, Any]) -> None:
    if "source_family" not in profile:
        contract = profile.get("history", {}).get("source_contract")
        source_modules = contract["modules"] if contract is not None else None
        admit(current, profile["reference"], whole_family=True, source_modules=source_modules)
        return
    expected = _qualified_family(profile)
    views = copy.deepcopy(current["views"])
    for view in views:
        if view["xmlids"] == [{"xmlid": "base_automation.view_server_action_form", "model": "ir.ui.view", "noupdate": False}]:
            architectures = view["arch_db"]
            if set(architectures) != {"en_US"}:
                raise RuntimeError("Deferred Base Automation translation rebase")
            view["arch_db"] = {"en_US": _normalized_architecture(architectures["en_US"])}
    views.sort(key=lambda row: json.dumps(row["xmlids"], sort_keys=True))
    admit(dict(current, views=views), expected, whole_family=True)


def write_reference(
    cr: Any,
    output: Path,
    target_xml: Path,
    source_image: str,
    target_image: str,
    *,
    history: dict[str, Any] | None = None,
    footer: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
    countries: dict[str, Any] | None = None,
) -> str:
    cr.execute("SHOW transaction_read_only")
    if cr.fetchone()[0] != "on":
        raise RuntimeError("Reference generation requires an independent read-only transaction")
    reference = capture(cr)
    if not reference["modules"].get("base", "").startswith("19."):
        raise RuntimeError("Reference generation requires a native19 stock database")
    if reference["customizations"] or reference["binding"]["noupdate"]:
        raise RuntimeError("Reference database contains unsupported customizations")
    target = reference["target"]
    if target.get("model") != "ir.actions.server" or target.get("type") != "form" or not target.get("active"):
        raise RuntimeError("Reference database does not contain the active native server-action form")
    if target.get("arch_updated"):
        raise RuntimeError("Reference database contains a modified Mail view")
    if set(reference["target"].get("arch_db", {})) != {"en_US"}:
        raise RuntimeError("Reference requires a separately qualified translation rebase")
    canonical_architecture(target_xml)
    for image in (source_image, target_image):
        if "@sha256:" not in image:
            raise RuntimeError("Reference images must be immutable digest references")
    profile = {
        "reference": reference,
        "source_image": source_image,
        "target_image": target_image,
        "target_xml_sha256": hashlib.sha256(target_xml.read_bytes()).hexdigest(),
    }
    if history is not None:
        if history.get("database_uuid") != reference["database_uuid"] or history.get("modules") != reference["modules"]:
            raise RuntimeError("History and view reference must come from the same independent source database")
        profile["history"] = history
    if footer is not None:
        if footer.get("database_uuid") != reference["database_uuid"] or footer.get("modules") != reference["modules"]:
            raise RuntimeError("Footer and Mail reference must come from the same independent source database")
        profile["footer"] = footer
    if templates is not None:
        if templates.get("database_uuid") != reference["database_uuid"] or templates.get("modules") != reference["modules"]:
            raise RuntimeError("Templates and Mail reference must come from the same independent source database")
        if templates["target"]["image"] != target_image:
            raise RuntimeError("Template target must use the same immutable target image")
        profile["templates"] = templates
    if countries is not None:
        if countries.get("database_uuid") != reference["database_uuid"] or countries.get("modules") != reference["modules"]:
            raise RuntimeError("Countries and Mail reference must come from the same independent source database")
        if countries["target"]["image"] != target_image:
            raise RuntimeError("Country target must use the same immutable target image")
        profile["countries"] = countries
    payload = json.dumps(profile, sort_keys=True, indent=2).encode() + b"\n"
    with output.open("xb") as stream:
        stream.write(payload)
    return hashlib.sha256(payload).hexdigest()


def load_reference(target_xml: Path) -> dict[str, Any]:
    path = os.environ.get(REFERENCE_ENV)
    expected = os.environ.get(REFERENCE_DIGEST_ENV)
    if not path or not expected:
        raise RuntimeError("Mail conversion requires an independently qualified reference and digest")
    payload = Path(path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("Mail reference artifact changed")
    profile = json.loads(payload)
    if hashlib.sha256(target_xml.read_bytes()).hexdigest() != profile["target_xml_sha256"]:
        raise RuntimeError("Target Mail source differs from the qualified reference")
    canonical_architecture(target_xml)
    return profile


def admit(
    current: dict[str, Any], reference: dict[str, Any], *, whole_family: bool, source_modules: dict[str, str | None] | None = None
) -> None:
    if current["database_uuid"] == reference["database_uuid"]:
        raise RuntimeError("Reference must be generated independently from the candidate")
    if current["customizations"] or current["binding"]["noupdate"]:
        raise RuntimeError("Deferred view conversion: preserve linked customizations or noupdate view")
    current_target = {field: current["target"].get(field) for field in MAIL_VIEW_FIELDS}
    reference_target = {field: reference["target"].get(field) for field in MAIL_VIEW_FIELDS}
    if current_target != reference_target:
        raise RuntimeError("Deferred view conversion: modified architecture, translations, binding or parent")
    if whole_family:
        expected_modules = reference["modules"] if source_modules is None else source_modules
        if current["modules"] != expected_modules or any(
            current["modules"].get(name) != version for name, version in reference["modules"].items()
        ):
            raise RuntimeError("Deferred view conversion: source module closure or independent component versions differ")
        if current["views"] != reference["views"]:
            raise RuntimeError("Deferred view conversion: inherited edits differ")


def admit_before_effects(cr: Any, target_xml: Path) -> None:
    profile = load_reference(target_xml)
    admit_profile(capture(cr, lock=True), profile)


def migrate_mail(cr: Any, version: str | None, target_xml: Path) -> None:
    profile = load_reference(target_xml)
    if version != profile["reference"]["modules"].get("mail"):
        raise RuntimeError("Mail pre-stage requires the qualified old source version; restore a fresh copy")
    current = capture(cr, lock=True)
    admit(current, profile["reference"], whole_family=False)
    architecture = json.dumps({"en_US": canonical_architecture(target_xml)})
    identity = current["binding"]["record_id"]
    cr.execute("UPDATE ir_ui_view SET arch_db=%s::jsonb WHERE id=%s RETURNING id", (architecture, identity))
    if cr.fetchone() != (identity,):
        raise RuntimeError("Mail view identity changed during conversion")
