from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AdapterId = Literal[
    "web_http",
    "browser_rendered",
    "sitemap",
    "rss",
    "cloakbrowser",
    "browseract",
    "batch_python",
]
AdapterRecommendation = AdapterId | Literal[""]

ALLOWED_ADAPTER_IDS: tuple[str, ...] = (
    "web_http",
    "browser_rendered",
    "sitemap",
    "rss",
    "cloakbrowser",
    "browseract",
    "batch_python",
)
ALLOWED_DOMAINS_ERROR = "allowed_domains must be a list of non-empty strings or a single string"


class AcquisitionAdapterConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    adapter: AdapterId
    enabled: bool = True
    reason: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)


class AcquisitionQualityGates(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", hide_input_in_errors=True)

    min_words: int = 120
    min_links: int = 3
    min_document_links: int = 0
    require_status_ok: bool = True
    blocked_markers: list[str] = Field(
        default_factory=lambda: [
            "access denied",
            "captcha",
            "cloudflare",
            "enable javascript",
            "forbidden",
        ]
    )

    @field_validator("min_words", "min_links", "min_document_links", mode="before")
    @classmethod
    def validate_exact_counts(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("quality gate counts must be integers without coercion")
        return value

    @field_validator("require_status_ok", mode="before")
    @classmethod
    def validate_exact_status_policy(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("require_status_ok must be a boolean without coercion")
        return value


class AcquisitionSafetyPolicy(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", hide_input_in_errors=True)

    allowed_domains: list[str] = Field(default_factory=list)
    allow_stealth_browser: bool = False
    require_authorized_access: bool = False

    @field_validator("allow_stealth_browser", "require_authorized_access", mode="before")
    @classmethod
    def validate_exact_authorization_flags(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("safety authorization flags must be booleans without coercion")
        return value

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def parse_allowed_domains(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, bytes | bytearray):
            raise ValueError(ALLOWED_DOMAINS_ERROR)
        elif isinstance(value, Sequence):
            values = list(value)
        else:
            raise ValueError(ALLOWED_DOMAINS_ERROR)
        if any(not isinstance(item, str) for item in values):
            raise ValueError(ALLOWED_DOMAINS_ERROR)
        domains = [item.strip() for item in values]
        if any(not domain for domain in domains):
            raise ValueError(ALLOWED_DOMAINS_ERROR)
        return domains

    @field_validator("allowed_domains")
    @classmethod
    def validate_allowed_domains(cls, value: list[str]) -> list[str]:
        if any(not str(domain).strip() for domain in value):
            raise ValueError(ALLOWED_DOMAINS_ERROR)
        return value

    @property
    def permits_cloakbrowser(self) -> bool:
        return self.allow_stealth_browser and self.require_authorized_access

    @property
    def permits_browseract(self) -> bool:
        return self.allow_stealth_browser and self.require_authorized_access


class AcquisitionRecipeMapping(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    adapter: AdapterId
    recipe_id: str = Field(min_length=1)


class AcquisitionResourceLimits(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    timeout_seconds: float | None = None
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def strict_timeout(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError("timeout_seconds must be a number without coercion")
        return value

    @field_validator("stdout_bytes", "stderr_bytes", mode="before")
    @classmethod
    def strict_bytes(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("byte limits must be integers without coercion")
        return value

    @model_validator(mode="after")
    def validate_bounded_limits(self) -> AcquisitionResourceLimits:
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0 or self.timeout_seconds > 300
        ):
            raise ValueError("timeout_seconds must be finite and in (0, 300]")
        for name, value, ceiling in (
            ("stdout_bytes", self.stdout_bytes, 16 * 1024 * 1024),
            ("stderr_bytes", self.stderr_bytes, 1024 * 1024),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > ceiling):
                raise ValueError(f"{name} must be a positive integer no greater than {ceiling}")
        return self


class AcquisitionProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    schema_version: Literal["acquisition-profile.v1"] = "acquisition-profile.v1"
    profile_id: str
    site_key: str
    generated_at: str
    strategy: str = "http-first-with-open-fallbacks"
    default_adapter: AdapterId = "web_http"
    fallback_order: list[AdapterId] = Field(default_factory=list)
    quality_gates: AcquisitionQualityGates = Field(default_factory=AcquisitionQualityGates)
    safety: AcquisitionSafetyPolicy = Field(default_factory=AcquisitionSafetyPolicy)
    adapters: list[AcquisitionAdapterConfig] = Field(default_factory=list)
    recipe_mappings: list[AcquisitionRecipeMapping] = Field(default_factory=list)
    resource_limits: AcquisitionResourceLimits = Field(default_factory=AcquisitionResourceLimits)
    adapter_resource_limits: dict[AdapterId, AcquisitionResourceLimits] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stealth_browser_safety(self) -> AcquisitionProfile:
        cloakbrowser_is_default = self.default_adapter == "cloakbrowser"
        cloakbrowser_in_fallback = "cloakbrowser" in self.fallback_order
        cloakbrowser_enabled = any(
            adapter.adapter == "cloakbrowser" and adapter.enabled
            for adapter in self.adapters
        )
        if (
            cloakbrowser_is_default
            or cloakbrowser_in_fallback
            or cloakbrowser_enabled
        ) and not self.safety.permits_cloakbrowser:
            raise ValueError(
                "cloakbrowser requires safety.allow_stealth_browser=true "
                "and safety.require_authorized_access=true"
            )
        browseract_is_default = self.default_adapter == "browseract"
        browseract_in_fallback = "browseract" in self.fallback_order
        browseract_enabled = any(
            adapter.adapter == "browseract" and adapter.enabled
            for adapter in self.adapters
        )
        if (
            browseract_is_default
            or browseract_in_fallback
            or browseract_enabled
        ) and not self.safety.permits_browseract:
            raise ValueError(
                "browseract requires safety.allow_stealth_browser=true "
                "and safety.require_authorized_access=true"
            )
        adapter_ids = [item.adapter for item in self.adapters]
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("adapters must have unique adapter values")
        mapped = [item.adapter for item in self.recipe_mappings]
        if len(mapped) != len(set(mapped)):
            raise ValueError("recipe_mappings must have unique adapter values")
        return self


class CaptureAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    schema_version: Literal["capture-attempt.v1"] = "capture-attempt.v1"
    adapter: AdapterId
    status: str
    url: str
    final_url: str = ""
    status_code: int | None = None
    word_count: int = 0
    link_count: int = 0
    document_link_count: int = 0
    failure_reason: str = ""
    recommended_next_adapter: AdapterRecommendation = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_default_acquisition_profile(
    site_key: str,
    allowed_domains: list[str] | None = None,
    allow_stealth_browser: bool = False,
    require_authorized_access: bool = False,
) -> AcquisitionProfile:
    safety = AcquisitionSafetyPolicy(
        allowed_domains=allowed_domains or [],
        allow_stealth_browser=allow_stealth_browser,
        require_authorized_access=require_authorized_access,
    )
    fallback_order: list[AdapterId] = [
        "browser_rendered",
        "sitemap",
        "rss",
        "batch_python",
    ]
    adapters = [
        AcquisitionAdapterConfig(
            adapter="web_http",
            reason="Default low-cost HTTP capture for public pages.",
        ),
        AcquisitionAdapterConfig(
            adapter="browser_rendered",
            reason="Fallback for pages that need client-side rendering.",
        ),
        AcquisitionAdapterConfig(
            adapter="sitemap",
            reason="Fallback discovery path for sites with useful sitemap XML.",
        ),
        AcquisitionAdapterConfig(
            adapter="rss",
            reason="Fallback discovery path for sites exposing feeds.",
        ),
        AcquisitionAdapterConfig(
            adapter="batch_python",
            reason="Reserved for explicit site-specific batch acquisition scripts.",
        ),
        AcquisitionAdapterConfig(
            adapter="browseract",
            enabled=False,
            reason="Optional isolated BrowserAct read-only recipes; explicit enablement only.",
            safety={"requires_authorized_access": True, "read_only": True},
        ),
    ]
    if safety.permits_cloakbrowser:
        fallback_order.append("cloakbrowser")
        adapters.append(
            AcquisitionAdapterConfig(
                adapter="cloakbrowser",
                reason="Authorized stealth-browser fallback for confirmed access contexts.",
                safety={"requires_authorized_access": True},
            )
        )
    return AcquisitionProfile(
        profile_id=f"{site_key}-acquisition-profile",
        site_key=site_key,
        generated_at=_utc_timestamp(),
        default_adapter="web_http",
        fallback_order=fallback_order,
        safety=safety,
        adapters=adapters,
        notes=[
            "Control artifact only; crawler and staged workflow integration are future work.",
        ],
    )


def load_acquisition_profile(path: str | Path) -> AcquisitionProfile:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise ValueError("acquisition profile YAML root must be a mapping/object")
    return AcquisitionProfile(**payload)


def render_acquisition_profile_yaml(profile: AcquisitionProfile) -> str:
    return yaml.safe_dump(
        profile.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def recommend_next_adapter(profile: AcquisitionProfile, attempts: list[CaptureAttempt]) -> AdapterRecommendation:
    if any(attempt.status == "passed" for attempt in attempts):
        return ""
    attempted = {attempt.adapter for attempt in attempts}
    disabled_adapters = {
        adapter.adapter for adapter in profile.adapters if not adapter.enabled
    }
    adapter_sequence: list[str] = [profile.default_adapter]
    for adapter in profile.fallback_order:
        if adapter not in adapter_sequence:
            adapter_sequence.append(adapter)
    for adapter in adapter_sequence:
        if adapter not in attempted and adapter not in disabled_adapters:
            return adapter
    return ""
