# web_listening

`web_listening` 3.1 is a governed website-monitoring platform for human operators and AI agents. It discovers site structure, turns reviewed monitoring intent into bounded scopes, captures repeatable evidence, detects later changes, and exports stable machine and human handoff artifacts.

The supported 1.0 scope is complete and stable. Future changes follow the semantic-version policy below. The canonical product flow is:

```text
discover -> classify -> select -> plan-scope -> bootstrap/run -> report/export
```

The packaged `web-listening` CLI is the canonical operator and agent interface. The REST API provides site-level, acquisition, job, execution, report, document, and analysis surfaces, but it does **not** provide full REST parity for the discover/classify/select/plan-scope planning flow.

Version 1.1 adds a producer-only, robots-first diagnostic command for new Agentic planning paths. It does not change the supported 1.0 `discover -> classify -> select -> plan-scope` behavior or grant acquisition authority.

Version 1.2 adds strict, offline `access-policy.v1` and `access-decision.v1` contracts for future read-path migration. It does not migrate or change any current network, crawler, sitemap, download, REST, or MCP execution path.

Version 1.3 publishes the producer-confirmed `acquisition-manifest.v1` contract bundle and its strict offline validator. It freezes a future producer/consumer handoff without implementing acquisition runtime, immutable storage, retention, or manifest production.

Version 1.4 adds the shared governed access gateway core that enforces the frozen access contracts around pinned HTTP transport, robots caching, manual redirects, and atomic per-origin pacing/budgets. It is a pure caller-independent core; current crawler, search, sitemap, file, CLI, API, and MCP paths are not migrated by this release.

Version 2.0 migrates the supported target-content execution surface to that gateway. A reviewed scope, acquisition profile, resolved Site Skill, and non-empty compiled execution plan are now the only formal execution authority. One sealed preparation loads that authority, applies only non-enlarging runtime limits, compiles once, and admits the exact seed before any Storage, job, run, site, scope, report, temporary-file, or content mutation. The unchanged exact seed URL—not its canonical dedupe form—is carried with that admitted response into the first tree acquisition, so it is rebound to persisted IDs without a second target request or budget unit. All current-execution database and content-file changes remain in one transaction and creation journal until the traversal completes; a propagated robots rejection/error, gateway transport/origin/redirect/policy/budget failure, or bounded-body failure at the seed, a later page, or a later file rolls them back while preserving preexisting rows, bytes, reports, downloads, and job state. File rollback first atomically renames each lexical name into an exclusive same-parent transaction quarantine while its inode is pinned, then deletes only an identity match or restores a mismatch without clobbering; a restore collision preserves both the replacement and quarantined candidate and surfaces a rollback failure. Directory cleanup has separate explicit provenance: preexisting empty parents and ancestors are never pruned, while only no-follow directories first created by the current execution are held by exact live identity and processed deepest-first after file rollback. Each empty lexical directory is atomically no-clobber renamed under its pinned parent to an exclusive transaction-private quarantine name before identity verification and removal; a mismatch is restored without clobbering, and a restore or quarantine-name collision preserves every victim and surfaces cleanup failure. Missing or partial authority, request-identity overrides, attempts to enlarge reviewed limits, legacy tree execution, and direct browser/subprocess target reads fail before Storage or target mutation. This incompatible authority change is the Major release required by the semantic-version rubric below.

Version 3.0 establishes `immutable-artifact-store.v1`, the durable storage contract for the frozen `acquisition-manifest.v1` identity. Canonical response entity bytes now enter one MIME-validated SHA-256 CAS path for HTML, PDF, and explicitly supported attachments. Blob, logical source version, run observation, and lineage identities are separate; equal bytes from different URLs share only the blob, while same-source changed bytes create a new version without updating history. The frozen `artifact_id` remains the run-observation identity, and portable `artifact:sha256:<digest>` URIs—not local absolute paths—identify content. Deterministic at-rest gzip preserves exact decompression replay and the entity-byte digest. Publication and metadata use the existing atomic execution transaction and exact-file ownership journal, and retention rechecks reachability across new and legacy references before deletion. Existing document/blob tables remain readable and are not destructively migrated. Issue #52 does not produce the final acquisition manifest or add crawler/search orchestration; those remain separately gated.

Version 3.1 adds `agentic-site-rules.v1` and `agentic-orchestration.v1` as an additive Agentic exploration core. Strict versioned YAML binds exact origins, allow globs, optional/required search queries, depth/request/decoded-byte/file/concurrency/retry budgets, and allowed content types. Crawler and explicitly authorized search adapters can only propose inert candidates; the orchestrator performs every target-content read through the unified gateway and persists every successful original through `ArtifactStore`. Deterministic parent/child tasks, read observations, and replay links preserve rule, Site Skill, execution-plan, adapter, discovery, access-decision, redirect, artifact, and run lineage. Issue #53 does not create the final acquisition manifest, classification/knowledge-base/chunk/index/ready outputs, a second network authority, or a new live-site default.

## Supported interfaces and target-read paths

This inventory is authoritative for 3.1. “Supported read” means production target content may be consumed; planning evidence and offline fixtures are not target reads. Version 3.1 preserves the governed 2.0 read authority and immutable 3.0 storage semantics while adding bounded Agentic orchestration.

| Path | 3.1 status | Execution rule |
|---|---|---|
| Canonical `discover` section inventory | Supported planning read through a per-target gateway | Each reviewed catalog target is digest-bound to its own planning `AccessGateway`; robots rejection/error and every gateway or bounded-body failure propagate before inventory directories, YAML, or report creation, and successful planning reads grant no later execution authority. |
| Crawler HTML, XML, feed, and sitemap content | Supported through scoped `web_http` execution | Every URL, including a discovered candidate and every redirect hop, enters `AccessGateway`; redirects are never followed automatically. |
| PDF, attachment, and other document bytes | Supported through scoped `web_http` execution | One bounded gateway read supplies the exact bytes and SHA-256; rejection occurs before temporary files, blob writes, or document rows. |
| Search, fallback, and rescue candidates | Discovery/planning only until re-admitted | Candidate discovery does not grant read authority. A selected URL must re-enter a complete compiled scoped plan and the gateway before content is read. |
| Agentic crawler/search exploration | Additive bounded orchestration core | A strict `agentic-site-rules.v1`, exact Site Skill/execution-plan authority, and versioned adapters create deterministic candidate tasks. Adapters return URLs only; every content read uses the unified gateway, and every successful original uses the immutable artifact store. |
| Tree bootstrap and incremental crawling | Supported only from the governed staged scope flow | The supplied sealed authority, gateway, and accepted exact-URL seed are mandatory. Initial admission occurs before constructing `Storage`; the accepted response is consumed once at its unchanged URL, while a seed or later page/file rejection/error rolls back the entire current execution and its deferred API job. Rollback never removes a preexisting artifact directory; exact current-execution directory ownership is journaled separately. Canonical URLs are dedupe keys only. Direct bootstrap without an admitted seed rejects, and the legacy incremental wrapper is disabled in favor of `PreparedScopeExecution`. |
| Browser, Playwright, CloakBrowser, and BrowserAct target navigation | Disabled | These adapters cannot navigate target URLs in 2.0. BrowserAct executor and wrapper calls fail locally before a target wrapper, stealth, or browser-open process can spawn; only the separate version/runtime/help inspection probes may execute. The adapters are not supported content readers. |
| Legacy `Crawler`/`DocumentProcessor` direct-client construction | Disabled in production | A gateway is required. The `httpx.MockTransport` compatibility seam is offline-test-only and itself wraps `AccessGateway`; it is not a production transport option. |
| `diagnose-site` robots/sitemap reads | Explicitly isolated planning producer | It uses its separate bounded, pinned, robots-first diagnostic transport, never fetches page candidates, mutates no execution scope, and grants no acquisition authority. |

The supported execution interfaces are deliberately aligned:

