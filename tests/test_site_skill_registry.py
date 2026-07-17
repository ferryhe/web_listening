from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import web_listening.site_skill_registry as registry_module
from web_listening.cli import app
from web_listening.site_skill_registry import (
    list_site_skills,
    resolve_site_skill,
    validate_site_skill_package,
)


EXAMPLE = Path("web_listening/skills/sites/example-news/1.0.0")
runner = CliRunner()


def copy_example(tmp_path: Path) -> Path:
    package = tmp_path / "sites" / "example-news" / "1.0.0"
    shutil.copytree(EXAMPLE, package)
    return package


def diagnostic_codes(result: dict[str, object]) -> set[str]:
    return {item["code"] for item in result["diagnostics"]}  # type: ignore[index, union-attr]


def set_secret_policy(package: Path, *, allowed: list[str]) -> None:
    path = package / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["secret_policy"] = {
        "allow_secret_references": True,
        "forbid_secret_values": True,
        "allowed_reference_schemes": allowed,
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_packaged_example_is_valid_and_digests_are_stable() -> None:
    first = validate_site_skill_package(EXAMPLE)
    second = validate_site_skill_package(EXAMPLE)
    assert first == second
    assert first["valid"] is True
    assert len(first["manifest_sha256"]) == 64  # type: ignore[arg-type]
    assert sorted(first["script_sha256"]) == [
        "scripts/executor.py",
        "scripts/recipe.py",
    ]


def test_wheel_package_data_declares_site_skill_tree() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        "skills/sites/**/*"
        in config["tool"]["setuptools"]["package-data"]["web_listening"]
    )


def test_registry_lists_invalid_candidates(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").unlink()
    items = list_site_skills(tmp_path / "sites")
    assert len(items) == 1
    assert items[0]["valid"] is False
    assert "package.missing_required" in diagnostic_codes(items[0])


def test_exact_resolution_rejects_zero_duplicate_and_noncanonical_selectors(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    item = validate_site_skill_package(package)
    root = tmp_path / "sites"
    assert (
        resolve_site_skill(
            site_key="example-news",
            version="1.0.0",
            package_sha256=item["package_sha256"],
            root=root,
        )
        == item
    )
    with pytest.raises(LookupError):
        resolve_site_skill(
            site_key="missing",
            version="1.0.0",
            package_sha256=item["package_sha256"],
            root=root,
        )
    shutil.copytree(package, root / "duplicate" / "copy")
    with pytest.raises(LookupError):
        resolve_site_skill(
            site_key="example-news",
            version="1.0.0",
            package_sha256=item["package_sha256"],
            root=root,
        )
    with pytest.raises(ValueError):
        resolve_site_skill(
            site_key="example-news",
            version="01.0.0",
            package_sha256=item["package_sha256"],
            root=root,
        )
    with pytest.raises(ValueError):
        resolve_site_skill(
            site_key="example-news",
            version="1.0.0",
            package_sha256=str(item["package_sha256"]).upper(),
            root=root,
        )


def test_missing_declared_file_and_verification_are_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts/recipe.py").unlink()
    (package / "tests/verification.json").write_text(
        '{"implemented_rule_ids": ["different"]}', encoding="utf-8"
    )
    codes = diagnostic_codes(validate_site_skill_package(package))
    assert {"reference.missing", "verification.not_implemented"} <= codes


def test_all_symlinks_are_rejected_without_following(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts/link.py").symlink_to("recipe.py")
    (package / "broken").symlink_to("missing")
    result = validate_site_skill_package(package)
    assert [item["code"] for item in result["diagnostics"]].count("path.symlink") == 2


def test_manifest_traversal_is_rejected_by_governed_json_validation(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["recipes"][0]["entrypoint"] = "../escape.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "manifest.invalid" in diagnostic_codes(validate_site_skill_package(package))


def test_malformed_portable_filename_is_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts/bad:name.txt").write_text("not portable", encoding="utf-8")
    assert "path.invalid" in diagnostic_codes(validate_site_skill_package(package))


def test_profile_domain_mismatch_and_unauthorized_url_are_rejected(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles/default.yaml").write_text(
        "allowed_domains:\n  - invalid.example\ntarget_url: https://evil.example/news\n",
        encoding="utf-8",
    )
    codes = diagnostic_codes(validate_site_skill_package(package))
    assert {"profile.domain_mismatch", "profile.unauthorized_url"} <= codes


def test_security_diagnostic_never_echoes_secret(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    secret = "super-sensitive-value"
    (package / "SKILL.md").write_text(
        f"Authorization: Bearer {secret}\n", encoding="utf-8"
    )
    result = validate_site_skill_package(package)
    assert "security.secret_value" in diagnostic_codes(result)
    assert secret not in json.dumps(result)


@pytest.mark.parametrize(
    ("content", "category"),
    [
        ('{"token": "actual-token-value"}', "security.secret.assignment"),
        ('apiKey: "actual-api-key"', "security.secret.assignment"),
        ("API_KEY: actual_api_key", "security.secret.assignment"),
        ('clientSecret: "actual-client-secret"', "security.secret.assignment"),
        ("AWS_SECRET_ACCESS_KEY: actual-aws-secret", "security.secret.assignment"),
        ("Authorization: Basic dXNlcjpwYXNz", "security.secret.authorization"),
        ('cookie: "session=actual-cookie"', "security.secret.cookie"),
        ("set-cookie: session=actual-cookie", "security.secret.cookie"),
        ("proxy_username: proxy-user", "security.secret.assignment"),
        ("proxy_password: proxy-password", "security.secret.assignment"),
        ("proxy_auth: proxy-auth-value", "security.secret.assignment"),
        ("proxy: http://user:password@proxy.example", "security.secret.proxy_userinfo"),
        ("-----BEGIN PRIVATE KEY-----", "security.secret.private_key"),
    ],
)
def test_secret_categories_cover_common_forms_without_echoing(
    tmp_path: Path, content: str, category: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(content, encoding="utf-8")
    result = validate_site_skill_package(package)
    assert category in diagnostic_codes(result)
    assert content not in json.dumps(result)


@pytest.mark.parametrize(
    "content",
    [
        "token: ${TOKEN}",
        "apiKey: $API_KEY",
        "clientSecret: env:CLIENT_SECRET",
        "proxy_password: <provided-at-runtime>",
        "Authorization: Bearer {{ACCESS_TOKEN}}",
    ],
)
def test_secret_references_obey_manifest_policy(tmp_path: Path, content: str) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(content, encoding="utf-8")
    assert "security.secret.reference_forbidden" in diagnostic_codes(
        validate_site_skill_package(package)
    )
    set_secret_policy(package, allowed=["env", "placeholder", "template"])
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    ("content", "scheme"),
    [
        ('{"api_key": "${API_KEY}"}', "env"),
        ('api_key: "env:API_KEY"', "env"),
        ("privateKey: secret://team/key", "secret"),
        ("credential: vault://team/key", "vault"),
    ],
)
def test_quoted_references_are_parsed_and_schemes_are_enforced(
    tmp_path: Path, content: str, scheme: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(content, encoding="utf-8")
    set_secret_policy(package, allowed=[scheme])
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )
    set_secret_policy(package, allowed=["template"])
    assert "security.secret.reference_scheme" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        ("json", '{"nested": {"t\\u006fken": "literal-sensitive-value"}}'),
        ("yaml", 'nested:\n  "t\\u006fken": literal-sensitive-value\n'),
    ],
)
def test_structured_escaped_nested_secret_keys_are_rejected(
    tmp_path: Path, suffix: str, content: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / f"escaped.{suffix}").write_text(content, encoding="utf-8")
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        (
            "mixed.JSON",
            '{"token": "literal-sensitive-value"}',
            "security.secret.assignment",
        ),
        ("mixed.Yaml", 'local_path: "/etc/passwd"\n', "security.absolute_path"),
        (
            "mixed.PY",
            'password = "literal-sensitive-value"\n',
            "security.secret.assignment",
        ),
    ],
)
def test_governed_suffix_classification_is_case_insensitive(
    tmp_path: Path, filename: str, content: str, expected: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / filename).write_text(content, encoding="utf-8")
    assert expected in diagnostic_codes(validate_site_skill_package(package))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("secret_policy", "literal-sensitive-value"),
        ("allow_secret_references", "literal-sensitive-value"),
        ("forbid_secret_values", "literal-sensitive-value"),
        ("allowed_reference_schemes", "literal-sensitive-value"),
    ],
)
def test_secret_metadata_names_are_exempt_only_in_typed_manifest_context(
    tmp_path: Path, key: str, value: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "metadata-abuse.yaml").write_text(
        yaml.safe_dump({key: value}), encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_valid_manifest_secret_policy_metadata_remains_accepted(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    set_secret_policy(package, allowed=["env", "vault"])
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    "key",
    [
        "tokens",
        "api_keys",
        "passwords",
        "cookies",
        "secrets",
        "access_keys",
        "private_keys",
        "authorizations",
        "accessKeys",
        "privateKeys",
        "apiKeys",
    ],
)
def test_plural_secret_keys_are_rejected(tmp_path: Path, key: str) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "plural.yaml").write_text(
        yaml.safe_dump({key: "literal-sensitive-value"}), encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize("key", ["clientAPIKeys", "APITokens", "JWTKeys", "auths"])
def test_acronym_and_plural_credential_names_are_rejected(
    tmp_path: Path, key: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "credential-names.yaml").write_text(
        yaml.safe_dump({key: "literal-sensitive-value"}), encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "key",
    [
        "sort_key",
        "cache_key",
        "site_key",
        "authorization_url",
        "token_count",
        "password_policy",
        "cookie_domain",
    ],
)
def test_ordinary_key_names_remain_non_secret(tmp_path: Path, key: str) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "ordinary-keys.yaml").write_text(
        yaml.safe_dump({key: "ordinary-value"}), encoding="utf-8"
    )
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ('headers["Authorization"] = "literal"', "security.secret.authorization"),
        ('os.environ["API_KEY"] = "literal"', "security.secret.assignment"),
    ],
)
def test_python_subscript_literal_secret_assignments_are_rejected(
    tmp_path: Path, assignment: str, expected: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "literal_assignment.py").write_text(
        assignment, encoding="utf-8"
    )
    assert expected in diagnostic_codes(validate_site_skill_package(package))


