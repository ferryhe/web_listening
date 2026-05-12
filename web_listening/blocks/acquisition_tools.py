from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from web_listening.blocks.acquisition_capture import build_builtin_adapters, run_capture_attempt
from web_listening.blocks.acquisition_profile import (
    AdapterId,
    ALLOWED_ADAPTER_IDS,
    AcquisitionProfile,
    build_default_acquisition_profile,
    load_acquisition_profile,
)


CATALOG_CONTRACT_VERSION = "acquisition-tools.v1"
PROFILE_BUILD_CONTRACT_VERSION = "acquisition-profile-build.v1"
PROBE_CONTRACT_VERSION = "acquisition-probe.v1"


class AcquisitionToolError(ValueError):
    """Raised when an acquisition tool request is invalid."""


def acquisition_tools_catalog() -> dict[str, Any]:
    tools = [
        {
            "adapter": "web_http",
            "category": "http",
            "purpose": "Fetch public HTML with the built-in HTTP crawler.",
            "built_in_now": True,
            "implemented_for_pr3_probing": True,
            "probe_capable": True,
            "safety_notes": [
                "Use only for public http/https URLs.",
                "Honors the configured user agent and HTTP timeout.",
            ],
        },
        {
            "adapter": "browser_rendered",
            "category": "browser",
            "purpose": "Fetch public pages that need client-side rendering.",
            "built_in_now": True,
            "implemented_for_pr3_probing": True,
            "probe_capable": True,
            "safety_notes": [
                "Use for authorized public-page rendering only.",
                "Requires optional browser runtime support to be installed.",
            ],
        },
        {
            "adapter": "sitemap",
            "category": "discovery",
            "purpose": "Reserved contract for sitemap-based acquisition planning.",
            "built_in_now": False,
            "implemented_for_pr3_probing": False,
            "probe_capable": False,
            "safety_notes": [
                "List-only in this build; do not execute as a probe adapter.",
            ],
        },
        {
            "adapter": "rss",
            "category": "feed",
            "purpose": "Reserved contract for RSS/feed acquisition planning.",
            "built_in_now": False,
            "implemented_for_pr3_probing": False,
            "probe_capable": False,
            "safety_notes": [
                "List-only in this build; do not execute as a probe adapter.",
            ],
        },
        {
            "adapter": "cloakbrowser",
            "category": "authorized_browser",
            "purpose": "Probe authorized pages with optional CloakBrowser stealth-browser runtime.",
            "built_in_now": True,
            "implemented_for_pr3_probing": True,
            "probe_capable": True,
            "optional_runtime": {
                "extra": "cloakbrowser",
                "package": "cloakbrowser>=0.3.26",
                "first_launch_download": "CloakBrowser may download a browser binary on first launch.",
            },
            "safety_notes": [
                "Requires safety.allow_stealth_browser=true and safety.require_authorized_access=true in the active acquisition profile.",
                "Use only where the operator has explicit authorization for the target site and access context.",
                "Optional runtime is not installed by the core package.",
            ],
        },
        {
            "adapter": "batch_python",
            "category": "site_specific_batch",
            "purpose": "Reserved contract for reviewed site-specific batch acquisition scripts.",
            "built_in_now": False,
            "implemented_for_pr3_probing": False,
            "probe_capable": False,
            "safety_notes": [
                "Requires reviewed code and explicit operator approval before future use.",
                "List-only in this build; do not execute as a probe adapter.",
            ],
        },
    ]
    return {
        "contract_version": CATALOG_CONTRACT_VERSION,
        "catalog_version": "pr5-2026-05-12",
        "tools": tools,
    }


def build_default_acquisition_profile_payload(
    *,
    site_key: str,
    allowed_domains: list[str] | None = None,
    allow_stealth_browser: bool = False,
    require_authorized_access: bool = False,
    output_path: str = "",
) -> dict[str, Any]:
    profile = build_default_acquisition_profile(
        site_key=site_key,
        allowed_domains=allowed_domains or [],
        allow_stealth_browser=allow_stealth_browser,
        require_authorized_access=require_authorized_access,
    )
    return {
        "contract_version": PROFILE_BUILD_CONTRACT_VERSION,
        "profile": profile.model_dump(mode="json"),
        "output_path": output_path,
    }


def validate_probe_adapter(adapter_id: str) -> AdapterId:
    if adapter_id not in ALLOWED_ADAPTER_IDS:
        supported = ", ".join(sorted(ALLOWED_ADAPTER_IDS))
        raise AcquisitionToolError(f"Unsupported acquisition adapter `{adapter_id}`. Supported adapters: {supported}.")
    tool = _catalog_tool(adapter_id)
    if not tool["probe_capable"]:
        raise AcquisitionToolError(f"Acquisition adapter `{adapter_id}` is not probe-capable in this build.")
    return adapter_id  # type: ignore[return-value]


