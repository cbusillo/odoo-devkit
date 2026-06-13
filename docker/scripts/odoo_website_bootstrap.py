import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

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


def _normalize_scalar_override_value(raw_value: object) -> str:
    if isinstance(raw_value, bool):
        return "True" if raw_value else "False"
    return str(raw_value).strip()


def _payload_web_base_url(parsed_payload: dict[str, object] | None) -> str:
    if not parsed_payload:
        return ""
    raw_parameters = parsed_payload.get("config_parameters")
    if raw_parameters is None:
        return ""
    if not isinstance(raw_parameters, list):
        raise RuntimeError("Odoo instance override payload field 'config_parameters' must be a list.")
    for raw_parameter in raw_parameters:
        if not isinstance(raw_parameter, dict):
            raise RuntimeError("Odoo config parameter override payload entries must be objects.")
        key = str(raw_parameter.get("key") or "").strip().lower()
        if not key:
            raise RuntimeError("Odoo config parameter override payload entries require key.")
        if key != "web.base.url":
            continue
        raw_value_payload = raw_parameter.get("value")
        if not isinstance(raw_value_payload, dict):
            raise RuntimeError(f"Odoo config parameter override '{key}' has an invalid value payload.")
        source = str(raw_value_payload.get("source") or "").strip()
        if source == "literal":
            if "value" not in raw_value_payload:
                raise RuntimeError(f"Odoo config parameter override '{key}' is missing a literal value.")
            return _normalize_scalar_override_value(raw_value_payload.get("value"))
        if source == "secret_binding":
            environment_variable = str(raw_value_payload.get("environment_variable") or "").strip()
            if not environment_variable:
                raise RuntimeError(
                    f"Secret-backed Odoo config parameter override '{key}' is missing its runtime environment variable."
                )
            if environment_variable not in os.environ:
                raise RuntimeError(
                    f"Secret-backed Odoo config parameter override '{key}' is missing environment variable {environment_variable}."
                )
            return os.environ.get(environment_variable, "")
        raise RuntimeError(f"Odoo config parameter override '{key}' has unsupported source '{source}'.")
    return ""


def _field_values(record: Any, values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if key in record._fields}


def _write_existing_fields(record: Any, values: dict[str, object]) -> None:
    filtered_values = _field_values(record, values)
    if filtered_values:
        record.sudo().write(filtered_values)


def _require_existing_fields(record: Any, field_names: tuple[str, ...], *, label: str) -> None:
    missing_fields = [field_name for field_name in field_names if field_name not in record._fields]
    if missing_fields:
        formatted_fields = ", ".join(sorted(missing_fields))
        raise RuntimeError(f"Website bootstrap cannot apply {label}; missing fields: {formatted_fields}.")


def _field_value(record: Any, field_name: str) -> object:
    return getattr(record, field_name, None)


def _record_id(record: Any) -> object:
    return getattr(record, "id", record) if record else None


def _binary_field_value(record: Any, field_name: str) -> str:
    value = _field_value(record, field_name)
    if isinstance(value, bytes):
        return value.decode("ascii")
    return "" if value is None else str(value)


def _assert_field_value(record: Any, field_name: str, expected_value: object, *, label: str) -> None:
    if field_name not in record._fields:
        raise RuntimeError(f"Website bootstrap cannot verify {label}; missing field: {field_name}.")
    actual_value = _field_value(record, field_name)
    if actual_value != expected_value:
        raise RuntimeError(f"Website bootstrap failed to persist {label}: expected {expected_value!r}, got {actual_value!r}.")


def _assert_binary_field_value(record: Any, field_name: str, expected_value: str, *, label: str) -> None:
    if field_name not in record._fields:
        raise RuntimeError(f"Website bootstrap cannot verify {label}; missing field: {field_name}.")
    actual_value = _binary_field_value(record, field_name)
    if actual_value != expected_value:
        raise RuntimeError(
            f"Website bootstrap failed to persist {label}: expected {len(expected_value)} encoded bytes, got {len(actual_value)}."
        )


def _assert_field_record_id(record: Any, field_name: str, expected_record: Any, *, label: str) -> None:
    if field_name not in record._fields:
        raise RuntimeError(f"Website bootstrap cannot verify {label}; missing field: {field_name}.")
    expected_id = _record_id(expected_record)
    actual_id = _record_id(_field_value(record, field_name))
    if actual_id != expected_id:
        raise RuntimeError(f"Website bootstrap failed to persist {label}: expected record {expected_id!r}, got {actual_id!r}.")