| Interface | Formal target-read entrypoint | Required authority | Reject/error result |
|---|---|---|---|
| CLI | `bootstrap-scope`, `run-scope` | Explicit scope path and acquisition-profile path; scope bindings resolve the Site Skill and compiled plan | Robots-policy reject/error uses exact frozen `access-rejection-error.v1`; later gateway/body failure uses separate stable `governed-read-error.v1`; JSON mode exits nonzero. |
| REST API | `POST /api/v1/monitor-scopes/{scope_id}/bootstrap` and `/run` | Request body supplies `scope_path` and `acquisition_profile_path`; the one sealed authority is admitted before job/Storage mutation and handed unchanged into execution | HTTP 403/502 with the applicable exact robots envelope, or HTTP 502 with the separate governed-read error; neither creates a job row. |
| MCP | `web_listening_bootstrap_scope`, `web_listening_run_scope` | Explicit `scope_path` and required `acquisition_profile_path` | The same exact robots envelope/reason code or separate governed-read error payload as CLI/API. |

The site-level `check`/`download-docs`, one-off acquisition probe/fallback/rescue, legacy tree wrappers, and their REST/MCP compatibility surfaces do not form a second target-read authority. In 2.0 they may inspect stored/planning evidence or report that governed authority is required, but they cannot bypass the scoped gateway. CLI remains canonical for `discover -> classify -> select -> plan-scope`; REST does not add planning authority.

## Robots and sitemap diagnosis

Before a new Agentic planning path proposes discovery or acquisition, run a bounded diagnosis with exact operator-supplied network boundaries:

```bash
web-listening diagnose-site \
  --url https://example.com/news \
  --site-key example \
  --allowed-domain example.com \
  --allowed-document-origin https://example.com \
  --output data/plans/site_diagnostic_example_2026-08-08.json \
  --json
```

`diagnose-site` always normalizes the requested origin and makes its first HTTP request to that canonical origin's `/robots.txt`. It then evaluates declared sitemap locations, or the single same-origin `/sitemap.xml` fallback when robots is absent or declares none. Sitemap indexes are processed FIFO. A cross-origin sitemap document is fetched only when its exact scheme/host/effective port was supplied in `--allowed-document-origin` and that exact origin has first passed its own robots preflight with the same identity. Page `<loc>` values are planning seeds only: the command never fetches them, accepts only canonical-origin page seeds, and records cross-origin pages as requiring a separate diagnosis.

The production transport is browser-free, proxy-free, credential-free, and fail-closed. Every robots, retry, redirect, and sitemap request repeats exact-origin gating and all-address public DNS validation, connects only to the validated address set, preserves the normalized `Host` and HTTPS SNI/certificate hostname, and verifies the actual public peer before sending HTTP request bytes. Canonical public IPv4/IPv6 literals are supported as allowed hosts (IPv6 URL and `Host` authorities remain bracketed); private, loopback, link-local, reserved, multicast, and unspecified literals are rejected before the first HTTP byte. Redirects cannot expand authority or downgrade HTTPS. Governed non-2xx status outcomes—including authority, empty, redirect, retryable, and terminal classes—are decided before body reads. Only 2xx response bodies are streamed under wire and decoded limits with bounded single-member gzip handling; unsafe XML constructs and non-sitemap roots are rejected.

The resulting `site-diagnostic.v1` is planning evidence, not permission, operator review, or an execution profile. Each origin policy includes the selected ordered `Allow`/`Disallow` rules, source line numbers, robots digest, and identity digest; `policy_id` and `policy_sha256` are recomputed from that visible evidence so consumers can verify and replay the exact matching policy. Accepted page seeds carry their source sitemap queue ordinal, parent document digest, and source entry ordinal. Rejected scheduled sitemap documents likewise retain a normalized URL or raw rejected value, reason, queue ordinal, parent digest, and source entry ordinal; rejected scheduling consumes a bounded request slot without opening the network. Request-slot ordinals and non-secret counted-occurrence lineage let readers recompute the HTTP request, sitemap-document, and URL occurrence usage rather than trusting aggregate counters. Each accepted redirect attempt records its canonical, approved `redirect_target_url`; the next attempt and any redirect-policy rejection are bound to that exact target rather than merely its origin. The contract validates FIFO sitemap evidence, robots-to-root and index-to-child digest lineage, and requires a sitemap-seeded recommendation to contain matching accepted evidence. The artifact's canonical SHA-256 excludes only `artifact_sha256`; readers must verify that digest and freshness. Writes are atomic and idempotent, and refuse to replace a different existing artifact. Diagnosis remains separate from `discover`, REST, MCP, and scope/profile/Site Skill execution authority.

When `--output` is omitted, the CLI derives a safe filename component from `site_key` and includes the generated `diagnostic_id`, so separate same-day diagnoses do not collide. An explicit `--output` remains no-overwrite and may be repeated only for the byte-identical artifact.

## Access policy and decision contracts

`access-policy.v1` freezes one canonical-origin robots observation for one transparent identity. The identity contains `identity_id`, the actual `user_agent`, the RFC 9309 `product_token`, and a recomputable SHA-256 over exactly those three visible fields. The policy embeds and revalidates the existing strict `site-diagnostic.v1` `OriginPolicyEvidence` model for valid 200 and 404 observations; it does not copy or reinterpret the diagnostic rule model. The embedded evidence must match origin, identity, robots digest, observation time, expiry, policy ID, and policy digest. Other observations require that field to be JSON `null`. Before an access artifact can be serialized, every access URL—including declared sitemap URLs inside the embedded evidence—is checked for NFKC-normalized delimiter, compact, and camel-case secret-key forms. Every frozen compact exact credential name also remains secret-like when appended to a namespace, including `privatekey`, `proxyauth`, `proxyuser`, and `proxyusername`. Query inspection covers raw plus two percent-decoding passes; each inspection copy is NFKC-normalized before treating `?`, `&`, `;`, and `=` as key boundaries, so encoded or raw fullwidth delimiters and nested decoded URL queries cannot hide credential keys. Identity/evidence free-text inspection likewise covers the raw, once-percent-decoded, and twice-percent-decoded NFKC copies. It rejects header assignments and credential-key/value pairs separated by `:`, `=`, whitespace, or true punctuation boundaries, including a credential token nested after an earlier non-secret assignment; token inspection has no arbitrary key-length cutoff and scans each complete token in a fixed linear pass. URI-userinfo inspection checks the same three NFKC copies for every HTTP(S), SOCKS/SOCKS4/SOCKS4a/SOCKS5/SOCKS5h, and boundary-qualified `//` network-path authority. Network paths may begin after governed assignment/separator/text punctuation or RFC-invalid text punctuation such as `|`, `^`, and backtick, but not in the middle of a path or scheme token. Those RFC-invalid characters are paired as both candidate-start boundaries and authority terminators, so an invalid separator inside the candidate cannot expose a later `@`. RFC-valid userinfo characters such as `(`, `)`, and `'` do not prematurely end an authority inspection; only genuine authority terminators, whitespace/control, or invalid text URI boundaries do. The work is fixed bounded linear scanning, with backslash and special-scheme normalization, rather than recursive decoding. This access-layer validation does not change the diagnostic producer contract.

Access query canonicalization preserves parameter order and reserved percent-encoding semantics. A canonical query contains only visible ASCII RFC 3986 query characters or well-formed percent triplets; percent hex digits are uppercase, percent-encoded unreserved bytes are written literally, and percent-encoded reserved/non-unreserved bytes remain encoded. Raw whitespace, controls, non-ASCII, malformed escapes, and other non-query ASCII characters fail closed. The submitted URL must already equal this complete canonical representation; validation never silently rewrites a signed artifact.

The access-policy cache-key digest is SHA-256 over canonical JSON containing exactly `canonical_origin` as its canonical origin URL string, `identity_sha256`, and fixed `policy_version: access-policy.v1`. The policy SHA-256 covers every policy field except `policy_id` and `policy_sha256`; `policy_id` is `access-policy-` plus the first 16 digest characters. Diagnostic artifact digest and origin-policy references are evidence bindings only: `site-diagnostic.v1` remains planning evidence and never becomes execution authority by itself.