def test_python_subscript_secret_reference_obeys_policy(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "reference_assignment.py").write_text(
        'os.environ["API_KEY"] = "${API_KEY}"', encoding="utf-8"
    )
    set_secret_policy(package, allowed=["env"])
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            'CONFIG = {"t\\x6fken": "literal-sensitive-value"}',
            "security.secret.assignment",
        ),
        (
            'HEADERS = {"Authori\\x7aation": "Bearer literal-sensitive-value"}',
            "security.secret.authorization",
        ),
        (
            'REFS = {"client_secr\\x65t": "${CLIENT_SECRET}"}',
            "security.secret.reference_forbidden",
        ),
        (
            'CONFIG = {b"t\\x6fken": "literal-sensitive-value"}',
            "security.secret.assignment",
        ),
        (
            'CONFIG = {b"t\\x6fken": b"literal-sensitive-value"}',
            "security.secret.assignment",
        ),
    ],
)
def test_python_mapping_literal_keys_are_decoded_and_govern_values(
    tmp_path: Path, content: str, expected: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "mapping.py").write_text(content, encoding="utf-8")
    assert expected in diagnostic_codes(validate_site_skill_package(package))


@pytest.mark.parametrize(
    "assignment",
    [
        'API_KEY, OTHER = ("literal-secret", "ordinary")',
        '[API_KEY] = ["literal-secret"]',
        '(OTHER, [API_KEY, PASSWORD]) = ("ordinary", ["literal-key", "literal-password"])',
    ],
)
def test_python_destructured_literal_secrets_are_rejected(
    tmp_path: Path, assignment: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "destructured.py").write_text(assignment, encoding="utf-8")
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_python_augmented_literal_subscript_secret_is_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "augmented.py").write_text(
        'headers["Authorization"] += "literal-secret"', encoding="utf-8"
    )
    assert "security.secret.authorization" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "assignment",
    [
        'API_KEY, OTHER = ("literal-secret",)',
        'API_KEY, *OTHER = ("literal-secret", "ordinary")',
        "API_KEY, OTHER = get_credentials()",
        'API_KEY, OTHER = (os.getenv("API_KEY"), "ordinary")',
    ],
)
def test_python_incompatible_or_nonliteral_destructuring_is_not_secret_linted(
    tmp_path: Path, assignment: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "destructured.py").write_text(assignment, encoding="utf-8")
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    "content",
    [
        'API_\\x4bEY = "literal-secret"\nif True print("broken")',
        'path = "\\x2fetc/passwd"\nif True print("broken")',
    ],
)
def test_malformed_python_fails_closed_with_stable_diagnostic(
    tmp_path: Path, content: str
) -> None:
    package = copy_example(tmp_path)
    path = package / "scripts" / "malformed.py"
    path.write_text(content, encoding="utf-8")
    result = validate_site_skill_package(package)
    assert result["valid"] is False
    assert any(
        diagnostic
        == {
            "code": "python.invalid",
            "path": "scripts/malformed.py",
            "message": "governed Python must be syntactically valid",
        }
        for diagnostic in result["diagnostics"]
    )