def _field_record_matches(record: Any, field_name: str, expected_record: Any) -> bool:
    if not record or field_name not in record._fields:
        return False
    return _record_id(_field_value(record, field_name)) == _record_id(expected_record)


def _config_parameter_value(env: Any, key: str) -> str:
    parameter_model = env["ir.config_parameter"].sudo()
    get_param = getattr(parameter_model, "get_param", None)
    if not callable(get_param):
        raise RuntimeError(f"Website bootstrap cannot verify config parameter {key!r}; get_param is unavailable.")
    value = get_param(key)
    return "" if value is None else str(value)


def _set_config_parameter(env: Any, key: str, value: str) -> None:
    env["ir.config_parameter"].sudo().set_param(key, value)
    actual_value = _config_parameter_value(env, key)
    if actual_value != value:
        raise RuntimeError(
            f"Website bootstrap failed to persist config parameter {key!r}: expected {value!r}, got {actual_value!r}."
        )


def _canonical_host(canonical_url: str) -> str:
    if not canonical_url:
        return ""
    return urlparse(canonical_url).netloc or canonical_url


def _marker_bool(value: bool) -> str:
    return "true" if value else "false"


def _print_bootstrap_readback(
    *,
    env: Any,
    website: Any,
    canonical_url: str,
    homepage_url: str,
    homepage_page: Any | None,
    primary_page_xmlid: str,
    primary_page_xmlid_found: bool,
    logo_expected: bool,
    page_website_bound_count: int,
    view_website_bound_count: int,
) -> None:
    canonical_host = _canonical_host(canonical_url)
    website_domain = str(_field_value(website, "domain") or "") if "domain" in website._fields else ""
    actual_homepage_url = str(_field_value(website, "homepage_url") or "") if "homepage_url" in website._fields else ""
    actual_homepage = _field_value(website, "homepage_id") if "homepage_id" in website._fields else None
    actual_homepage_id = _record_id(actual_homepage)
    homepage_page_id = _record_id(homepage_page)
    homepage_view = _field_value(homepage_page, "view_id") if homepage_page and "view_id" in homepage_page._fields else None
    logo_present = bool(_field_value(website, "logo")) if "logo" in website._fields else False
    web_base_url_matches = False
    if canonical_url:
        web_base_url_matches = _config_parameter_value(env, "web.base.url") == canonical_url

    print(f"website_bootstrap_website_id={getattr(website, 'id', '')}")
    print(f"website_bootstrap_domain_set={_marker_bool(bool(website_domain))}")
    print(f"website_bootstrap_domain_matches_canonical={_marker_bool(bool(canonical_host) and website_domain == canonical_host)}")
    print(f"website_bootstrap_web_base_url_matches={_marker_bool(not canonical_url or web_base_url_matches)}")
    print(f"website_bootstrap_homepage_url_set={_marker_bool(bool(actual_homepage_url))}")
    print(f"website_bootstrap_homepage_url_matches={_marker_bool(bool(homepage_url) and actual_homepage_url == homepage_url)}")
    print(f"website_bootstrap_homepage_page_found={_marker_bool(bool(homepage_page_id))}")
    print(f"website_bootstrap_primary_page_xmlid_found={_marker_bool(bool(primary_page_xmlid) and primary_page_xmlid_found)}")
    print(
        f"website_bootstrap_homepage_matches_page={_marker_bool(bool(homepage_page_id) and actual_homepage_id == homepage_page_id)}"
    )
    print(
        f"website_bootstrap_homepage_page_website_matches={_marker_bool(_field_record_matches(homepage_page, 'website_id', website))}"
    )
    print(
        f"website_bootstrap_homepage_view_website_matches={_marker_bool(_field_record_matches(homepage_view, 'website_id', website))}"
    )
    print(f"website_bootstrap_page_website_bound_count={page_website_bound_count}")
    print(f"website_bootstrap_view_website_bound_count={view_website_bound_count}")
    print(f"website_bootstrap_logo_present={_marker_bool(not logo_expected or logo_present)}")


def _select_website(
    website_model: Any,
    *,
    canonical_url: str,
    primary_page: Any | None = None,
    default_website: Any | None = None,
) -> Any:
    if primary_page and "website_id" in primary_page._fields:
        page_website = cast(Any, _field_value(primary_page, "website_id"))
        if page_website:
            return page_website.sudo()
    canonical_host = _canonical_host(canonical_url)
    if canonical_host and "domain" in website_model._fields:
        website = website_model.search([("domain", "in", (canonical_host, canonical_url))], order="id", limit=1)
        if website:
            return website
    if default_website:
        return default_website.sudo()
    return website_model.search([], order="id", limit=1)