`access-decision.v1` evaluates one canonical URL under an embedded, fully revalidated `access-policy.v1`. Its decision SHA-256 covers every decision field except `decision_id` and `decision_sha256`; `decision_id` is `access-decision-` plus the first 16 digest characters. A decision is fresh only from the policy observation time through its expiry. It records the decision time, exact rule source, longest matching source-line numbers, and non-sensitive digest/origin/freshness/robots-observation evidence. Each consumed redirect request contains a finite, digest-bound `access-decision-proof.v1` allow proof with its exact canonical source URL/origin, complete revalidated policy and evidence, decision time, disposition, request-slot reservation, and per-origin pacing/budget reservation. The hop separately binds request start, response observation, redirect status, and canonical target URL/origin. Every allow reservation's earliest execution time (`not_before`) must remain within its bound policy authority through `expires_at`. Every consumed redirect request must start at or after `not_before`, inside that reservation's half-open budget window, and no later than the bound policy expiry. Proof decision/reservation, request, response, the next proof decision, and the final decision must form a strictly causal sequence; source/target continuity and contiguous request-slot ordinals are mandatory, and HTTPS downgrade redirects fail validation. Proofs do not embed prior redirect history, so the artifact remains finite and deterministic.

The robots matrix is fixed as follows. Every reject or error fails closed; retryability is part of the contract rather than caller policy.

| Observation | Required policy evidence | Outcome | Reason code | Retryable | Rule source |
|---|---|---|---|---|---|
| Valid 200, selected rules allow | available `OriginPolicyEvidence` | `allow` | `robots.allowed` | no | `origin_policy_evidence` |
| Valid 200, selected rules disallow | available `OriginPolicyEvidence` | `reject` | `robots.disallowed` | no | `origin_policy_evidence` |
| 404 | `OriginPolicyEvidence` with `robots_status: absent` | `allow` | `robots.absent` | no | `robots_absent` |
| 401 | JSON `null` | `reject` | `robots.auth_required` | no | `http_status` |
| 403 | JSON `null` | `reject` | `robots.forbidden` | no | `http_status` |
| Timeout | JSON `null` | `error` | `robots.timeout` | yes | `transport` |
| DNS failure | JSON `null` | `error` | `robots.dns_error` | yes | `transport` |
| Network failure | JSON `null` | `error` | `robots.network_error` | yes | `transport` |
| Parse failure | JSON `null` | `error` | `robots.parse_error` | no | `parser` |

Only `allow` decisions may reserve the target request. They require both a final `request_slot_reservation` and a matching `origin_reservation`; reject and error decisions require both fields to be JSON `null`. The origin reservation freezes reservation/pacing times, pacing interval, budget window, limit, prior usage, one reserved unit, and its per-origin budget ordinal. Its active budget window is half-open: `budget_window_started_at <= reserved_at < window_end`, and `not_before` must also remain inside that window. Pacing lineage is tracked by canonical origin independently of budget-window identity: every later same-origin reservation preserves the pacing interval and schedules `not_before` no earlier than the prior same-origin request start plus that interval. Exact budget windows preserve their limit and monotonically advance prior usage. Same-origin windows with different shapes must not overlap; only a genuinely later non-overlapping window may begin an independently proven budget lineage. Different origins still require independent complete proofs.

Reservation arithmetic has fixed portable maxima: timestamps are no later than `9998-12-31T23:59:59.999999Z`, request/hop/budget ordinals are at most `1,000,000`, pacing is at most `86,400,000` milliseconds, a budget window is at most `86,400` seconds, and the budget limit and prior usage are each at most `1,000,000`. Arithmetic outside those bounds is a governed validation error and the offline CLI emits the shared canonical error envelope. Version 1.2 froze these fields, version 1.4 added the shared gateway core, and version 2.0 applies them to supported target reads without changing their semantics. Reject and error decisions carry the same strict `access-rejection-error.v1` envelope consumed by CLI/API/MCP, and that envelope is also a standalone loadable access contract. Its evidence independently requires typed non-sensitive IDs derived from their policy digests, a cache key recomputed from canonical origin + identity digest + fixed policy version, zero-to-24-hour ordered freshness, an all-null or all-present origin-policy ID/digest/robots-digest trio, and the exact reason/outcome/retryability/robots-observation/evidence-nullability matrix. `contract.invalid` alone requires null evidence.

### Shared governed access gateway core

`web_listening.blocks.access_gateway.AccessGateway` is the runtime core for every supported 2.0 target-content read. Its configuration binds one transparent identity, a non-empty set of exact allowed origins, the compiled-authority digest, policy TTL, redirect-hop cap, pacing interval, and hard budget window/limit. The default transport is the existing browser-free, proxy-free `SafePinnedTransport`; clients cannot auto-follow redirects. Every content request—including each consumed redirect source—therefore performs fresh all-address public DNS validation, connects only to the pinned set, preserves HTTPS SNI and the normalized `Host`, and verifies the actual public peer before HTTP bytes.

Robots policies are cached under exactly the frozen SHA-256 of canonical origin + transparent identity digest + `access-policy.v1`. Cache entries obey their contract expiry, support exact-origin or complete explicit invalidation, and use per-key concurrent single-flight; an invalidation racing an in-flight fetch prevents the stale result from repopulating the cache. Valid 200, 404, 401, 403, timeout, DNS/network, and parse outcomes are built only through the frozen access-policy and access-decision constructors. Redirect targets repeat canonical URL, exact-origin, HTTPS downgrade, cached robots-policy, reservation, and pinned-transport checks before their request; the explicit hop cap counts consumed redirect responses exactly. The gateway carries the canonical final-hop URL into the bounded body consumer, so suffix-based compression checks apply to the admitted final response rather than the original pre-redirect URL.

An allow atomically reserves one irreversible budget unit and its per-origin pacing slot before opening the target content request. Same-origin request starts are serialized through the reservation/start boundary, while different origins use independent locks and budgets. Once reserved, a unit remains consumed after cancellation, timeout, transport failure, redirect rejection, or consumer error so retries cannot amplify the hard budget; every path still releases the origin lock and closes any returned response. The core itself creates no files or temporary artifacts and invokes the caller's response consumer only after an allow decision. Robots reject/error decisions return strict `access-decision.v1` with no target request. Origin, SSRF/peer, downgrade, redirect-cap, and hard-budget failures remain typed fail-closed gateway errors because version 1.2 defines no valid decision reason code for them; the gateway does not misuse `contract.invalid` or invent parallel semantics.

Validate or inspect either committed or producer-generated contract without network access:

```bash
web-listening validate-access-contract --path docs/testing/fixtures/access-decision-v1.sample.json --json
```

With `--json`, contract validation failures and command-parser/path failures (including a missing, nonexistent, directory, or unreadable `--path`) emit exactly one compact canonical `access-rejection-error.v1` envelope with `contract.invalid`; human mode retains the usual Typer diagnostics.

Parsing is strict and fail-closed: unknown fields, duplicate JSON keys, excessive JSON nesting, wrong required/null fields or enums, non-canonical URLs/origins/queries, stale evidence, raw/encoded/nested/fullwidth-delimiter secret-key URLs, namespaced NFKC/compact/camel secret-key forms, raw/once/twice-percent-encoded header or credential-key free text (including long HTTP tokens), nested delimiter- or whitespace-shaped secret-bearing evidence/identity text, raw/percent/double-percent HTTP/SOCKS/network-path userinfo (including pipe/text-punctuation-delimited network paths), sensitive or matrix-conflicting standalone envelope evidence, missing or tampered per-hop proofs, out-of-authority or out-of-window request timing, overlapping/redefined budget windows, broken origin pacing or reservation lineage, extreme numeric/time arithmetic, and identity/cache/policy/decision digest tampering are rejected. Repeated validation is idempotent and does not mutate the input. Model pre-parsing and the offline loader convert parser recursion into governed validation errors; JSON CLI mode still emits exactly one canonical `contract.invalid` envelope.