def test_malformed_python_preserves_raw_secret_diagnostics(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "malformed.py").write_text(
        'API_KEY = "literal-secret"\nif True print("broken")', encoding="utf-8"
    )
    codes = diagnostic_codes(validate_site_skill_package(package))
    assert {"python.invalid", "security.secret.assignment"} <= codes


@pytest.mark.parametrize(
    "content",
    [
        "!!binary dG9rZW4=: literal-sensitive-value\n",
        "nested:\n  7: literal-sensitive-value\n",
        "shared: &shared\n  7: literal-sensitive-value\nalias: *shared\n",
    ],
)
def test_yaml_non_string_mapping_keys_fail_closed(tmp_path: Path, content: str) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "non-string-key.yaml").write_text(content, encoding="utf-8")
    result = validate_site_skill_package(package)
    assert any(
        diagnostic["code"] == "structured.invalid_key"
        and diagnostic["path"] == "profiles/non-string-key.yaml"
        for diagnostic in result["diagnostics"]
    )


def test_yaml_unhashable_mapping_key_fails_closed_without_crashing(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "unhashable-key.yaml").write_text(
        "? [token]\n: literal-sensitive-value\n", encoding="utf-8"
    )
    result = validate_site_skill_package(package)
    assert any(
        diagnostic["code"] == "structured.invalid"
        and diagnostic["path"] == "profiles/unhashable-key.yaml"
        for diagnostic in result["diagnostics"]
    )


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        ("json", '{"nested":{"t\\u006fken":"literal-sensitive-value"'),
        ("yaml", 'nested:\n  "t\\u006fken": [literal-sensitive-value\n'),
    ],
)
def test_malformed_structured_files_with_escaped_secret_keys_fail_closed(
    tmp_path: Path, suffix: str, content: str
) -> None:
    package = copy_example(tmp_path)
    path = package / "profiles" / f"malformed.{suffix}"
    path.write_text(content, encoding="utf-8")
    result = validate_site_skill_package(package)
    assert result["valid"] is False
    assert any(
        diagnostic["code"] == "structured.invalid"
        and diagnostic["path"] == f"profiles/malformed.{suffix}"
        for diagnostic in result["diagnostics"]
    )


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("Bearer ${TOKEN}", True),
        ("Basic vault://team/credential", True),
        ("Bearer <provided at runtime>", True),
        ("Bearer ${TOKEN}literal", False),
        ("Bearer literal-credential", False),
        ("literal-credential", False),
    ],
)
@pytest.mark.parametrize("suffix", ["json", "yaml"])
def test_structured_authorization_uses_exact_header_reference_semantics(
    tmp_path: Path, value: str, valid: bool, suffix: str
) -> None:
    package = copy_example(tmp_path)
    content = (
        json.dumps({"authorization": value})
        if suffix == "json"
        else yaml.safe_dump({"authorization": value})
    )
    (package / "profiles" / f"authorization.{suffix}").write_text(
        content, encoding="utf-8"
    )
    set_secret_policy(package, allowed=["env", "placeholder", "vault"])
    secret_codes = {
        code
        for code in diagnostic_codes(validate_site_skill_package(package))
        if code.startswith("security.secret")
    }
    if valid:
        assert not secret_codes
    else:
        assert "security.secret.authorization" in secret_codes
        assert "security.secret_value" in secret_codes


