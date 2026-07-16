# Site Skill Capture Protocol

## Status and boundary

This document freezes four additive JSON contracts:

- `site-skill.v1`
- `capture-request.v1`
- `capture-result.v1`
- `acquisition-attempt.v2`

They define portable control and evidence records only. They do not change
`bootstrap-scope`, `run-scope`, storage, CLI, API, MCP, or database behavior.
`acquisition-profile.v1` and `capture-attempt.v1` remain unchanged compatibility
contracts.

All four models use Pydantic v2 with `extra="forbid"`, `strict=True`, and frozen
instances. Nested JSON objects and collections are also frozen after validation,
using immutable mappings and tuples rather than mutable built-in subclasses,
while remaining JSON-serializable. The immutable mapping rejects supported/public
mutation APIs and ordinary direct attribute assignment or deletion, including
assignment to its real mangled backing slot. This is an ordinary Python
immutability boundary, not a security claim against deliberate reflection with
`object.__setattr__`, which can bypass normal attribute handling on Python frozen
objects. Serialization remains validated under normal supported usage. Defaults
are validated under the same rules.
All `model_copy()` calls, including calls with no update or an empty update,
fully revalidate the resulting record,
including nested existing contract instances, cross-field invariants, and secret
checks. Ordinary validation also revalidates existing nested contract instances.
Pydantic's `model_construct`
remains an explicitly trusted escape hatch and must not be used for untrusted
protocol data. Schema versions are literals. Timestamps must be timezone-aware.
Identifiers reject empty and whitespace-only values. `config`, `metadata`, and
error metadata are portable, non-secret JSON: validation recursively rejects
secret-like keys including authorization, cookie, password, secret, token,
snake_case, kebab-case, and camelCase forms such as `api_key`, `api-key`,
`apiKey`, `APIKey`, `apikey`, `clientSecret`, `privateKey`, `refreshToken`,
`proxyAuth`, `proxyPassword`, and `sessionCookie`, plus proxy credential keys.
The conservative vocabulary first splits punctuation and camel case and rejects
normalized components named authorization, cookie, credential, key, password,
secret, or token. It also removes separators and applies a bounded suffix list:
compact names ending in `apikey`, `accesskey`, `accesskeyid`, authorization,
cookie, credential(s), password, secret, or token are rejected. The explicit
compact key categories also include `xapikey`, `privatekey`, `clientapikey`,
`accesskey`, `awsaccesskeyid`, and the compact
proxy-authentication names. This covers forms
such as `clientsecret`, `refreshtoken`, `sessioncookie`, `accesstoken`,
`OAUTHTOKEN`, `XAPIKEY`, `AWSAPIKEY`, `googleapikey`, `myaccesskey`, and
`myaccesskeyid` without attempting an open-ended secret vocabulary. Before
camel-case and separator classification, keys are normalized with Unicode NFKC,
so full-width and compatibility forms cannot bypass the same checks. This
normalization is detection-only: serialized keys retain their original spelling.
JSON numbers
must be finite. Request/result URL fields and
URI strings nested in portable JSON reject structurally detectable userinfo,
including scheme-relative network-path references. Detection first applies
Unicode NFKC compatibility normalization and treats backslashes as slashes, so
full-width delimiters and browser-style HTTP backslashes cannot obscure
userinfo. For the special `http` and `https` schemes only, detection also treats
zero or one slash after the scheme colon as an authority introducer, matching
browser parsing of forms such as `https:user:pass@example.com/path`,
`https:/user:pass@example.com/path`, and the equivalent single-backslash form.
These transformations are detection-only; accepted values retain their original
spelling for serialization. This boundary detects URI structure after those
transformations, not arbitrary secret-looking literal values or non-HTTP(S)
scheme-specific paths.

`forbid_secret_values=true` is also a producer and governance requirement. A
schema cannot reliably recognize every arbitrary literal secret value. The
schema-enforceable boundary is secret-like keys and credential-bearing URI
userinfo; producers must prevent, redact, or replace other secret values before
constructing these records.

## Executor identities

The accepted executor IDs are `web_http`, `browser_rendered`, `browseract`,
`sitemap`, `rss`, `cloakbrowser`, and `batch_python`.

`browser_rendered` remains the ID of the existing Playwright compatibility
adapter. `browseract` is a separate accepted protocol ID. This freeze does not
import, install, register, or execute a BrowserAct runtime, and does not alias it
to Playwright. Acceptance by a schema is not evidence that a runtime is
available.

The governed direct entrypoints are `Model.model_validate(...)` for Python input
and `Model.model_validate_json(...)` for complete wire JSON. Both reject
`strict=False` and reject `extra="allow"` or `extra="ignore"`; callers may omit
those overrides, use `strict=True`, and omit `extra` or use `extra="forbid"`.
Governed wire JSON is additionally fail-closed for object keys. Every object key
must be unique at every nesting level. A duplicate key is a parse error rather
than a last-value-wins override, including keys inside configuration, metadata,
and error metadata, for string, bytes, and bytearray input. Partial JSON
validation through `experimental_allow_partial` is explicitly unsupported at
this entrypoint, so truncated input cannot bypass duplicate-key validation.
Rebuilding a contract model's Pydantic core schema does not remove this
enforcement.

Generic or composite `TypeAdapter(...)` validation, including its Python and JSON
methods, is outside the protocol authority boundary. This package intentionally
leaves Pydantic's default `TypeAdapter` behavior untouched and does not
monkeypatch Pydantic. A `TypeAdapter` call must not be used to weaken governed
model policy. Callers receiving a list, mapping, tuple, union, or other envelope
must split or parse that envelope as appropriate and pass each governed record
to its model's direct `model_validate(...)` entrypoint, or pass each complete
wire JSON record to `model_validate_json(...)`. Only the latter carries this
package's duplicate-key guarantee.