## Acquisition manifest contract bundle

The producer authority for the future cross-repository handoff is the exact three-file bundle under `contracts/acquisition-manifest.v1/`: `schema.json`, `fixture.json`, and `producer-confirmation.json`. The schema identifies `acquisition-manifest.v1`, uses JSON Schema draft 2020-12, closes every normative object shape, and uses `source_run_id` consistently. The confirmation binds the exact repository, Issues #46 and #48, producer-confirmed scope, and SHA-256 digests of the committed UTF-8 LF schema and fixture bytes.

The contract separates blob identity from observation identity. `retrieval.sha256` identifies content bytes and may therefore be shared by different URLs. A stable `artifact_id` is `artifact-` plus the first 24 hexadecimal characters of SHA-256 over canonical JSON containing exactly `manifest_version`, `source_run_id`, `normalized_source_identity`, and the nullable retrieval `sha256`. Consequently, a later body at the same normalized source and identical bytes at different normalized sources remain distinct observations. Stable artifact IDs bind parent, source, discovered-from, and derived lineage. Portable content locations use `artifact:sha256:<digest>` URIs rather than machine paths.

Each observation records requested, source, and final URLs; the complete ordered redirect chain; a stable `access_decision_id` reference to the 1.2 access contract; acquisition adapter/version; discovery and lineage; retrieval time, HTTP status, MIME type, size, SHA-256, wire/content encoding, and portable artifact URI. Run and artifact status enums independently support `completed`, `partial`, `rejected`, and `failed`. The producer-confirmed fixture covers HTML, PDF, derived Markdown, parent/child lineage, allowed and rejected redirects, same-URL new content, different-URL identical bytes, shared blob URIs, and idempotent no-mutation replay. It contains no production data or credential material.

Validate the canonical bundle or an exact vendored copy without network or runtime activity:

```bash
web-listening validate-acquisition-contract \
  --bundle-path contracts/acquisition-manifest.v1 \
  --json
```

The command emits canonical `acquisition-contract-validation.v1` JSON. Missing, unreadable, corrupt, non-LF, digest-mismatched, identity-mismatched, unknown-file, unknown-shape, sample-only, old-version, sensitive, broken-lineage, and invalid-replay bundles fail closed with stable reason codes. Validation verifies the schema meta-contract, fixture, producer identity, exact byte hashes, artifact identities, redirect continuity, lineage, portable blob URIs, coverage assertions, and repeatability without modifying any bundle byte. The existing `web-listening-manifest.v1` is a separate compatibility export and cannot substitute for this contract.

## Product model and authority

The system separates three kinds of input:

- **Monitoring intent and scope**: section inventories, classifications, reviewed selections, monitor tasks, and compiled monitor scopes define what to observe.
- **Acquisition authority**: an `acquisition-profile.v1` defines quality gates, allowed domains, adapter availability, and safety approvals.
- **Site-specific authority**: a versioned `site-skill.v1` package defines governed domains, recipes, executor bindings, scripts, capabilities, and verification rules.

Formal `bootstrap-scope` and `run-scope` execution requires an acquisition profile and the complete six-field Site Skill binding in `monitor_scope.yaml`:

1. `acquisition_profile_id`
2. `site_skill_version`
3. `site_skill_package_sha256`
4. `site_skill_recipe_id`
5. `site_skill_script_sha256`
6. `executor_version`

The package resolves and validates the exact Site Skill, applies requested runtime limits without allowing them to enlarge the reviewed scope, compiles one non-empty `acquisition-execution-plan.v1`, verifies executor capability and runtime policy, and constructs the gateway **before opening Storage or mutating state**. The gateway body ceiling and transport timeout come strictly from the sealed `web_http` step's `stdout_bytes` and `timeout_seconds`; missing, invalid, or inconsistent step limits fail closed before a target consumer or write. That same sealed preparation owns the compiled plan, gateway, exact target, and admitted seed from initial admission through execution; scope/profile files are not reloaded and authority is not recompiled after bookkeeping begins. CLI bootstrap/run prepare once and use `artifacts.plan` after execution rather than performing a preliminary scope load. The compiled plan—not picker metadata, a probe result, `fetch_mode`, or `fetch_config_json`—is formal executor authority. Partial governed bindings fail closed. Legacy fetch fields retain compatibility and lineage meaning only.

Packaged Site Skills are discovered and validated statically: registry inspection does not import scripts, execute code, access the network, or resolve DNS. Package versions and SHA-256 digests make the selected authority reproducible.

### Agentic exploration rules and task state

`load_agentic_site_rules` accepts only strict, duplicate-key-free UTF-8 YAML with `schema_version: agentic-site-rules.v1`. Its semantic SHA-256 binds the rule ID/version, site key, exact seed URLs and origins, URL/path allow globs, required/optional query declarations, all six hard budgets, and the MIME allowlist. URLs use the same canonical credential-free validation as the access contracts. The committed sample is [agentic-site-rules-v1.sample.yaml](docs/testing/fixtures/agentic-site-rules-v1.sample.yaml).

`prepare_agentic_authority` recompiles the complete canonical acquisition plan from the trusted scope, profile, resolved `SiteSkill`, and executor-registry capability, verifies every provenance algorithm, fingerprint, step, recipe, entrypoint, script, limit, capability, and top-to-first-step binding, and admits only an exact `web_http`/`http_get` execution before sealing it to one concrete `AccessGateway` and `ArtifactStore`. Browser-rendered, cloaking, browser-action, and shaped executor plans cannot enter Agentic storage or I/O. Production preparation accepts only the exact `SafePinnedTransport` type and seals its identity, timeout/chunk configuration, gateway callables, class request behavior, and address resolver; its timeout must equal the compiled step timeout and its positive chunk size is capped by both the body ceiling and the implementation safety ceiling. Instance-shadowed or changed class methods fail before I/O. The offline seam separately seals the exact mock wrapper, client, `httpx.MockTransport`, handler identity/call behavior, and every inner gateway. Its legacy direct-use mode retains the historical synthetic `/robots.txt` 404 without invoking the target-only handler; Agentic preparation performs a one-way switch to handler-driven robots and seals that exact mode before any read. The seal also retains the exact semantic monitor scope (seed, homepage, origins, page/file prefixes, and budgets) and immutable-store Storage identity, resolved root, encoding, MIME policy, and store/read call paths; each run and read revalidates them before I/O or a write. `AgenticOrchestrator` accepts only that non-replaceable prepared capability; caller-supplied identities, forged plan dataclasses, subclasses, post-prepare swaps, and reader/store-shaped substitutes fail closed. Rules, seeds, allow patterns, candidates, and final URLs may only narrow the compiled scope predicate, resolved skill origins, plan budgets, gateway ceiling, and immutable store MIME policy. Immediately before every target send, including a same-origin redirect, the current URL must still satisfy both the site rules and the compiled page-or-file scope; the final governed URL is then classified from its admitted MIME type and checked against the corresponding page or file prefix. Successful pages and files use separate durable counters and their respective plan/rule ceilings. The crawler adapter is a pure post-read candidate extractor. A search adapter must carry explicit authorization plus a stable identity/SemVer and may return only typed candidate URLs and discovery provenance. Adapter invocation and bounded candidate materialization are the only ordinary-`Exception` boundary: call- or iteration-time adapter faults become stable search/crawler results, while `BaseException` and repository scheduling failures still propagate. The orchestrator snapshots each adapter object, identity/version, authorization, and exact bound callable before invocation and revalidates it after call and lazy materialization, before persistence or scheduling. Candidate type validation and sorting remain bounded; malformed or identity-drifting crawler output becomes a stable partial warning without gaining read authority. Candidates outside the exact origin/glob/depth boundary are rejected before a target request. The scheduler is deterministic and currently executes one child at a time. A persisted monotonic lease epoch fences every run mutation and atomic compare-and-reserve counter, hard-bounding concurrency, decoded response bytes, successful originals, retries, and depth; an expired owner cannot send, write, or clear its successor's lease. The run request budget is consumed atomically immediately before every target-content transport send, including redirect hops and retry attempts; robots-policy fetches are gateway authority work and are explicitly excluded from this run-level target counter. A caller-specific per-read limit narrows, but never enlarges, the sealed gateway body ceiling.