@pytest.mark.parametrize(
    ("suffix", "value"),
    [
        ("json", "123456789"),
        ("json", "true"),
        ("json", '{"nested": "value"}'),
        ("yaml", "123456789"),
        ("yaml", "true"),
        ("yaml", "{nested: value}"),
    ],
)
def test_structured_secret_keys_reject_non_string_values(
    tmp_path: Path, suffix: str, value: str
) -> None:
    package = copy_example(tmp_path)
    content = (
        f'{{"t\\u006fken": {value}}}'
        if suffix == "json"
        else f'"t\\u006fken": {value}\n'
    )
    (package / "profiles" / f"typed-secret.{suffix}").write_text(
        content, encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "key",
    [
        "authorization_urls",
        "token_counts",
        "password_policies",
        "cookie_domains",
        "token_endpoint",
        "token_url",
        "auth_method",
        "password_reset_url",
        "cookie_name",
        "credential_type",
    ],
)
def test_bounded_public_credential_metadata_names_are_allowed(
    tmp_path: Path, key: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "metadata.yaml").write_text(
        yaml.safe_dump({key: "public-metadata"}), encoding="utf-8"
    )
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    "key",
    [
        "authorization_token",
        "token_endpoint_secret",
        "password_reset_token",
        "cookie_name_token",
        "credential_type_secret",
    ],
)
def test_public_metadata_exemptions_do_not_hide_secret_bearing_names(
    tmp_path: Path, key: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "secret.yaml").write_text(
        yaml.safe_dump({key: "literal-secret"}), encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "key",
    [
        "refresh_token_value",
        "client_secret_value",
        "oauth_access_token_value",
        "oauth_access_token_backup",
        "client_secret_material_v2",
    ],
)
def test_compound_credential_value_names_are_rejected(tmp_path: Path, key: str) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "compound-secret.yaml").write_text(
        yaml.safe_dump({key: "literal-secret"}), encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "key",
    [
        "refresh_interval_value",
        "client_label_value",
        "token_bucket_size",
        "password_strength_hint",
    ],
)
def test_noncredential_compound_value_names_remain_allowed(
    tmp_path: Path, key: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / "compound-metadata.yaml").write_text(
        yaml.safe_dump({key: "public-metadata"}), encoding="utf-8"
    )
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        ("json", '{"local_path":"\\u002fetc\\u002fpasswd"}'),
        ("yaml", 'local_path: "\\x2f"\n'),
        ("json", '{"local_path":"C:\\\\Windows\\\\system.ini"}'),
    ],
)
def test_structured_escaped_absolute_paths_are_rejected(
    tmp_path: Path, suffix: str, content: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles" / f"escaped-path.{suffix}").write_text(
        content, encoding="utf-8"
    )
    assert "security.absolute_path" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "content",
    [
        "allowed_domains: [example.com]\ncycle: &cycle [*cycle]\n",
        "allowed_domains: [example.com]\n" + "nested: " * 110 + "value\n",
    ],
)
def test_yaml_recursive_aliases_and_excessive_nesting_are_invalid(
    tmp_path: Path, content: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles/default.yaml").write_text(content, encoding="utf-8")
    result = validate_site_skill_package(package)
    assert result["valid"] is False
    assert {"profile.invalid", "structured.invalid"} & diagnostic_codes(result)


@pytest.mark.parametrize("header", ["cookie", "set-cookie"])
@pytest.mark.parametrize(
    ("reference", "scheme"),
    [
        ("${COOKIE}", "env"),
        ("$COOKIE", "env"),
        ("%COOKIE%", "env"),
        ("env:COOKIE", "env"),
        ("<runtime-cookie>", "placeholder"),
        ("{{cookie_template}}", "template"),
        ("secret://team/cookie", "secret"),
        ("vault://team/cookie", "vault"),
        ("keyring://team/cookie", "keyring"),
        ("aws-sm://team/cookie", "aws-sm"),
        ("gcp-sm://team/cookie", "gcp-sm"),
        ("azure-kv://team/cookie", "azure-kv"),
    ],
)
def test_cookie_reference_families_are_classified_as_complete_values(
    tmp_path: Path, header: str, reference: str, scheme: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(f"{header}: {reference}", encoding="utf-8")
    set_secret_policy(package, allowed=[scheme])
    assert not any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    ("key", "reference"),
    [
        ("token", "TOKEN"),
        ("cookie", "COOKIE"),
        ("set-cookie", "COOKIE"),
    ],
)
def test_environment_reference_with_literal_suffix_is_rejected(
    tmp_path: Path, key: str, reference: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(
        f"{key}: ${{{reference}}}literal-secret-value", encoding="utf-8"
    )
    set_secret_policy(package, allowed=["env"])
    assert any(
        code.startswith("security.secret")
        for code in diagnostic_codes(validate_site_skill_package(package))
    )


@pytest.mark.parametrize(
    "absolute_path",
    [
        "/custom/install/location/file.txt",
        "Z:\\custom\\file.txt",
        "\\\\server\\share\\file.txt",
        "file:///custom/file.txt",
        "/",
        "//server/share/file.txt",
    ],
)
def test_all_absolute_path_families_are_rejected(
    tmp_path: Path, absolute_path: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(absolute_path, encoding="utf-8")
    assert "security.absolute_path" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize("absolute_path", ['"/"', '"//"', "'/'", "'//'"])
def test_quoted_posix_root_paths_are_rejected(
    tmp_path: Path, absolute_path: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(absolute_path, encoding="utf-8")
    assert "security.absolute_path" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "expression", ["ratio = total / count", "buckets = total // count"]
)
def test_python_division_operators_are_not_absolute_paths(
    tmp_path: Path, expression: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts/division.py").write_text(expression, encoding="utf-8")
    assert "security.absolute_path" not in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_escaped_python_string_absolute_path_is_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "escaped_path.py").write_text(
        r'path = Path("\x2fetc/passwd")', encoding="utf-8"
    )
    assert "security.absolute_path" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    ("literal", "rejected"),
    [
        (r'b"\x2fetc/passwd"', True),
        (r'b"profiles/default.yaml"', False),
    ],
)
def test_python_bytes_literal_paths_are_decoded_before_classification(
    tmp_path: Path, literal: str, rejected: bool
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "bytes_path.py").write_text(
        f"fd = os.open({literal}, os.O_RDONLY)", encoding="utf-8"
    )
    assert (
        "security.absolute_path"
        in diagnostic_codes(validate_site_skill_package(package))
    ) is rejected


def test_nonliteral_python_path_expression_is_not_decoded(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "dynamic_path.py").write_text(
        'path = Path(prefix + "etc/passwd")', encoding="utf-8"
    )
    assert "security.absolute_path" not in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_invalid_utf8_governed_text_is_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_bytes(b"invalid: \xff")
    assert "text.invalid_utf8" in diagnostic_codes(validate_site_skill_package(package))


@pytest.mark.parametrize("name", ["bad\x7f.txt", "bad\u0085.txt", "bad\u202e.txt"])
def test_all_unicode_control_filename_categories_are_rejected(
    tmp_path: Path, name: str
) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / name).write_text("x", encoding="utf-8")
    assert "path.invalid" in diagnostic_codes(validate_site_skill_package(package))


def test_surrogateescape_filename_is_invalid_and_digest_never_crashes(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    directory = os.fsencode(package / "scripts")
    fd = os.open(directory + b"/bad-\xff.txt", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    result = validate_site_skill_package(package)
    assert "path.invalid" in diagnostic_codes(result)
    assert len(result["package_sha256"]) == 64


@pytest.mark.parametrize("suffix", ["json", "yaml"])
def test_profiles_reject_duplicate_keys(tmp_path: Path, suffix: str) -> None:
    package = copy_example(tmp_path)
    profile = package / "profiles" / f"duplicate.{suffix}"
    if suffix == "json":
        profile.write_text(
            '{"allowed_domains":["example.com"],"allowed_domains":["example.com"]}',
            encoding="utf-8",
        )
    else:
        profile.write_text(
            "allowed_domains: [example.com]\nallowed_domains: [example.com]\n",
            encoding="utf-8",
        )
    assert "profile.invalid" in diagnostic_codes(validate_site_skill_package(package))


def test_verification_declaration_rejects_duplicate_keys(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "tests/verification.json").write_text(
        '{"implemented_rule_ids":["status-ok"],"implemented_rule_ids":["status-ok"]}',
        encoding="utf-8",
    )
    assert "verification.invalid_declaration" in diagnostic_codes(
        validate_site_skill_package(package)
    )


@pytest.mark.parametrize(
    "profile",
    [
        "- not-a-mapping\n",
        "allowed_domains: []\n",
        "allowed_domains: [example.com, evil.example]\n",
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: ftp://example.com/file\n",
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: //example.com/file\n",
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: https://user:pass@example.com/file\n",
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: https://example.com.evil.test/file\n",
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: https://[malformed/file\n",
    ],
)
def test_profiles_are_mapping_domain_subset_and_recursively_url_safe(
    tmp_path: Path, profile: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles/default.yaml").write_text(profile, encoding="utf-8")
    assert validate_site_skill_package(package)["valid"] is False


def test_nested_authorized_subdomain_url_is_valid(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "profiles/default.yaml").write_text(
        "allowed_domains: [example.com]\nnested:\n  endpoint_url: https://news.example.com/path\n",
        encoding="utf-8",
    )
    assert validate_site_skill_package(package)["valid"] is True


@pytest.mark.parametrize(
    "url",
    [
        " https://example.com/path",
        "https://example.com/path ",
        "https://example.com/pa\tth",
        "https://example.com/pa\x00th",
        "https://user@example.com/path",
        "http:example.com/path",
    ],
)
def test_profile_urls_reject_raw_controls_whitespace_userinfo_and_nonabsolute(
    tmp_path: Path, url: str
) -> None:
    package = copy_example(tmp_path)
    (package / "profiles/default.yaml").write_text(
        f"allowed_domains: [example.com]\nendpoint_url: {json.dumps(url)}\n",
        encoding="utf-8",
    )
    assert "profile.unauthorized_url" in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_ordinary_http_urls_are_not_linted_as_absolute_paths(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text("https://example.com/a/b", encoding="utf-8")
    assert "security.absolute_path" not in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_proxy_username_only_userinfo_is_rejected_without_echo(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    value = "http://proxy-user@proxy.example"
    (package / "SKILL.md").write_text(f"proxy_url: {value}", encoding="utf-8")
    result = validate_site_skill_package(package)
    assert "security.secret.proxy_userinfo" in diagnostic_codes(result)
    assert value not in json.dumps(result)


@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "privateKey",
        "private_key",
        "aws_access_key_id",
        "AWS_SECRET_ACCESS_KEY",
        "sessionToken",
        "authCookie",
        "proxyCredential",
    ],
)
def test_nfkc_and_style_independent_secret_key_detection(
    tmp_path: Path, key: str
) -> None:
    package = copy_example(tmp_path)
    (package / "SKILL.md").write_text(
        f"{key}: literal-sensitive-value", encoding="utf-8"
    )
    assert "security.secret.assignment" in diagnostic_codes(
        validate_site_skill_package(package)
    )


def test_non_nfc_and_casefold_colliding_names_are_rejected(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    (package / "scripts" / "cafe\u0301.txt").write_text("x", encoding="utf-8")
    (package / "scripts" / "Case.txt").write_text("x", encoding="utf-8")
    (package / "scripts" / "case.txt").write_text("x", encoding="utf-8")
    codes = diagnostic_codes(validate_site_skill_package(package))
    assert {"path.invalid", "path.case_collision"} <= codes


def test_file_change_during_descriptor_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    original_stat = registry_module.os.stat
    calls = 0

    def racing_stat(path, *args, **kwargs):
        nonlocal calls
        if path == "SKILL.md" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                (package / "SKILL.md").write_text(
                    "changed during read", encoding="utf-8"
                )
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "stat", racing_stat)
    assert "path.tree_changed" in diagnostic_codes(validate_site_skill_package(package))


def test_same_inode_rewrite_between_stat_and_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    original_open = registry_module.os.open
    rewritten = False

    def rewrite_then_open(path, flags, *args, **kwargs):
        nonlocal rewritten
        if path == "SKILL.md" and kwargs.get("dir_fd") is not None and not rewritten:
            rewritten = True
            skill = package / "SKILL.md"
            original = skill.read_bytes()
            skill.write_bytes(original + b"\nrewritten")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "open", rewrite_then_open)
    result = validate_site_skill_package(package)

    assert "path.tree_changed" in diagnostic_codes(result)


def test_same_inode_directory_mutation_between_stat_and_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    original_open = registry_module.os.open
    mutated = False

    def mutate_then_open(path, flags, *args, **kwargs):
        nonlocal mutated
        if path == "scripts" and kwargs.get("dir_fd") is not None and not mutated:
            mutated = True
            (package / "scripts" / "injected.txt").write_text("x", encoding="utf-8")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "open", mutate_then_open)
    result = validate_site_skill_package(package)

    assert "path.tree_changed" in diagnostic_codes(result)


def test_profile_diagnostics_are_globally_bounded_and_stable(tmp_path: Path) -> None:
    package = copy_example(tmp_path)
    profile = package / "profiles" / "default.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "allowed_domains": ["example.com"],
                "urls": [
                    f"https://unauthorized.example/{index:04}"
                    for index in range(registry_module._MAX_PACKAGE_DIAGNOSTICS * 2)
                ],
            }
        ),
        encoding="utf-8",
    )

    first = validate_site_skill_package(package)
    second = validate_site_skill_package(package)
    serialized = json.loads(json.dumps(first))

    assert first == second
    assert len(first["diagnostics"]) == registry_module._MAX_PACKAGE_DIAGNOSTICS
    assert len(serialized["diagnostics"]) == registry_module._MAX_PACKAGE_DIAGNOSTICS
    assert [
        diagnostic
        for diagnostic in first["diagnostics"]
        if diagnostic["code"] == "package.diagnostic_limit"
    ] == [
        {
            "code": "package.diagnostic_limit",
            "path": ".",
            "message": "package diagnostics truncated at 1024 entries",
        }
    ]


