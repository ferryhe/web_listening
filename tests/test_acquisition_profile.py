from pathlib import Path

import pytest
from pydantic import ValidationError

from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig,
    AcquisitionProfile,
    AcquisitionQualityGates,
    AcquisitionSafetyPolicy,
    CaptureAttempt,
    build_default_acquisition_profile,
    load_acquisition_profile,
    recommend_next_adapter,
    render_acquisition_profile_yaml,
)


def test_legacy_profile_without_recipe_mappings_or_limits_remains_valid():
    profile = AcquisitionProfile(profile_id="legacy", site_key="demo", generated_at="2026-01-01T00:00:00Z")
    assert profile.recipe_mappings == []
    assert profile.resource_limits.timeout_seconds is None


def test_shared_quality_and_safety_models_preserve_legacy_scalar_coercion():
    quality = AcquisitionQualityGates(min_words="7", require_status_ok="true")
    safety = AcquisitionSafetyPolicy(allow_stealth_browser="true", require_authorized_access="false")

    assert quality.min_words == 7
    assert quality.require_status_ok is True
    assert safety.allow_stealth_browser is True
    assert safety.require_authorized_access is False


def test_governed_quality_and_authorization_scalars_accept_exact_types():
    quality = AcquisitionQualityGates(
        min_words=0,
        min_links=1,
        min_document_links=2,
        require_status_ok=False,
    )
    safety = AcquisitionSafetyPolicy(
        allow_stealth_browser=True,
        require_authorized_access=False,
    )

    assert (quality.min_words, quality.min_links, quality.min_document_links) == (0, 1, 2)
    assert quality.require_status_ok is False
    assert safety.allow_stealth_browser is True
    assert safety.require_authorized_access is False


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "docs/testing/fixtures/acquisition-profile-v1.sample.yaml"


def test_default_profile_does_not_include_cloakbrowser_when_not_allowed():
    profile = build_default_acquisition_profile(
        "example-site",
        allowed_domains=["example.com"],
    )

    assert profile.schema_version == "acquisition-profile.v1"
    assert profile.default_adapter == "web_http"
    assert "cloakbrowser" not in profile.fallback_order
    assert "cloakbrowser" not in {
        adapter.adapter for adapter in profile.adapters if adapter.enabled
    }
    assert profile.safety.allow_stealth_browser is False
    assert profile.safety.require_authorized_access is False


def test_default_profile_requires_authorized_access_before_adding_cloakbrowser():
    profile = build_default_acquisition_profile(
        "example-site",
        allowed_domains=["example.com"],
        allow_stealth_browser=True,
    )

    assert profile.safety.allow_stealth_browser is True
    assert profile.safety.require_authorized_access is False
    assert "cloakbrowser" not in profile.fallback_order
    assert "cloakbrowser" not in {
        adapter.adapter for adapter in profile.adapters if adapter.enabled
    }


def test_default_profile_includes_cloakbrowser_when_both_safety_flags_are_set():
    profile = build_default_acquisition_profile(
        "example-site",
        allowed_domains=["example.com"],
        allow_stealth_browser=True,
        require_authorized_access=True,
    )

    assert profile.safety.allow_stealth_browser is True
    assert profile.safety.require_authorized_access is True
    assert "cloakbrowser" in profile.fallback_order
    assert "cloakbrowser" in {
        adapter.adapter for adapter in profile.adapters if adapter.enabled
    }


def test_cloakbrowser_fallback_is_rejected_without_explicit_safety_settings():
    with pytest.raises(ValidationError, match="cloakbrowser"):
        AcquisitionProfile(
            profile_id="example-site-acquisition",
            site_key="example-site",
            generated_at="2026-05-12T12:00:00Z",
            strategy="http-first",
            default_adapter="web_http",
            fallback_order=["web_http", "cloakbrowser"],
            safety=AcquisitionSafetyPolicy(
                allowed_domains=["example.com"],
                allow_stealth_browser=True,
                require_authorized_access=False,
            ),
        )


def test_cloakbrowser_default_adapter_is_rejected_without_explicit_safety_settings():
    with pytest.raises(ValidationError, match="cloakbrowser"):
        AcquisitionProfile(
            profile_id="example-site-acquisition",
            site_key="example-site",
            generated_at="2026-05-12T12:00:00Z",
            strategy="unauthorized-stealth-default",
            default_adapter="cloakbrowser",
            fallback_order=[],
            safety=AcquisitionSafetyPolicy(
                allowed_domains=["example.com"],
                allow_stealth_browser=False,
                require_authorized_access=False,
            ),
        )