The versioned durable `agentic_runs`, `agentic_tasks`, and `agentic_observations` ledger is currently `agentic-ledger.v2`. A read-only version/shape preflight runs before any schema, index, or trigger statement and verifies every current column's order, name, type, null/default/primary-key flags plus the exact table, index, uniqueness, and trigger object kinds: only an object-free bootstrap, exact v2, or structurally valid v1 may enter one atomic schema transaction. Unknown/future markers, malformed or renamed metadata, alien layouts, and missing required legacy columns fail with zero schema mutation. The migration then backfills genuine terminal v1 rows from immutable artifact evidence and validates every parent, task, replay, contiguous attempt, terminal result, and artifact reference before changing the marker or replacing guards; running v1 runs are explicitly rejected as non-migratable. The ledger uses stable digest-derived parent, child, and observation IDs and performs the same strict identity/reference validation on load. Observations may be inserted only while both their run and task are running. Terminal finalization and replay revalidate coherent exactly-derived outcomes, the exact final completed observation, matching task artifact/access evidence, and an existing immutable artifact with the same run, requested identity, final URL, adapter, and access decision. Every read observation binds its run, parent task, child task, discovery kind/source/parent artifact, adapter, requested/current/final URL, response status when present, a closed canonical redirect chain, stable access-decision ID when the gateway produced one, result reason, and immutable artifact ID when successful. Before an access decision exists, only a finite set of initial global-budget, pre-reservation policy/budget, or robots safety/transport failures may carry a null decision; unknown transport kinds collapse to the single safe `gateway.transport.unclassified_transport` reason, while known retryable connect/disconnect failures retain bounded attempt accounting. Decisionless failures retain the exact current/final endpoint and accepted-hop proof IDs and release any active read reservation. Body, response-metadata, redirect, and post-reservation policy failures likewise retain their original governed decision, endpoint, response status when present, and redirect context. Governed reads preserve truthful wire/decoded byte and content-encoding evidence plus a bounded safe `Content-Disposition` filename; artifact, usage, observation, and terminal task state commit together or roll back together. Required and optional children are atomically created and sealed before execution. A parent cannot become `completed` until every required child is terminal and successful; optional failure yields `partial` with `optional_child_failed`. Terminal run/task outcomes are `completed`, `partial`, `rejected`, `failed`, and `cancelled`, and a terminal row cannot be mutated. Post-artifact cancellation or another `BaseException` records a deterministic global parent outcome even when all children are already terminal; lease recovery atomically clears stale active-read reservations, and a secondary SQLite bookkeeping error never masks the original interruption. Budget exhaustion, cancellation, retry attempts, crawler/search/store failure, and cross-run replay retain bounded stable evidence without exception messages, credentials, host paths, or response bodies in the task ledger. Repeating an already-terminal run with the same exact authority is read-free and observation-idempotent; a new run may name a terminal, rules/Site-Skill/plan/authority-compatible `replay_of_run_id`, and each replay link must bind the same task kind/key inside that exact source run.

## Install

Python 3.12.x is required. Create a fresh environment with an approved 3.12 interpreter.

Linux/macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Optional installations are additive:

```bash
# Playwright rendered-browser support
python -m pip install -e ".[browser]"
python -m playwright install chromium

# Explicitly authorized CloakBrowser probing
python -m pip install -e ".[cloakbrowser]"

# MCP stdio server without the development extra
python -m pip install -e ".[mcp]"
```