def _clear_duplicate_canonical_domains(website_model: Any, *, website: Any, canonical_url: str) -> None:
    canonical_host = _canonical_host(canonical_url)
    if not canonical_host or "domain" not in website_model._fields:
        return
    duplicates = website_model.search(
        [
            ("id", "!=", website.id),
            ("domain", "in", (canonical_host, canonical_url)),
        ],
        order="id",
    )
    if duplicates:
        duplicates.sudo().write({"domain": ""})


def _bind_page_to_website(page: Any, website: Any, *, published: bool) -> tuple[bool, bool]:
    if not page:
        return False, False
    page_values: dict[str, object] = {}
    if published:
        page_values.update({"is_published": True, "website_published": True})
    page_bound = False
    if "website_id" in page._fields:
        page_values["website_id"] = website.id
        page_bound = True
    _write_existing_fields(page, page_values)
    if page_bound:
        _assert_field_record_id(page, "website_id", website, label="page website")

    view = cast(Any, _field_value(page, "view_id")) if "view_id" in page._fields else None
    view_bound = False
    if view and "website_id" in view._fields:
        _write_existing_fields(view, {"website_id": website.id})
        _assert_field_record_id(view, "website_id", website, label="page view website")
        view_bound = True
    return page_bound, view_bound


def _homepage_values(website: Any, *, homepage_url: str, homepage_page: Any | None) -> dict[str, object]:
    values: dict[str, object] = {}
    if homepage_page and "homepage_id" in website._fields:
        values["homepage_id"] = homepage_page.id
    if homepage_url and "homepage_url" in website._fields:
        values["homepage_url"] = homepage_url
    if homepage_url and homepage_page is None and "homepage_id" in website._fields:
        values["homepage_id"] = False
    return values


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


def _find_website_page_by_xmlid(env: Any, *, xmlid: str) -> Any | None:
    if xmlid:
        candidate = env.ref(xmlid, raise_if_not_found=False)
        if candidate and candidate._name == "website.page":
            return candidate.sudo()
    return None


def _find_website_page_by_url(env: Any, website: Any, *, url: str) -> Any | None:
    if not url:
        return None
    page_model = env["website.page"].sudo()
    page_domain: list[Any] = [("url", "=", url)]
    if "website_id" in page_model._fields:
        page_domain = ["&", ("url", "=", url), "|", ("website_id", "=", False), ("website_id", "=", website.id)]
        scoped_page = page_model.search(page_domain, order="website_id desc,id", limit=1)
        if scoped_page:
            return scoped_page
        # A previous partial bootstrap can leave the requested URL bound to a
        # stale website. Reclaim the exact page so the selected website owns the
        # public route instead of delegating it back to the stale binding.
        return page_model.search([("url", "=", url)], order="id", limit=1)
    return page_model.search(page_domain, order="id", limit=1)


def _find_website_page(env: Any, website: Any, *, xmlid: str, url: str) -> tuple[Any | None, bool]:
    page = _find_website_page_by_xmlid(env, xmlid=xmlid)
    if page:
        return page, True
    if xmlid:
        return None, False
    return _find_website_page_by_url(env, website, url=url), False


def _verify_route(env: Any, website: Any, route_payload: dict[str, object], *, fallback_module: str) -> Any | None:
    route_url = str(route_payload.get("url") or "").strip()
    if not route_url:
        return None
    module_name = str(route_payload.get("module") or fallback_module or "").strip()
    page = _find_website_page_by_url(env, website, url=route_url)
    if page:
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
    raise RuntimeError(f"Website bootstrap route {route_url!r} is not verifiable; ir.http._match is unavailable.")