def test_cloakbrowser_is_allowed_when_both_safety_flags_are_true():
    profile = AcquisitionProfile(
        profile_id="example-site-acquisition",
        site_key="example-site",
        generated_at="2026-05-12T12:00:00Z",
        strategy="authorized-stealth-fallback",
        default_adapter="web_http",
        fallback_order=["web_http", "browser_rendered", "cloakbrowser"],
        safety=AcquisitionSafetyPolicy(
            allowed_domains=["example.com"],
            allow_stealth_browser=True,
            require_authorized_access=True,
        ),
    )

    assert profile.fallback_order[-1] == "cloakbrowser"


def test_yaml_fixture_loads_and_round_trips_to_valid_profile(tmp_path: Path):
    profile = load_acquisition_profile(FIXTURE_PATH)

    assert profile.schema_version == "acquisition-profile.v1"
    assert profile.profile_id
    assert profile.site_key
    assert profile.adapters

    round_trip_path = tmp_path / "acquisition-profile.yaml"
    round_trip_path.write_text(render_acquisition_profile_yaml(profile), encoding="utf-8")
    round_tripped = load_acquisition_profile(round_trip_path)

    assert round_tripped == profile


@pytest.mark.parametrize(
    ("yaml_text", "root_kind"),
    [
        ("- web_http\n- browser_rendered\n", "list"),
        ("web_http\n", "scalar"),
    ],
)
def test_load_acquisition_profile_rejects_non_mapping_yaml_roots(
    tmp_path: Path,
    yaml_text: str,
    root_kind: str,
):
    profile_path = tmp_path / f"{root_kind}-profile.yaml"
    profile_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="YAML root must be a mapping"):
        load_acquisition_profile(profile_path)