There is no `core` extra: `python -m pip install .` installs the base package. BrowserAct is not a project dependency and must not be installed in the project environment; see [Version and Runtime Compatibility](#version-and-runtime-compatibility).

## Configuration

Copy `.env.example` to `.env` (`cp .env.example .env` on POSIX or `Copy-Item .env.example .env` in PowerShell).

| Variable | Default | Purpose |
|---|---|---|
| `WL_DATA_DIR` | `./data` | Control, report, and evidence root |
| `WL_DB_PATH` | `./data/web_listening.db` | SQLite database |
| `WL_DOWNLOADS_DIR` | `./data/downloads` | Download storage root |
| `WL_OPENAI_API_KEY` | empty | Optional OpenAI-backed explanation/summary only |
| `WL_OPENAI_MODEL` | `gpt-4o-mini` | Optional explanation model |
| `WL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `WL_USER_AGENT` | `web-listening-bot/1.0` | Default HTTP user agent |
| `WL_REQUEST_TIMEOUT` | `30` | Request timeout in seconds |

Discovery, crawling, downloads, SHA-256 deduplication, reports, and manifests do not require an OpenAI key.

## Quick start

A new catalog must be initialized through review rather than sent directly to production acquisition:

1. Run broad smoke/tree validation to identify reachable, blocked, thin-HTML, or section-seed-sensitive sites.
2. Run discovery and classification.
3. Generate draft selections and scopes, using profiles such as `blocked_hold`, `thin_html_watch`, `section_news`, `section_documents`, or `homepage_standard` where useful.
4. Have an operator review and confirm the monitoring boundary.
5. Bind the confirmed scope to the exact acquisition profile and Site Skill authority.
6. Preview the execution plan, then bootstrap the baseline.
7. Run incremental checks and export reports/manifests.

Draft selection and scope files are review artifacts, not implicit production approval.

After discovery, classification, and operator review have produced a selection, scope, and acquisition profile, use their actual paths in the canonical flow below:

```bash
SITE_KEY=example
RUN_DATE="$(date +%F)"
SELECTION_PATH="data/plans/section_selection_${SITE_KEY}_${RUN_DATE}.yaml"
SCOPE_PATH="data/plans/monitor_scope_${SITE_KEY}_${RUN_DATE}.yaml"
PROFILE_PATH="data/plans/acquisition_profile_${SITE_KEY}_${RUN_DATE}.yaml"

web-listening discover --catalog dev
web-listening classify --catalog dev
web-listening select --selection-path "$SELECTION_PATH"
web-listening plan-scope --selection-path "$SELECTION_PATH" --yaml-path "$SCOPE_PATH"
web-listening preview-execution-plan \
  --scope-path "$SCOPE_PATH" \
  --profile-path "$PROFILE_PATH" \
  --json
web-listening bootstrap-scope \
  --scope-path "$SCOPE_PATH" \
  --acquisition-profile-path "$PROFILE_PATH" \
  --download-files --include-summary
web-listening run-scope \
  --scope-path "$SCOPE_PATH" \
  --acquisition-profile-path "$PROFILE_PATH" \
  --download-files
web-listening report-scope --scope-path "$SCOPE_PATH"
web-listening export-manifest --scope-path "$SCOPE_PATH"
```

`SITE_KEY`, `RUN_DATE`, and all three paths above are templates: replace them with the artifacts generated and approved for the target site. Use `build-acquisition-profile --site-key ... --allowed-domain ... --output "$PROFILE_PATH"` to create the draft profile before review and Site Skill binding.

Use `web-listening COMMAND --help` for complete options. The lower-level `tools/*.py` programs remain compatibility/developer wrappers, not a second product authority.

## Full CLI inventory

### Canonical staged workflow

- `discover` — inventory reachable site sections.
- `classify` — attach categories and priority hints.
- `select` — inspect a reviewed section selection.
- `plan-scope` — compile a selection into a monitor scope.
- `bootstrap-scope` — create a governed baseline for the selected scope.
- `run-scope` — perform a later governed change-detection run.
- `report-scope` — produce a scope tracking report.
- `export-manifest` — export document and `web-listening-manifest.v1` handoff artifacts.
- `create-monitor-task` — create the monitoring-intent artifact.
- `export-tracking-report` — export tracking output from stored evidence.

### Governance and acquisition

- `diagnose-site` — probe robots.txt first and emit bounded, digest-verifiable sitemap planning evidence; it does not run discovery or grant authority.
- `validate-access-contract` — strictly validate and inspect one local access policy or decision artifact offline.
- `validate-acquisition-contract` — validate the canonical producer-confirmed acquisition manifest contract bundle offline without mutation.
- `list-site-skills`, `inspect-site-skill`, `validate-site-skill` — statically inspect governed Site Skill packages.
- `list-acquisition-tools` — return the stable acquisition picker catalog.
- `build-acquisition-profile` — create a reviewed profile input.
- `probe-acquisition` — inspect a candidate adapter/profile as planning evidence; target-content probing is disabled without formal scoped gateway authority.
- `preview-execution-plan` — compile and inspect formal authority without executing it.
- `inspect-browseract` — perform the isolated, read-only BrowserAct identity/capability handshake.

### Jobs and delivery

- `list-jobs`, `get-job` — inspect persisted job state, progress, artifacts, and delivery envelopes.

### Site-level compatibility and service

- `add-site`, `list-sites` — manage monitored sites.
- `check`, `list-changes` — inspect compatibility monitoring and stored changes; `check` is not a 2.0 target-read authority.
- `download-docs`, `list-docs` — inspect stored documents; direct acquisition is disabled without formal scoped gateway authority.
- `analyze` — generate analysis from stored evidence.
- `serve` — run the FastAPI service.

## MCP server

Install the `mcp` extra and run `web-listening-mcp` for stdio transport. The server exposes exactly ten thin wrappers around shared package services:

1. `web_listening_list_acquisition_tools`
2. `web_listening_probe_tool_once`
3. `web_listening_recommend_next_tool`
4. `web_listening_acquire_with_fallback`
5. `web_listening_bootstrap_scope`
6. `web_listening_run_scope`
7. `web_listening_report_scope`
8. `web_listening_export_manifest`
9. `web_listening_get_job`
10. `web_listening_read_artifact`

MCP tool responses use the stable `web-listening-tool-result.v1` envelope where applicable. `web_listening_bootstrap_scope` and `web_listening_run_scope` require `acquisition_profile_path`; a robots-policy rejection/error instead returns the exact frozen `access-rejection-error.v1` envelope, while a non-robots gateway or bounded-body execution failure returns the independent `governed-read-error.v1` payload without inventing a frozen access-decision reason code. Acquisition fallback and recommendation surfaces can discover or rank candidates, but cannot read target content or supersede governed scope authority.

## REST API

Run `web-listening serve`; routes are under `/api/v1`. Current API groups are:

- **Acquisition**: tool catalog, default profile building, one-off probes, and execution-plan preview.
- **Sites**: create/list/get/deactivate sites, latest snapshots, rescue checks, and queued checks.
- **Jobs and delivery**: monitor-task creation, job status/payload retrieval, and a job-delivery webhook registration stub.
- **Scoped execution and artifacts**: bootstrap/run require explicit scope/profile paths and validate complete authority before job persistence; report jobs plus latest report and manifest retrieval operate on stored evidence.
- **Evidence and analysis**: changes, documents, document-content updates/downloads, analysis creation, and analysis listing.

The CLI remains canonical for `discover`, `classify`, `select`, and `plan-scope`; do not infer full planning REST parity from the scoped execution routes.

## Stable schemas and artifacts

Stable machine contracts in the current surface include:

- `access-policy.v1` and `access-decision.v1` (frozen in 1.2 and enforced by all supported 2.0 target reads)
- `access-decision-proof.v1` (finite per-consumed-redirect authorization embedded by `access-decision.v1`)
- `access-rejection-error.v1` (shared strict reject/error envelope)
- `site-diagnostic.v1` (additive in 1.1; producer-only planning evidence)
- `site-skill.v1`
- `capture-request.v1`
- `capture-result.v1`
- `acquisition-attempt.v2`
- `acquisition-profile.v1`
- `acquisition-tools.v1`
- `acquisition-probe.v1`
- `acquisition-execution-plan.v1` and `acquisition-execution-plan-preview.v1`
- `acquisition-evidence.v1`
- `acquisition-manifest.v1`
- `web-listening-manifest.v1`
- `web-listening-tool-result.v1`
- `artifact_contract.v1` and `job_delivery.v1`
- `immutable-artifact-store.v1` (additive tables with incompatible immutable-write semantics in 3.0)
- `agentic-site-rules.v1` and `agentic-orchestration.v1` (additive bounded exploration rules and durable task/observation state in 3.1)
- `site-skill-list.v1`, `site-skill-inspect.v1`, and `site-skill-validation.v1`
- `browseract-inspection.v1`

Canonical machine-readable examples remain active under `docs/testing/fixtures/`:

- The producer-confirmed [acquisition-manifest.v1 schema](contracts/acquisition-manifest.v1/schema.json), [fixture](contracts/acquisition-manifest.v1/fixture.json), and [confirmation](contracts/acquisition-manifest.v1/producer-confirmation.json) form one digest-bound canonical bundle outside the sample-fixture directory.

- [access-policy-v1.sample.json](docs/testing/fixtures/access-policy-v1.sample.json)
- [access-decision-v1.sample.json](docs/testing/fixtures/access-decision-v1.sample.json)
- [access-decision-v1.sensitive-url.invalid.json](docs/testing/fixtures/access-decision-v1.sensitive-url.invalid.json), [access-decision-v1.nested-sensitive-url.invalid.json](docs/testing/fixtures/access-decision-v1.nested-sensitive-url.invalid.json), [access-decision-v1.nfkc-query.invalid.json](docs/testing/fixtures/access-decision-v1.nfkc-query.invalid.json), [access-decision-v1.namespaced-secret.invalid.json](docs/testing/fixtures/access-decision-v1.namespaced-secret.invalid.json), [access-decision-v1.overlapping-userinfo.invalid.json](docs/testing/fixtures/access-decision-v1.overlapping-userinfo.invalid.json), [access-decision-v1.pipe-network-userinfo.invalid.json](docs/testing/fixtures/access-decision-v1.pipe-network-userinfo.invalid.json), [access-decision-v1.proxy-authority.invalid.json](docs/testing/fixtures/access-decision-v1.proxy-authority.invalid.json), [access-policy-v1.sensitive-evidence.invalid.json](docs/testing/fixtures/access-policy-v1.sensitive-evidence.invalid.json), [access-policy-v1.sensitive-identity.invalid.json](docs/testing/fixtures/access-policy-v1.sensitive-identity.invalid.json), [access-policy-v1.nested-sensitive-text.invalid.json](docs/testing/fixtures/access-policy-v1.nested-sensitive-text.invalid.json), [access-policy-v1.encoded-sensitive-text.invalid.json](docs/testing/fixtures/access-policy-v1.encoded-sensitive-text.invalid.json), [access-policy-v1.namespaced-secret.invalid.json](docs/testing/fixtures/access-policy-v1.namespaced-secret.invalid.json), [access-policy-v1.pipe-network-userinfo.invalid.json](docs/testing/fixtures/access-policy-v1.pipe-network-userinfo.invalid.json), [access-policy-v1.uri-userinfo.invalid.json](docs/testing/fixtures/access-policy-v1.uri-userinfo.invalid.json), [access-policy-v1.encoded-nested-userinfo.invalid.json](docs/testing/fixtures/access-policy-v1.encoded-nested-userinfo.invalid.json), and [access-policy-v1.network-authority.invalid.json](docs/testing/fixtures/access-policy-v1.network-authority.invalid.json) are committed fail-closed examples for encoded/nested/fullwidth query, namespaced/encoded/nested/whitespace/long-token credentials, and raw/percent/double-percent HTTP/SOCKS/network-path userinfo forms, including pipe-delimited network paths.
- [access-rejection-error-v1.sample.json](docs/testing/fixtures/access-rejection-error-v1.sample.json) is the canonical standalone shared envelope. Its tampered-ID, cache-key, freshness, matrix, and partial-origin-policy negative companions freeze independent fail-closed validation.
- [access-decision-v1.numeric-overflow.invalid.json](docs/testing/fixtures/access-decision-v1.numeric-overflow.invalid.json) freezes fail-closed extreme-number handling and the shared CLI error envelope.
- [site-skill-v1.sample.json](docs/testing/fixtures/site-skill-v1.sample.json)
- [agentic-site-rules-v1.sample.yaml](docs/testing/fixtures/agentic-site-rules-v1.sample.yaml)
- [capture-request-v1.sample.json](docs/testing/fixtures/capture-request-v1.sample.json)
- [capture-result-v1.sample.json](docs/testing/fixtures/capture-result-v1.sample.json)
- [acquisition-attempt-v2.sample.json](docs/testing/fixtures/acquisition-attempt-v2.sample.json)
- [acquisition-profile-v1.sample.yaml](docs/testing/fixtures/acquisition-profile-v1.sample.yaml)
- [acquisition-tools-v1.sample.json](docs/testing/fixtures/acquisition-tools-v1.sample.json)
- [acquisition-execution-plan-v1.sample.json](docs/testing/fixtures/acquisition-execution-plan-v1.sample.json)
- [web-listening-manifest-v1.sample.json](docs/testing/fixtures/web-listening-manifest-v1.sample.json)
- [site-diagnostic-v1.sample.json](docs/testing/fixtures/site-diagnostic-v1.sample.json)

Typical durable workflow artifacts are:

- control: `section_inventory_<site>_<date>.yaml`, `section_classification_<site>_<date>.yaml`, `section_selection_<site>_<date>.yaml`, `monitor_scope_<site>_<date>.yaml`, `monitor_task_<task>_<date>.yaml`, `acquisition_profile_<site>_<date>.yaml`
- evidence: SQLite page snapshots/edges, tracked files and observations, `capture-attempt.v1` compatibility records, and acquisition evidence
- reports: `tree_bootstrap_scope_<site>_<date>.md`, `bootstrap_scope_summary_<site>_<date>.md`, `tracking_report_<site>_<date>.md` or `.yaml`
- handoff: `web_listening_manifest_<site>_<date>.json`, `document_manifest_<site>_<date>.yaml` or `.md`

## Storage, safety, and initialization rules

Data is split into three planes:

- control: `data/plans/*.yaml`
- explanation: `data/reports/*.md` and report YAML
- evidence: `data/web_listening.db` and `data/downloads/`

Storage rules:

- `data/downloads/_blobs` is the canonical SHA-256-addressed deduplicated store. SHA-256 covers the exact canonical response entity bytes after transfer decoding, never the at-rest gzip bytes.
- `data/downloads/_tracked` is a source-oriented browsing view.
- `documents.local_path` points to the canonical blob; `tracked_local_path` points to the source view.
- `preferred_display_path` prefers the tracked view and falls back to the blob.
- Preserve `scope_id`, `run_id`, source/final URLs, timestamps, executor and Site Skill lineage, and SHA-256 values in agent-facing output.

The 3.0 immutable artifact model is additive at the database-schema level and deliberately separate at the identity level:

- `artifact_blobs.sha256` is content identity; its portable URI is exactly `artifact:sha256:<digest>`, and its database storage path is a portable POSIX-relative path below the configured downloads root. MIME is deliberately excluded from blob identity and blob semantics, so equal canonical bytes admitted under two different valid MIME types still share one CAS leaf.
- `artifact_versions.version_id` is SHA-256-derived from the same exact identity tuple frozen by Issue #48: `manifest_version`, `source_run_id`, `normalized_source_identity`, and the entity SHA-256. MIME remains version and observation provenance. An exact replay is idempotent, while a later run remains a distinct provenance-bearing version even when it reuses the same blob.
- `artifact_observations.artifact_id` is the exact frozen `acquisition-manifest.v1` ID derived from `manifest_version`, `source_run_id`, `normalized_source_identity`, and nullable retrieval SHA-256. A replay of the identical acquisition is idempotent; a different run or source remains a distinct observation.
- `artifact_lineage.lineage_id` is a deterministic digest of the observation, relation, related observation, and ordinal. Parent, source, and derived-from references must already exist and never collapse merely because their bytes match.

`ArtifactStore.store_observation` is the single 3.0 persistence path for completed HTML, PDF, and allowed attachment entity bytes. Before any mutation it validates the response `Content-Type`, final-URL extension and explicit filename extension independently when present, content magic, exact access-decision ID shape, lowercase adapter ID, numeric SemVer adapter version, closed and contiguous redirect evidence, and the closed discovery-kind semantics frozen by Issue #48. Explicit filename evidence is persisted with the observation, limited symmetrically to 255 characters on write and replay, and revalidated on replay; one valid extension cannot hide a contradiction in the other. The frozen write and load bounds are also enforced exactly: every URL is at most 2048 characters, adapter SemVer is at most 64 characters, each entity/stored/observation size is an exact nonnegative JSON-portable integer no greater than 9,007,199,254,740,991, and `derived_from_artifact_ids` contains at most 1000 members. Unknown or contradictory evidence fails closed with a stable reason code. Default evidence is a manifest-compatible seed record and stable access-decision reference; it is never an empty placeholder. A derived identity must name its selected `source_artifact_id` exactly, use that observation as its parent, include it in `derived_from`, and persist matching derived discovery evidence. Wire and content encodings are retained as observation metadata. Storage gzip is independent and deterministic: `mtime` is zero and the platform-dependent gzip OS header byte is normalized to 255, so Python 3.12/Windows and POSIX produce the same canonical container. Replay accepts exactly one complete gzip member with no trailing bytes and incrementally caps both stored-byte reads and decompressed output at the validated declared sizes. Stored bytes are verified byte-for-byte as well as by bounded decompression and entity re-hash before publication; replay repeats size and digest verification. Exclusive publication never replaces an existing CAS leaf, and a hard-link operation which takes effect before raising is reconciled, journaled, and removed on rollback rather than becoming an orphan. A replay compares the complete lineage set even when either set is empty, so lineage cannot be appended to a frozen observation. Loaded rows revalidate the exact manifest/run identity, canonical retrieval time, completed 2xx status, MIME and content magic, wire/content/storage encodings, deterministic identity, reference existence, relation cardinality and contiguous ordinals, source/derived role semantics, global lineage DAG acyclicity, frozen provenance, and the exact canonical `_blobs/<prefix>/<digest>[.gz]` portable path. A conflicting replay rolls back without mutating the frozen observation, and `BaseException`/cancellation removes only the invocation-owned file and database rows through the execution journal. Every public operation on one `Storage` waits while a different thread owns a governed transaction and remains reentrant for the owner; only the explicit #51 cross-thread rollback/cleanup handoff bypasses that wait. Store, get, replay, resolve, read, and retention each hold their complete Storage turn, so legacy methods cannot join or overlap any artifact lifecycle.

CAS publication and reads pin both the configured root and target parent. Native directory-relative no-follow operations keep temporary creation, verification, exclusive hard-link publication, readback, and exact-identity cleanup anchored to those descriptors; the Windows fallback holds directory handles, retains the opened leaf's actual path through the parent postcheck, and closes plus identity-cleans only that owned context on failure. Parent replacement, symlink escape, temporary collision, and cleanup replacement therefore fail without clobbering a victim or leaving an owned partial. Retention may remove only a new-store blob with no `artifact_versions`, `artifact_observations`, legacy `documents`/`document_blobs`, `tracked_files`, or `acquisition_artifacts` reference at the deletion boundary. In the same SQLite write transaction which deletes the blob row it commits a durable `artifact_blob_retirements` marker. Insert/update triggers prevent independent Storage processes from committing any new legacy reference to that digest after the retirement lock is released; a later immutable-store resurrection publishes bytes and removes the marker atomically with its new row/references. Retention pins the no-follow leaf below the configured root, atomically moves the exact identity into an exclusive same-parent quarantine with a platform no-replace primitive, revalidates its bytes and lexical ancestor chain immediately before commit, and reconciles both rolled-back/durable database commits and secure/fallback transfers which take effect before raising. A candidate-name or post-effect target race never overwrites either victim; the primary interruption remains authoritative while the original is restored to the canonical name. Post-commit compensation restores and verifies the exact canonical leaf before re-inserting its row and clearing retirement; a collision instead keeps the delete and retirement durable and preserves both the primary error and quarantined original. Candidate unlink is the irreversible byte-deletion boundary: if later removal of the already-empty quarantine directory fails or is interrupted, the durable blob-row deletion is retained rather than re-inserting a row that points to missing bytes. New-table-only integrity triggers reject dangling inserts and referenced deletes without enabling legacy-wide SQLite foreign-key enforcement. Existing document and `document_blobs` rows remain compatibility-readable, including their historical local paths; startup creates the new tables, indexes, retirement guards, and integrity triggers without destructively copying or rewriting legacy document state. A pre-release artifact schema that stored MIME on the blob remains readable only when its artifact evidence is valid under the frozen contract: the extra blob column is ignored for identity, a newly added version MIME field is backfilled from its existing observation, and the additive filename-evidence field defaults empty, while malformed migrated provenance or lineage fails stably on load. No legacy content bytes, paths, or observations are automatically moved. New-store outputs expose the portable artifact URI and relative storage path, not an absolute machine path. The existing `_tracked` view remains a non-canonical browsing aid.

Safety rules:

- Keep scopes bounded with explicit/effective `max_depth`, `max_pages`, and `max_files`; never expand a whole site blindly.
- Profile domains must be a non-empty subset of the governed Site Skill domains.
- Browser, Playwright, CloakBrowser, and BrowserAct target navigation is disabled in 2.0; optional-runtime inspection does not grant read authority.
- Treat acquisition picker/probe results as planning evidence, never as permission to bypass the reviewed scope, profile, Site Skill, or compiled plan.
- A bootstrap creates the baseline; a later run performs change detection.
- Agentic discovery adapters propose candidates only. They cannot supply target bodies, enlarge the rule/plan/Site Skill boundary, bypass the gateway, or bypass immutable original storage.

## Version and Runtime Compatibility

Compatibility inventory last reviewed on **2026-08-21**.

| Component | Compatibility policy / observation |
|---|---|
| `web-listening` | `3.1.0` |
| Python | Declared `>=3.12,<3.13`; verified with 3.12.3 |
| FastAPI | Project environment verified at 0.139.2 |
| MCP | Declared `>=1.28.1,<2.0.0`; verified at 1.28.1; 2.x is not qualified |
| BrowserAct | Exact isolated contract `browser-act-cli==1.0.6`; latest observed 1.0.6 |
| Playwright | Declared `>=1.52.0`; external host observed 1.59.0; latest observed 1.61.0 |
| CloakBrowser | Declared `>=0.3.26`; external host observed 0.3.27; latest observed 0.4.12 |

External-host and latest-version observations are inventory signals, **not compatibility certification**. Do not raise lower bounds or upgrade deployed runtimes from those observations alone. Every upgrade requires qualification in an isolated environment, focused adapter/contract tests, the full project suite, and a rollback decision.

The `dev` and `mcp` extras both declare `mcp>=1.28.1,<2.0.0`. The stdio server uses the qualified MCP 1.x `FastMCP` API; MCP 2.x must not be installed until it is separately qualified.

BrowserAct has an exact isolated inspection contract: it must run from a separate Python 3.12 tool environment, must not be added to project dependencies, and must pass `web-listening inspect-browseract --json` as version 1.0.6 with the expected advertised capabilities. Inspection executes only bounded version/runtime/help probes; the production executor and wrapper reject every target request before spawning BrowserAct. It, Playwright, and CloakBrowser are not supported 2.0 target-content readers.

### Semantic-version decision rubric

- **Patch (`1.0.x`)**: backward-compatible bug, security, documentation, or packaging correction; no intended contract or workflow expansion.
- **Minor (`1.x.0`)**: backward-compatible capability, command/field/tool addition, optional integration, or additive schema evolution.
- **Major (`x.0.0`)**: incompatible CLI/API/MCP/schema/artifact/storage behavior, changed authority semantics, removed supported behavior, or a runtime/dependency change that requires consumer migration.

Dependency qualification can trigger any level: use patch only when the supported contract is unchanged, minor for additive newly supported runtime capability, and major when consumers or persisted artifacts must migrate.

Issue #49 was an additive contract and offline-CLI capability, advancing the project from 1.1.0 to **1.2.0** while deliberately leaving execution authority unchanged. It established that the complete runtime migration changes authority semantics and therefore requires **2.0.0** under this rubric.

Issue #48 is another additive contract and offline-CLI capability, so it advances the project from 1.2.0 to **1.3.0**. It publishes canonical producer bytes only; the later runtime/storage producer work remains separately gated under Issues #52–#54.

Issue #50 added a shared caller-independent gateway core without migrating supported read paths or changing execution authority, advancing the project from 1.3.0 to **1.4.0** and reserving the runtime migration for a Major release.

Issue #51 migrates supported crawler, sitemap/feed, document/attachment, candidate re-entry, scoped CLI/API/MCP, and legacy execution boundaries to the unified gateway or explicitly disables them. It changes execution authority and removes supported direct-read behavior, so the project advances from 1.4.0 to **2.0.0**. No acquisition-manifest storage, AI InfoSearch database, or default/admin override is included.

Issue #52 changes durable content behavior from mutable same-URL document state to an immutable blob/version/observation/lineage contract. Although its tables are added without destructively migrating legacy rows, consumers adopting the new store must use portable URIs, distinguish blob/version/observation identities, and stop relying on same-URL overwrite semantics. That is incompatible artifact/storage behavior under the rubric, so the project advances from 2.0.0 to **3.0.0**. Crawler/search orchestration and acquisition-manifest production remain out of scope.

Issue #53 adds versioned Agentic rules, candidate exploration, and durable parent/child/read-observation state without changing the existing staged CLI, access authority, immutable identity, or storage contract. It is a backward-compatible capability and additive schema evolution, so the project advances from 3.0.0 to **3.1.0**. Final acquisition-manifest production and downstream classification/index/ready work remain separately gated.

### Weekly review policy

Once each week, maintainers review the project version, supported Python range, resolved project-environment versions, optional-runtime observations, upstream security/release notes, and available latest versions. Record whether each change is **observe**, **qualify**, **adopt**, **defer**, or **reject**. Adoption requires the qualification gates above and an explicit SemVer decision; the weekly review does not automatically modify dependency bounds.

## Validation

From the project environment:

```bash
python -m pytest tests -q
python tools/validate_real_sites.py
python tools/run_dev_regression.py
python tools/run_smoke_site_catalog.py --report-only
python tools/run_tree_catalog_validation.py
python tools/run_agent_rescue_validation.py
web-listening --help
```

Network/live catalog commands should be run only in an authorized environment. Contract and package checks should use the committed fixtures and offline test suite first.

## Documentation and archive policy

This root `README.md` is the sole active human-facing product document. Executable Markdown assets (`AGENTS.md`, `.codex/**/SKILL.md`, `skills/**/SKILL.md`, and packaged Site Skill `SKILL.md` files) remain active as runtime/agent instructions, not parallel product documentation. Machine-readable fixtures under `docs/testing/fixtures/` remain active contracts/examples.

Historical designs, plans, reports, contract prose, and operations notes are retained under `docs/archive/` for provenance only. They may contain superseded paths, versions, limitations, or future-phase language and are not current authority. The prior April roadmap history remains unchanged under `docs/archive/2026-04-roadmap-history/`; the final consolidation snapshot is under `docs/archive/2026-07-readme-consolidation/`. New product guidance must update this README rather than create another active prose document under `docs/`.