def test_incomplete_digest_after_diagnostic_truncation_is_not_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)

    def truncated_read_tree(path, diagnostics, *, package_fd=None):
        del path, package_fd
        for index in range(registry_module._MAX_PACKAGE_DIAGNOSTICS):
            diagnostics.append(
                registry_module.Diagnostic("path.symlink", str(index), "forbidden")
            )
        diagnostics.append(
            registry_module.Diagnostic("path.tree_changed", "later", "incomplete")
        )
        return []

    monkeypatch.setattr(registry_module, "_read_tree", truncated_read_tree)
    result = validate_site_skill_package(package)

    assert len(result["diagnostics"]) == registry_module._MAX_PACKAGE_DIAGNOSTICS
    assert "path.tree_changed" not in diagnostic_codes(result)
    assert "package.diagnostic_limit" in diagnostic_codes(result)
    assert result["package_sha256"] is None


def test_registry_discovery_keeps_pinned_package_when_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    expected = validate_site_skill_package(package)["package_sha256"]
    original = registry_module.validate_site_skill_package
    replaced = False

    def replace_then_validate(path, *, _package_fd=None):
        nonlocal replaced
        if not replaced:
            replaced = True
            moved = package.with_name("1.0.0-pinned")
            package.rename(moved)
            shutil.copytree(EXAMPLE, package)
            (package / "SKILL.md").write_text("replacement", encoding="utf-8")
        return original(path, _package_fd=_package_fd)

    monkeypatch.setattr(
        registry_module, "validate_site_skill_package", replace_then_validate
    )
    results = list_site_skills(tmp_path / "sites")
    assert results[0]["package_sha256"] == expected