def test_load_acquisition_profile_normalizes_malformed_yaml_without_canaries(tmp_path: Path):
    profile_path = tmp_path / "SECRET-PATH-CANARY-profile.yaml"
    profile_path.write_text("profile_id: [SECRET-CONTENT-CANARY\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_acquisition_profile(profile_path)

    assert str(exc_info.value) == "acquisition profile YAML is invalid"
    assert "SECRET-PATH-CANARY" not in str(exc_info.value)
    assert "SECRET-CONTENT-CANARY" not in str(exc_info.value)


def test_default_loader_coerces_legacy_quality_and_safety_scalars(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("""profile_id: legacy
site_key: demo
generated_at: "2026-01-01T00:00:00Z"
quality_gates:
  min_words: "120"
  require_status_ok: "true"
safety:
  allow_stealth_browser: "false"
  require_authorized_access: "true"
""", encoding="utf-8")

    profile = load_acquisition_profile(profile_path)

    assert profile.quality_gates.min_words == 120
    assert profile.quality_gates.require_status_ok is True
    assert profile.safety.allow_stealth_browser is False
    assert profile.safety.require_authorized_access is True


@pytest.mark.parametrize("yaml_line", [
    'quality_gates: {min_words: "120"}',
    'quality_gates: {min_links: true}',
    'quality_gates: {require_status_ok: "true"}',
    'safety: {allow_stealth_browser: "false"}',
    'safety: {require_authorized_access: "true"}',
])
def test_strict_loader_rejects_coercible_quality_and_safety_scalars(tmp_path: Path, yaml_line: str):
    profile_path = tmp_path / "SECRET-PATH-CANARY-profile.yaml"
    profile_path.write_text(
        'profile_id: legacy\nsite_key: demo\ngenerated_at: "2026-01-01T00:00:00Z"\n' + yaml_line + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_acquisition_profile(profile_path, strict=True)

    assert "SECRET-PATH-CANARY" not in str(exc_info.value)
    assert "120" not in str(exc_info.value)


@pytest.mark.parametrize("enabled_yaml", ['"true"', "1"])
def test_strict_loader_rejects_coercible_adapter_enabled_scalars(tmp_path: Path, enabled_yaml: str):
    profile_path = tmp_path / "SECRET-PATH-CANARY-profile.yaml"
    profile_path.write_text(
        'profile_id: legacy\nsite_key: demo\ngenerated_at: "2026-01-01T00:00:00Z"\n'
        f'adapters: [{{adapter: web_http, enabled: {enabled_yaml}, reason: "SECRET-CONTENT-CANARY"}}]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_acquisition_profile(profile_path, strict=True)

    assert str(exc_info.value) == "acquisition profile has an invalid adapter enabled flag"
    assert "SECRET-PATH-CANARY" not in str(exc_info.value)
    assert "SECRET-CONTENT-CANARY" not in str(exc_info.value)


def test_default_loader_and_direct_adapter_model_preserve_enabled_coercion(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        'profile_id: legacy\nsite_key: demo\ngenerated_at: "2026-01-01T00:00:00Z"\n'
        'adapters: [{adapter: web_http, enabled: "true"}]\n',
        encoding="utf-8",
    )

    profile = load_acquisition_profile(profile_path)
    adapter = AcquisitionAdapterConfig(adapter="web_http", enabled=1)

    assert profile.adapters[0].enabled is True
    assert adapter.enabled is True


def test_recommend_next_adapter_walks_fallback_order_and_stops_after_passed_attempt():
    profile = build_default_acquisition_profile(
        "example-site",
        allowed_domains=["example.com"],
    )

    first_attempt = CaptureAttempt(
        adapter="web_http",
        status="failed_quality_gate",
        url="https://example.com/",
        status_code=200,
        word_count=10,
        link_count=1,
        document_link_count=0,
        failure_reason="too few words",
    )
    second_attempt = CaptureAttempt(
        adapter="browser_rendered",
        status="blocked",
        url="https://example.com/",
        failure_reason="blocked marker",
    )

    assert recommend_next_adapter(profile, []) == "web_http"
    assert recommend_next_adapter(profile, [first_attempt]) == "browser_rendered"
    assert recommend_next_adapter(profile, [first_attempt, second_attempt]) == "sitemap"
    assert (
        recommend_next_adapter(
            profile,
            [
                first_attempt,
                CaptureAttempt(
                    adapter="browser_rendered",
                    status="passed",
                    url="https://example.com/",
                    word_count=300,
                    link_count=12,
                    document_link_count=2,
                ),
            ],
        )
        == ""
    )


def test_recommend_next_adapter_skips_explicitly_disabled_adapters():
    profile = build_default_acquisition_profile(
        "example-site",
        allowed_domains=["example.com"],
    )
    profile.adapters = [
        adapter.model_copy(update={"enabled": False})
        if adapter.adapter == "browser_rendered"
        else adapter
        for adapter in profile.adapters
    ]

    first_attempt = CaptureAttempt(
        adapter="web_http",
        status="failed_quality_gate",
        url="https://example.com/",
        failure_reason="too few words",
    )

    assert recommend_next_adapter(profile, [first_attempt]) == "sitemap"


def test_default_profile_keeps_browseract_disabled_and_out_of_fallback():
    profile = build_default_acquisition_profile("example-site", allowed_domains=["example.com"])
    browseract = next(item for item in profile.adapters if item.adapter == "browseract")
    assert browseract.enabled is False
    assert "browseract" not in profile.fallback_order


def test_enabled_browseract_requires_both_authorization_gates():
    with pytest.raises(ValidationError, match="browseract requires"):
        AcquisitionProfile(
            profile_id="example-site-acquisition", site_key="example-site",
            generated_at="2026-07-18T00:00:00Z", default_adapter="web_http",
            adapters=[{"adapter": "browseract", "enabled": True}],
        )


def test_contract_models_reject_unknown_top_level_fields():
    with pytest.raises(ValidationError, match="extra"):
        AcquisitionProfile(
            profile_id="example-site-acquisition",
            site_key="example-site",
            generated_at="2026-05-12T12:00:00Z",
            default_adapter="web_http",
            unexpected="field",
        )

    with pytest.raises(ValidationError, match="extra"):
        CaptureAttempt(
            adapter="web_http",
            status="passed",
            url="https://example.com/",
            unexpected="field",
        )


def test_capture_attempt_rejects_unsupported_recommended_next_adapter():
    with pytest.raises(ValidationError, match="recommended_next_adapter"):
        CaptureAttempt(
            adapter="web_http",
            status="failed_quality_gate",
            url="https://example.com/",
            recommended_next_adapter="unsupported_adapter",
        )


@pytest.mark.parametrize(
    "bad_value",
    [
        {"example.com": True},
        b"example.com",
        bytearray(b"example.com"),
        [123],
        range(1),
    ],
)
def test_allowed_domains_error_message_mentions_single_string_or_list(bad_value):
    with pytest.raises(
        ValidationError,
        match="list of non-empty strings or a single string",
    ):
        AcquisitionSafetyPolicy(allowed_domains=bad_value)