def apply_website_bootstrap(env: Any, parsed_payload: dict[str, object] | None) -> None:
    if not parsed_payload:
        return
    website_payload = parsed_payload.get("website_bootstrap")
    if not isinstance(website_payload, dict) or not website_payload:
        return
    if "website" not in env.registry:
        raise RuntimeError("Website bootstrap supplied, but the website module is not installed.")

    canonical_url = str(website_payload.get("canonical_url") or _payload_web_base_url(parsed_payload) or "").strip()

    homepage_url = str(website_payload.get("homepage_url") or "").strip()
    primary_page_xmlid = str(website_payload.get("primary_page_xmlid") or "").strip()
    primary_page = _find_website_page_by_xmlid(env, xmlid=primary_page_xmlid)
    primary_page_xmlid_found = bool(primary_page)
    if primary_page_xmlid and not primary_page:
        raise RuntimeError(f"Website bootstrap primary page XML ID not found: {primary_page_xmlid}")
    default_website = env.ref("website.default_website", raise_if_not_found=False)

    website_model = env["website"].sudo()
    website = _select_website(
        website_model,
        canonical_url=canonical_url,
        primary_page=primary_page,
        default_website=default_website,
    )
    if not website:
        default_name = str(website_payload.get("name") or "Website").strip() or "Website"
        create_values = _field_values(website_model, {"name": default_name})
        website = website_model.create(create_values or {"name": default_name})

    website_values: dict[str, object] = {}
    website_name = str(website_payload.get("name") or "").strip()
    if website_name:
        _require_existing_fields(website, ("name",), label="website name")
        website_values["name"] = website_name
    if canonical_url:
        _require_existing_fields(website, ("domain",), label="canonical domain")
        _set_config_parameter(env, "web.base.url", canonical_url)
        _set_config_parameter(env, "web.base.url.freeze", "True")
        _clear_duplicate_canonical_domains(website_model, website=website, canonical_url=canonical_url)
        website_values["domain"] = _canonical_host(canonical_url)
        if "sequence" in website._fields:
            website_values["sequence"] = 0
    default_lang = str(website_payload.get("default_lang") or "").strip()
    if default_lang and "default_lang_id" in website._fields:
        lang = env["res.lang"].sudo().search([("code", "=", default_lang)], limit=1)
        if lang:
            website_values["default_lang_id"] = lang.id
    logo_path = _resolve_bootstrap_logo_path(website_payload.get("logo_path"))
    logo_expected = logo_path is not None
    logo_value = ""
    if logo_path is not None:
        _require_existing_fields(website, ("logo",), label="website logo")
        logo_value = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        website_values["logo"] = logo_value
    _write_existing_fields(website, website_values)
    if website_name:
        _assert_field_value(website, "name", website_name, label="website name")
    if canonical_url:
        _assert_field_value(website, "domain", _canonical_host(canonical_url), label="canonical domain")
    if logo_expected:
        _assert_binary_field_value(website, "logo", logo_value, label="website logo")

    homepage_page = primary_page or _find_website_page_by_url(env, website, url=homepage_url)
    page_website_bound_count = 0
    view_website_bound_count = 0
    if homepage_page:
        page_bound, view_bound = _bind_page_to_website(homepage_page, website, published=True)
        page_website_bound_count += int(page_bound)
        view_website_bound_count += int(view_bound)
    _write_existing_fields(website, _homepage_values(website, homepage_url=homepage_url, homepage_page=homepage_page))
    final_homepage_url = homepage_url
    final_homepage_page = homepage_page

    raw_routes_source = website_payload.get("routes_source")
    routes_source = raw_routes_source if isinstance(raw_routes_source, dict) else {}
    fallback_module = str(routes_source.get("module") or "").strip()
    if homepage_url and not homepage_page:
        route_page = _verify_route(
            env, website, {"url": homepage_url, "module": fallback_module, "published": True}, fallback_module=fallback_module
        )
        page_bound, view_bound = _bind_page_to_website(route_page, website, published=True)
        page_website_bound_count += int(page_bound)
        view_website_bound_count += int(view_bound)
        _write_existing_fields(website, _homepage_values(website, homepage_url=homepage_url, homepage_page=route_page))
        final_homepage_page = route_page
    for route_payload in website_payload.get("routes") or []:
        if isinstance(route_payload, dict):
            route_page = _verify_route(env, website, route_payload, fallback_module=fallback_module)
            page_bound, view_bound = _bind_page_to_website(route_page, website, published=bool(route_payload.get("published", True)))
            page_website_bound_count += int(page_bound)
            view_website_bound_count += int(view_bound)
            if bool(route_payload.get("homepage")):
                route_url = str(route_payload.get("url") or "").strip()
                _write_existing_fields(website, _homepage_values(website, homepage_url=route_url, homepage_page=route_page))
                final_homepage_url = route_url
                final_homepage_page = route_page

    if final_homepage_url and "homepage_url" in website._fields:
        _assert_field_value(website, "homepage_url", final_homepage_url, label="homepage URL")
    if final_homepage_page and "homepage_id" in website._fields:
        _assert_field_record_id(website, "homepage_id", final_homepage_page, label="homepage page")

    _print_bootstrap_readback(
        env=env,
        website=website,
        canonical_url=canonical_url,
        homepage_url=final_homepage_url,
        homepage_page=final_homepage_page,
        primary_page_xmlid=primary_page_xmlid,
        primary_page_xmlid_found=primary_page_xmlid_found,
        logo_expected=logo_expected,
        page_website_bound_count=page_website_bound_count,
        view_website_bound_count=view_website_bound_count,
    )
    print("website_bootstrap_applied=true")