def test_deep_package_tree_returns_validation_result_without_recursion_error(
    tmp_path: Path,
) -> None:
    package = copy_example(tmp_path)
    directory = package / "scripts"
    created_directories = []
    leaf = None
    try:
        for _ in range(1050):
            directory /= "d"
            directory.mkdir()
            created_directories.append(directory)
        leaf = directory / "leaf.txt"
        leaf.write_text("leaf", encoding="utf-8")

        first = validate_site_skill_package(package)
        second = validate_site_skill_package(package)

        assert first == second
        assert first["valid"] is False
        assert any(
            diagnostic["code"] == "package.depth_limit"
            and diagnostic["message"] == "package directory depth exceeds 100"
            for diagnostic in first["diagnostics"]
        )
    finally:
        if leaf is not None:
            leaf.unlink(missing_ok=True)
        for created_directory in reversed(created_directories):
            created_directory.rmdir()


def test_package_file_count_limit_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    existing_count = sum(path.is_file() for path in package.rglob("*"))
    monkeypatch.setattr(registry_module, "_MAX_PACKAGE_FILES", existing_count)
    (package / "scripts" / "overflow.txt").write_text("x", encoding="utf-8")

    first = validate_site_skill_package(package)
    second = validate_site_skill_package(package)

    assert first == second
    assert any(
        diagnostic["code"] == "package.file_count_limit"
        and diagnostic["message"]
        == f"package contains more than {existing_count} files"
        for diagnostic in first["diagnostics"]
    )
    assert (
        sum(
            diagnostic["code"] == "package.file_count_limit"
            for diagnostic in first["diagnostics"]
        )
        == 1
    )


def test_broad_directory_traversal_is_bounded_and_reports_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    monkeypatch.setattr(registry_module, "_MAX_PACKAGE_FILES", 4)
    for index in range(40):
        (package / "scripts" / f"broad-{index:03}.txt").write_text(
            "x", encoding="utf-8"
        )
    original_stat = registry_module.os.stat
    inspected = 0

    def counting_stat(path, *args, **kwargs):
        nonlocal inspected
        if kwargs.get("dir_fd") is not None:
            inspected += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "stat", counting_stat)
    result = validate_site_skill_package(package)

    assert inspected <= 8
    assert [
        diagnostic["code"]
        for diagnostic in result["diagnostics"]
        if diagnostic["code"] == "package.file_count_limit"
    ] == ["package.file_count_limit"]


def test_package_file_size_limit_preserves_required_file_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    monkeypatch.setattr(registry_module, "_MAX_FILE_BYTES", 8)

    result = validate_site_skill_package(package)

    codes = diagnostic_codes(result)
    assert "package.file_size_limit" in codes
    assert "package.missing_required" in codes
    assert result["package_sha256"] is None


def test_package_aggregate_size_limit_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    monkeypatch.setattr(registry_module, "_MAX_PACKAGE_BYTES", 1)

    first = validate_site_skill_package(package)
    second = validate_site_skill_package(package)

    assert first == second
    assert "package.aggregate_size_limit" in diagnostic_codes(first)
    assert first["package_sha256"] is None