def probe_acquisition_url(
    *,
    url: str,
    site_key: str | None = None,
    adapter_id: str = "web_http",
    profile_path: str | Path | None = None,
    allowed_domains: list[str] | None = None,
    allow_stealth_browser: bool = False,
    require_authorized_access: bool = False,
) -> dict[str, Any]:
    normalized_url = validate_http_url(url)
    adapter = validate_probe_adapter(adapter_id)
    profile = _load_or_build_profile(
        site_key=site_key,
        profile_path=profile_path,
        allowed_domains=allowed_domains if allowed_domains is not None else [_url_host(normalized_url)],
        allow_stealth_browser=allow_stealth_browser,
        require_authorized_access=require_authorized_access,
    )
    _validate_adapter_profile_safety(adapter, profile)
    adapters = build_builtin_adapters()
    try:
        selected = adapters.get(adapter)
        if selected is None:
            raise AcquisitionToolError(f"Acquisition adapter `{adapter}` has no built-in probe implementation.")

        attempt = run_capture_attempt(normalized_url, selected, profile)
        return {
            "contract_version": PROBE_CONTRACT_VERSION,
            "profile": profile.model_dump(mode="json"),
            "attempt": attempt.model_dump(mode="json"),
            "available_tools": acquisition_tools_catalog(),
            "next_action": _next_action(attempt.status, attempt.recommended_next_adapter),
        }
    finally:
        _close_adapters(adapters)


def validate_http_url(url: str) -> str:
    normalized = (url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise AcquisitionToolError("url must be a valid http or https URL")
    if parsed.username or parsed.password:
        raise AcquisitionToolError("url must not include embedded credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise AcquisitionToolError("url port must be valid") from exc
    _reject_private_probe_host(parsed.hostname)
    return normalized


def _load_or_build_profile(
    *,
    site_key: str | None,
    profile_path: str | Path | None,
    allowed_domains: list[str],
    allow_stealth_browser: bool,
    require_authorized_access: bool,
) -> AcquisitionProfile:
    if profile_path:
        return load_acquisition_profile(profile_path)
    normalized_site_key = (site_key or "").strip()
    if not normalized_site_key:
        raise AcquisitionToolError("site_key is required when profile_path is not provided")
    return build_default_acquisition_profile(
        site_key=normalized_site_key,
        allowed_domains=allowed_domains,
        allow_stealth_browser=allow_stealth_browser,
        require_authorized_access=require_authorized_access,
    )


def _catalog_tool(adapter_id: str) -> dict[str, Any]:
    for tool in acquisition_tools_catalog()["tools"]:
        if tool["adapter"] == adapter_id:
            return tool
    raise AcquisitionToolError(f"Unsupported acquisition adapter `{adapter_id}`.")


def _validate_adapter_profile_safety(adapter_id: AdapterId, profile: AcquisitionProfile) -> None:
    if adapter_id != "cloakbrowser":
        return
    if not profile.safety.permits_cloakbrowser:
        raise AcquisitionToolError(
            "Acquisition adapter `cloakbrowser` requires safety.allow_stealth_browser=true "
            "and safety.require_authorized_access=true in the active acquisition profile."
        )
    cloakbrowser_config = next(
        (adapter for adapter in profile.adapters if adapter.adapter == "cloakbrowser"),
        None,
    )
    if cloakbrowser_config is not None and not cloakbrowser_config.enabled:
        raise AcquisitionToolError("Acquisition adapter `cloakbrowser` is disabled in the active acquisition profile.")


def _close_adapters(adapters: dict[str, Any]) -> None:
    seen: set[int] = set()
    for adapter in adapters.values():
        adapter_id = id(adapter)
        if adapter_id in seen:
            continue
        seen.add(adapter_id)
        close_target = getattr(adapter, "close", None)
        if not callable(close_target):
            close_target = getattr(getattr(adapter, "crawler", None), "close", None)
        if callable(close_target):
            try:
                close_target()
            except Exception:
                pass


def _url_host(url: str) -> str:
    return urlparse(url).hostname or ""


def _reject_private_probe_host(hostname: str) -> None:
    normalized = hostname.strip().casefold().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost") or normalized.endswith(".local"):
        raise AcquisitionToolError("url host must not be localhost or a local-only hostname")
    try:
        address = ip_address(normalized)
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise AcquisitionToolError("url host must not be a private, loopback, link-local, or reserved IP address")


def _next_action(status: str, recommended_next_adapter: str) -> str:
    if status == "passed":
        return "use_adapter_output"
    if recommended_next_adapter:
        return f"try_adapter:{recommended_next_adapter}"
    return "review_probe_failure"
