import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"


def load_instance_override_payload() -> dict[str, object] | None:
    encoded_payload = os.environ.get(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, "").strip()
    if not encoded_payload:
        return None
    try:
        decoded_payload = base64.b64decode(encoded_payload, validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
        raise RuntimeError("Invalid Odoo instance override payload.") from error
    if not isinstance(parsed_payload, dict):
        raise RuntimeError("Odoo instance override payload must decode to an object.")
    return parsed_payload


def payload_has_launchplane_settings(parsed_payload: dict[str, object] | None) -> bool:
    if not parsed_payload:
        return False
    return bool(parsed_payload.get("config_parameters") or parsed_payload.get("addon_settings"))


def _field_values(record: Any, values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if key in record._fields}


def _write_existing_fields(record: Any, values: dict[str, object]) -> None:
    filtered_values = _field_values(record, values)
    if filtered_values:
        record.sudo().write(filtered_values)


def _module_is_installed(env: Any, module_name: object) -> bool:
    normalized_module_name = str(module_name or "").strip()
    if not normalized_module_name:
        return False
    module = (
        env["ir.module.module"]
        .sudo()
        .search(
            [("name", "=", normalized_module_name), ("state", "=", "installed")],
            limit=1,
        )
    )
    return bool(module)


def _resolve_bootstrap_logo_path(raw_logo_path: object) -> Path | None:
    logo_path = str(raw_logo_path or "").strip()
    if not logo_path:
        return None
    candidate_paths: list[Path] = []
    candidate = Path(logo_path)
    if candidate.is_absolute():
        candidate_paths.append(candidate)
    else:
        candidate_paths.append(Path("/opt/project") / logo_path)
        candidate_paths.append(Path("/opt/project/addons") / logo_path)
    for candidate_path in candidate_paths:
        if candidate_path.is_file():
            return candidate_path
    formatted_candidates = ", ".join(str(candidate_path) for candidate_path in candidate_paths)
    raise RuntimeError(f"Website bootstrap logo file not found: {formatted_candidates}")


def _find_website_page(env: Any, website: Any, *, xmlid: str, url: str) -> Any | None:
    page = None
    if xmlid:
        candidate = env.ref(xmlid, raise_if_not_found=False)
        if candidate and candidate._name == "website.page":
            page = candidate.sudo()
    if page:
        return page
    if not url:
        return None
    page_domain: list[Any] = [("url", "=", url)]
    if "website_id" in env["website.page"]._fields:
        page_domain = ["&", ("url", "=", url), "|", ("website_id", "=", False), ("website_id", "=", website.id)]
    return env["website.page"].sudo().search(page_domain, order="website_id desc,id", limit=1)


def _verify_route(env: Any, website: Any, route_payload: dict[str, object], *, fallback_module: str) -> Any | None:
    route_url = str(route_payload.get("url") or "").strip()
    if not route_url:
        return None
    module_name = str(route_payload.get("module") or fallback_module or "").strip()
    page = _find_website_page(env, website, xmlid="", url=route_url)
    if page:
        if bool(route_payload.get("published", True)):
            _write_existing_fields(page, {"is_published": True, "website_published": True})
        return page
    if module_name:
        if not _module_is_installed(env, module_name):
            raise RuntimeError(f"Website bootstrap route {route_url!r} requires module {module_name!r}, but it is not installed.")
        print(f"Website bootstrap route {route_url} is delegated to installed module {module_name}.")
        return None
    match = getattr(env["ir.http"].sudo(), "_match", None)
    if callable(match):
        try:
            match(route_url)
            return None
        except Exception as error:
            raise RuntimeError(f"Website bootstrap route {route_url!r} is not routable.") from error
    print(f"Website bootstrap route verification skipped for {route_url}: ir.http._match unavailable.")
    return None


def apply_website_bootstrap(env: Any, parsed_payload: dict[str, object] | None) -> None:
    if not parsed_payload:
        return
    website_payload = parsed_payload.get("website_bootstrap")
    if not isinstance(website_payload, dict) or not website_payload:
        return
    if "website" not in env.registry:
        raise RuntimeError("Website bootstrap supplied, but the website module is not installed.")

    website_model = env["website"].sudo()
    website = website_model.search([], order="id", limit=1)
    if not website:
        default_name = str(website_payload.get("name") or "Website").strip() or "Website"
        create_values = _field_values(website_model, {"name": default_name})
        website = website_model.create(create_values or {"name": default_name})

    website_values: dict[str, object] = {}
    website_name = str(website_payload.get("name") or "").strip()
    if website_name:
        website_values["name"] = website_name
    canonical_url = str(website_payload.get("canonical_url") or "").strip()
    if canonical_url:
        env["ir.config_parameter"].sudo().set_param("web.base.url", canonical_url)
        env["ir.config_parameter"].sudo().set_param("web.base.url.freeze", "True")
        website_values["domain"] = canonical_url
    default_lang = str(website_payload.get("default_lang") or "").strip()
    if default_lang and "default_lang_id" in website._fields:
        lang = env["res.lang"].sudo().search([("code", "=", default_lang)], limit=1)
        if lang:
            website_values["default_lang_id"] = lang.id
    logo_path = _resolve_bootstrap_logo_path(website_payload.get("logo_path"))
    if logo_path is not None and "logo" in website._fields:
        website_values["logo"] = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    _write_existing_fields(website, website_values)

    homepage_url = str(website_payload.get("homepage_url") or "").strip()
    primary_page_xmlid = str(website_payload.get("primary_page_xmlid") or "").strip()
    homepage_page = _find_website_page(env, website, xmlid=primary_page_xmlid, url=homepage_url)
    if homepage_page:
        page_values: dict[str, object] = {"is_published": True, "website_published": True}
        if "website_id" in homepage_page._fields:
            page_values["website_id"] = website.id
        _write_existing_fields(homepage_page, page_values)
        _write_existing_fields(website, {"homepage_id": homepage_page.id})
    elif primary_page_xmlid:
        raise RuntimeError(f"Website bootstrap primary page XML ID not found: {primary_page_xmlid}")

    raw_routes_source = website_payload.get("routes_source")
    routes_source = raw_routes_source if isinstance(raw_routes_source, dict) else {}
    fallback_module = str(routes_source.get("module") or "").strip()
    if homepage_url and not homepage_page:
        _verify_route(
            env, website, {"url": homepage_url, "module": fallback_module, "published": True}, fallback_module=fallback_module
        )
    for route_payload in website_payload.get("routes") or []:
        if isinstance(route_payload, dict):
            route_page = _verify_route(env, website, route_payload, fallback_module=fallback_module)
            if route_page and bool(route_payload.get("homepage")):
                _write_existing_fields(website, {"homepage_id": route_page.id})

    print("website_bootstrap_applied=true")