@pytest.mark.parametrize("limit_kind", ["file", "aggregate"])
def test_incomplete_package_digest_is_null_in_json_envelopes_and_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_kind: str
) -> None:
    package = copy_example(tmp_path)
    root = tmp_path / "sites"
    if limit_kind == "file":
        monkeypatch.setattr(registry_module, "_MAX_FILE_BYTES", 8)
    else:
        monkeypatch.setattr(registry_module, "_MAX_PACKAGE_BYTES", 1)

    validation = validate_site_skill_package(package)
    assert validation["valid"] is False
    assert validation["package_sha256"] is None

    retained_files = registry_module._read_tree(package, [])
    old_partial_digest = registry_module._package_digest(retained_files)
    with pytest.raises(LookupError):
        resolve_site_skill(
            site_key="example-news",
            version="1.0.0",
            package_sha256=old_partial_digest,
            root=root,
        )

    commands = [
        (
            ["list-site-skills", "--root", str(root), "--json"],
            "site-skill-list.v1",
            "skills",
            0,
        ),
        (
            [
                "inspect-site-skill",
                "--site-key",
                "example-news",
                "--version",
                "1.0.0",
                "--package-digest",
                old_partial_digest,
                "--root",
                str(root),
                "--json",
            ],
            "site-skill-inspect.v1",
            "skill",
            1,
        ),
        (
            ["validate-site-skill", "--package-path", str(package), "--json"],
            "site-skill-validation.v1",
            "skill",
            1,
        ),
    ]
    for args, schema, payload_key, exit_code in commands:
        result = runner.invoke(app, args)
        assert result.exit_code == exit_code
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == schema
        skill = payload[payload_key]
        if isinstance(skill, list):
            skill = skill[0]
        assert skill["package_sha256"] is None


def test_memory_error_during_bounded_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    original_fdopen = registry_module.os.fdopen
    raised = False

    class MemoryFailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            raise MemoryError

    def memory_failing_fdopen(fd, *args, **kwargs):
        nonlocal raised
        stream = original_fdopen(fd, *args, **kwargs)
        if not raised:
            raised = True
            return MemoryFailingStream(stream)
        return stream

    monkeypatch.setattr(registry_module.os, "fdopen", memory_failing_fdopen)
    result = validate_site_skill_package(package)

    assert "package.resource_exhausted" in diagnostic_codes(result)


def test_regular_file_replaced_by_fifo_at_open_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = copy_example(tmp_path)
    original_open = registry_module.os.open
    replaced = False

    def replace_with_fifo(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "SKILL.md" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            (package / "SKILL.md").unlink()
            os.mkfifo(package / "SKILL.md")
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "open", replace_with_fifo)
    result = validate_site_skill_package(package)

    assert "path.tree_changed" in diagnostic_codes(result)