## `site-skill.v1`

`SiteSkill` is the governed manifest, not merely an executor catalog. It carries
a canonical `MAJOR.MINOR.PATCH` `version`; top-level `status`, `runtime_requirements`, and
`secret_policy`; canonical unique `allowed_domains`; executor definitions; and
one or more recipes. The exact lifecycle statuses are `draft`, `probed`,
`reviewed`, `active`, and `deprecated`. Only `reviewed` and `active` are
production-eligible. `draft` and `probed` are not production-eligible, and a
`deprecated` skill must not be selected for new runs.

Versions contain exactly three dot-separated numeric components. Each component
is either `0` or starts with a non-zero digit, so `0.1.0` is valid and `01.0.0`
is invalid. This deliberately narrower grammar does not implement or claim full
Semantic Versioning; prerelease and build suffixes are not accepted.

For `site-skill.v1`, `secret_policy.forbid_secret_values` is required and must
be `true`. `allow_secret_references` explicitly controls whether references are
permitted. Optional `allowed_reference_schemes` must be non-empty when
references are allowed and empty when they are forbidden.

Each recipe has a unique `recipe_id`, enabled state, `executor_id`, profile
reference, Python entrypoint, literal `capture-result.v1` `output_contract`,
non-empty unique `required_capabilities`, and non-empty unique
`verification_rules`. The default recipe and executor must both be enabled and
must agree; every enabled recipe must use an enabled executor.

Artifact, script, profile, and entrypoint pointers use canonical portable POSIX
relative paths. Raw input is rejected for absolute paths, empty/dot/dotdot
components, duplicate separators, backslashes, whitespace padding, controls or
NUL, Windows-invalid filename characters (`<`, `>`, `:`, `"`, `|`, `?`, `*`),
or Windows reserved device names (`CON`, `PRN`,
`AUX`, `NUL`, `COM1` through `COM9`, and `LPT1` through `LPT9`) in any component,
including `CONIN$`, `CONOUT$`, Windows-recognized superscript `COM`/`LPT`
aliases, names with extensions, and components ending in a space or period.
Script and entrypoint paths end in `.py`; profile references end in
`.json`, `.yaml`, or `.yml`. The contract validates but never loads these paths.

## `capture-request.v1`

`CaptureRequest` binds `request_id`, an executor, an HTTP(S) URL, and a
timezone-aware request time to immutable reproducibility lineage: `site_key`,
`site_skill_id`, canonical three-component `site_skill_version`, lowercase SHA-256
`site_skill_digest`, `recipe_id`, `run_id`, and `scope_id`.

`site_skill_digest` is the lowercase hexadecimal SHA-256 digest of the exact
governed Site Skill artifact bytes as stored and distributed. Producers and
consumers hash those bytes directly; they must not parse and reserialize JSON
before hashing. A published artifact is immutable for its version: any byte
change requires a newly versioned artifact and its newly computed digest.

## `capture-result.v1`

`CaptureResult` repeats the complete immutable lineage so it remains useful as
a standalone evidence record. `finished_at` cannot precede `started_at`. A
`succeeded` result requires content and forbids an error; a `failed` result
requires an error and forbids content. Content includes inline text or a
canonical artifact pointer. Optional SHA-256 values are lowercase 64-character
hexadecimal strings.

## `acquisition-attempt.v2`

`AcquisitionAttempt` envelopes exactly one request and result. Every immutable
lineage field, request ID, and executor ID must agree, and capture cannot start
before the request timestamp.
An accepted attempt must have a succeeded result. A rejected attempt must state
an `acceptance_reason`. Acceptance means that the caller accepted the capture;
it does not change or infer crawler quality-gate behavior.

## Legacy mapping and retirement

The existing staged fields keep their current meaning:

| Legacy value | Contract executor | Current behavior |
|---|---|---|
| `fetch_mode=http` | `web_http` | Existing HTTP path. |
| `fetch_mode=browser` | `browser_rendered` | Existing Playwright compatibility path. |
| `fetch_mode=auto` | `web_http` | Keeps today's effective HTTP behavior; no new fallback is implied. |

`fetch_config_json` remains configuration for the legacy selected executor. No
legacy value maps to `browseract`. Any future bridge that copies
`fetch_config_json` into request `config` must recursively sanitize or redact
secret-bearing keys and proxy credentials before validation and copying; raw
legacy JSON must never be copied into these portable records.

Legacy `fetch_mode` and `fetch_config_json` may be retired only after all of the
following are measured in the default branch for two consecutive releases:

1. 100% of supported bootstrap/run inputs can carry a validated site-skill and
   capture request without losing legacy executor configuration.
2. Contract-vs-legacy shadow selection agrees for 100% of the repository's
   behavior-preservation fixtures, including `auto` selecting effective HTTP.
3. At least one migration release emits a documented deprecation diagnostic for
   every observed legacy read path, while the compatibility tests remain green.
4. A repository search and runtime telemetry show zero unowned legacy writers
   and zero legacy-only reads for 30 consecutive days.
5. A rollback test demonstrates that the last legacy-compatible release can
   consume the preserved inputs without a database or artifact migration.

Removal requires a separately reviewed runtime PR; this protocol freeze does
not start the retirement clock.

## Canonical fixtures

- [`site-skill-v1.sample.json`](../testing/fixtures/site-skill-v1.sample.json)
- [`capture-request-v1.sample.json`](../testing/fixtures/capture-request-v1.sample.json)
- [`capture-result-v1.sample.json`](../testing/fixtures/capture-result-v1.sample.json)
- [`acquisition-attempt-v2.sample.json`](../testing/fixtures/acquisition-attempt-v2.sample.json)