@pytest.mark.parametrize("kind", ["missing", "symlink"])
def test_explicit_invalid_registry_root_is_structured_and_cli_nonzero(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "registry"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    results = list_site_skills(root)
    assert diagnostic_codes(results[0]) == {"registry.invalid_root"}
    cli = runner.invoke(app, ["list-site-skills", "--root", str(root), "--json"])
    assert cli.exit_code == 1
    assert (
        json.loads(cli.stdout)["skills"][0]["diagnostics"][0]["code"]
        == "registry.invalid_root"
    )
    assert cli.stderr == ""


def test_explicit_unreadable_registry_root_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    original_open = registry_module.os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path) == root and kwargs.get("dir_fd") is None:
            raise PermissionError("denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "open", denied)
    assert diagnostic_codes(list_site_skills(root)[0]) == {"registry.invalid_root"}


def test_registry_breadth_is_bounded_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    monkeypatch.setattr(registry_module, "_MAX_REGISTRY_ENTRIES", 3)
    for index in range(4):
        (root / f"site-{index}").mkdir()

    first = list_site_skills(root)
    second = list_site_skills(root)

    assert first == second
    assert len(first) == 1
    assert diagnostic_codes(first[0]) == {"registry.entry_count_limit"}


def test_site_breadth_is_bounded_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sites"
    site = root / "broad-site"
    site.mkdir(parents=True)
    monkeypatch.setattr(registry_module, "_MAX_REGISTRY_ENTRIES", 3)
    for index in range(4):
        (site / f"version-{index}").mkdir()

    results = list_site_skills(root)

    assert len(results) == 1
    assert diagnostic_codes(results[0]) == {"site.entry_count_limit"}


def test_registry_enumeration_memory_error_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sites"
    root.mkdir()

    def fail_scandir(path):
        raise MemoryError

    monkeypatch.setattr(registry_module.os, "scandir", fail_scandir)
    results = list_site_skills(root)

    assert len(results) == 1
    assert diagnostic_codes(results[0]) == {"registry.resource_exhausted"}


def test_absent_default_registry_root_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "missing-registry"
    monkeypatch.setattr(registry_module, "default_registry_root", lambda: root)
    assert list_site_skills() == []


def test_dangling_default_registry_symlink_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "registry"
    root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    monkeypatch.setattr(registry_module, "default_registry_root", lambda: root)
    results = list_site_skills()
    assert diagnostic_codes(results[0]) == {"registry.invalid_root"}
    cli = runner.invoke(app, ["list-site-skills", "--json"])
    assert cli.exit_code == 1
    assert (
        json.loads(cli.stdout)["skills"][0]["diagnostics"][0]["code"]
        == "registry.invalid_root"
    )
    assert cli.stderr == ""


@pytest.mark.parametrize("command", ["inspect-site-skill", "validate-site-skill"])
def test_selector_commands_preserve_invalid_root_json_envelope(
    tmp_path: Path, command: str
) -> None:
    root = tmp_path / "missing-registry"
    result = runner.invoke(
        app,
        [
            command,
            "--site-key",
            "example-news",
            "--version",
            "1.0.0",
            "--package-digest",
            "0" * 64,
            "--root",
            str(root),
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["skill"]["diagnostics"][0]["code"] == "registry.invalid_root"
    assert result.stderr == ""


@pytest.mark.parametrize("command", ["inspect-site-skill", "validate-site-skill"])
def test_selector_commands_resolve_from_one_registry_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    package = copy_example(tmp_path)
    item = validate_site_skill_package(package)
    original_list = registry_module.list_site_skills
    calls = 0

    def list_once(root=None):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("registry scanned more than once")
        return original_list(root)

    monkeypatch.setattr(registry_module, "list_site_skills", list_once)
    result = runner.invoke(
        app,
        [
            command,
            "--site-key",
            "example-news",
            "--version",
            "1.0.0",
            "--package-digest",
            str(item["package_sha256"]),
            "--root",
            str(tmp_path / "sites"),
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["skill"]["valid"] is True
    assert calls == 1


@pytest.mark.parametrize("command", ["inspect-site-skill", "validate-site-skill"])
@pytest.mark.parametrize(
    ("site_key", "version", "digest"),
    [
        ("missing", "1.0.0", "0" * 64),
        ("example-news", "01.0.0", "0" * 64),
        ("example-news", "1.0.0", "INVALID"),
    ],
)
def test_selector_failures_preserve_json_envelope(
    tmp_path: Path, command: str, site_key: str, version: str, digest: str
) -> None:
    copy_example(tmp_path)
    result = runner.invoke(
        app,
        [
            command,
            "--site-key",
            site_key,
            "--version",
            version,
            "--package-digest",
            digest,
            "--root",
            str(tmp_path / "sites"),
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert (
        json.loads(result.stdout)["skill"]["diagnostics"][0]["code"]
        == "selector.invalid"
    )
    assert result.stderr == ""


@pytest.mark.parametrize("command", ["inspect-site-skill", "validate-site-skill"])
def test_incomplete_selectors_preserve_json_envelope(command: str) -> None:
    result = runner.invoke(app, [command, "--site-key", "example-news", "--json"])
    assert result.exit_code == 1
    assert (
        json.loads(result.stdout)["skill"]["diagnostics"][0]["code"]
        == "selector.invalid"
    )
    assert result.stderr == ""


@pytest.mark.parametrize("kind", ["symlink", "file", "unreadable"])
def test_invalid_site_entries_are_emitted_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    site = root / "candidate"
    if kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        site.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        site.write_text("not a directory", encoding="utf-8")
    else:
        site.mkdir()
        original_open = registry_module.os.open

        def denied(path, flags, *args, **kwargs):
            if path == "candidate" and kwargs.get("dir_fd") is not None:
                raise PermissionError("denied")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(registry_module.os, "open", denied)

    results = list_site_skills(root)
    assert [result["path"] for result in results] == [str(site)]
    assert diagnostic_codes(results[0]) == {"site.not_directory"}


def test_site_list_failure_retains_prior_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_package = copy_example(tmp_path)
    root = valid_package.parents[1]
    broken_site = root / "z-broken"
    broken_site.mkdir()
    broken_identity = (broken_site.stat().st_dev, broken_site.stat().st_ino)
    original_scandir = registry_module.os.scandir

    def fail_broken_site(path):
        if isinstance(path, int):
            info = os.fstat(path)
            if (info.st_dev, info.st_ino) == broken_identity:
                raise OSError("post-open list failure")
        return original_scandir(path)

    monkeypatch.setattr(registry_module.os, "scandir", fail_broken_site)
    results = list_site_skills(root)

    assert [result["path"] for result in results] == [
        str(valid_package),
        str(broken_site),
    ]
    assert results[0]["site_key"] == "example-news"
    assert diagnostic_codes(results[1]) == {"site.not_directory"}


@pytest.mark.parametrize("command", ["list", "inspect", "validate"])
def test_cli_json_is_idempotent_stdout_only_and_does_not_mutate(
    tmp_path: Path, command: str
) -> None:
    package = copy_example(tmp_path)
    before = {
        path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
    }
    item = validate_site_skill_package(package)
    if command == "list":
        args = ["list-site-skills", "--root", str(tmp_path / "sites"), "--json"]
        schema = "site-skill-list.v1"
    elif command == "inspect":
        args = [
            "inspect-site-skill",
            "--site-key",
            "example-news",
            "--version",
            "1.0.0",
            "--package-digest",
            str(item["package_sha256"]),
            "--root",
            str(tmp_path / "sites"),
            "--json",
        ]
        schema = "site-skill-inspect.v1"
    else:
        args = ["validate-site-skill", "--package-path", str(package), "--json"]
        schema = "site-skill-validation.v1"
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ""
    assert json.loads(first.stdout)["schema_version"] == schema
    after = {
        path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX byte filenames")
@pytest.mark.parametrize("command", ["list", "inspect", "validate"])
def test_cli_json_is_surrogate_safe_and_byte_identical(
    tmp_path: Path, command: str
) -> None:
    package = copy_example(tmp_path)
    root = tmp_path / "sites"
    undecodable = os.fsencode(root) + b"/invalid-\xff"
    os.mkdir(undecodable)
    item = validate_site_skill_package(package)
    if command == "list":
        args = ["list-site-skills", "--root", str(root), "--json"]
    else:
        args = [
            f"{command}-site-skill",
            "--site-key",
            "example-news",
            "--version",
            "1.0.0",
            "--package-digest",
            str(item["package_sha256"]),
            "--root",
            str(root),
            "--json",
        ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == second.exit_code == 0
    assert first.stdout_bytes == second.stdout_bytes
    assert first.stderr == second.stderr == ""
    json.loads(first.stdout)


@pytest.mark.parametrize(
    "command", ["list-site-skills", "inspect-site-skill", "validate-site-skill"]
)
def test_cli_commands_have_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert command in result.stdout.lower()


@pytest.mark.parametrize(
    ("command", "schema", "payload_key"),
    [
        ("list-site-skills", "site-skill-list.v1", "skills"),
        ("inspect-site-skill", "site-skill-inspect.v1", "skill"),
        ("validate-site-skill", "site-skill-validation.v1", "skill"),
    ],
)
@pytest.mark.parametrize("bad_args", [["--unknown-option"], ["--root"]])
def test_site_skill_parser_failures_are_json_stdout_only(
    command: str, schema: str, payload_key: str, bad_args: list[str]
) -> None:
    result = runner.invoke(app, [command, "--json", *bad_args])

    assert result.exit_code != 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == schema
    failures = payload[payload_key]
    failure = failures[0] if isinstance(failures, list) else failures
    assert diagnostic_codes(failure) == {"parser.invalid"}


def test_validate_cli_rejects_path_combined_with_selectors() -> None:
    result = runner.invoke(
        app,
        [
            "validate-site-skill",
            "--package-path",
            str(EXAMPLE),
            "--site-key",
            "example-news",
        ],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output
