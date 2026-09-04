"""Bounded Agentic candidate exploration over governed reads and immutable bytes.

Discovery adapters may propose URLs, but this module owns every target-content
read.  A candidate is therefore inert until it passes the versioned site rules
and is admitted through the existing :class:`GovernedReadGateway` facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from functools import wraps
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from httpx import MockTransport
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from web_listening.blocks import access_gateway as access_gateway_module
from web_listening.blocks import governed_read as governed_read_module
from web_listening.blocks.access_gateway import (
    AccessGateway,
    AccessGatewayBudgetError,
    AccessGatewayConfig,
    AccessGatewayError,
    AccessGatewayOriginError,
    AccessGatewayTransportError,
)
from web_listening.blocks.acquisition_execution_plan import (
    AcquisitionExecutionPlan,
    compile_acquisition_execution_plan,
)
from web_listening.blocks.acquisition_profile import AcquisitionProfile
from web_listening.blocks.acquisition_terminal import classify_html_capture
from web_listening.blocks.diff import extract_links
from web_listening.blocks.governed_read import (
    _MOCK_STATE_LOCK_TYPE,
    AccessRejectedError,
    GovernedReadGateway,
    GovernedReadResult,
    MockClientReadGateway,
    _MockClientTransport,
)
from web_listening.blocks.immutable_artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    StoredArtifact,
)
from web_listening.blocks.monitor_scope_planner import (
    MonitorScopePlan,
    compute_semantic_scope_fingerprint,
)
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    SafePinnedTransport,
    TransportFailure,
    normalize_http_url,
)
from web_listening.contracts import access_decision as access_decision_module
from web_listening.contracts.acquisition_batch import (
    ACQUISITION_BATCH_RESULT_VERSION,
    AcquisitionBatchCounts,
    AcquisitionBatchResult,
    AcquisitionDisposition,
)
from web_listening.contracts.access_decision import (
    _validate_non_sensitive_text,
    canonicalize_access_url,
)
from web_listening.contracts.site_diagnostic import canonical_json
from web_listening.executors.registry import ExecutorRegistry
from web_listening.site_skill_registry import ResolvedSiteSkill

AGENTIC_SITE_RULES_VERSION = "agentic-site-rules.v1"
AGENTIC_ORCHESTRATION_VERSION = "agentic-orchestration.v1"
AGENTIC_LEDGER_VERSION = "agentic-ledger.v2"
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCESS_DECISION_RE = re.compile(r"^access-decision-[0-9a-f]{16}$")
_SOURCE_RUN_RE = re.compile(r"^source-run-[a-z0-9][a-z0-9._-]{0,63}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact-[0-9a-f]{24}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "partial", "rejected", "failed", "cancelled"}
)
_RUN_STATUSES = _TERMINAL_TASK_STATUSES | {"running"}
_MAX_RULE_BYTES = 1024 * 1024
_PREPARED_AUTHORITY_CAPABILITY = object()
_MAX_SAFE_TRANSPORT_CHUNK_BYTES = 65_536


def _make_agentic_predicate_root_cell():
    roots: tuple[Any, Any] | None = None

    def bind(dispatch: Any, validator: Any) -> None:
        nonlocal roots
        if roots is not None:
            raise RuntimeError("Agentic predicate roots are already bound")
        roots = (dispatch, validator)

    def lookup() -> tuple[Any, Any]:
        if roots is None:
            raise RuntimeError("Agentic predicate roots are not bound")
        return roots

    return bind, lookup


(
    _BIND_AGENTIC_PREDICATE_ROOTS,
    _AGENTIC_PREDICATE_ROOT_LOOKUP,
) = _make_agentic_predicate_root_cell()


def _with_agentic_predicate_roots(function: Callable[..., Any]):
    root_lookup = _AGENTIC_PREDICATE_ROOT_LOOKUP

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any):
        if "_predicate_roots" in kwargs:
            raise TypeError("internal predicate roots cannot be supplied")
        return function(*args, _predicate_roots=root_lookup(), **kwargs)

    return wrapped


_KNOWN_TRANSPORT_KINDS = frozenset(
    {
        "certificate",
        "connect",
        "connect_or_http",
        "dns",
        "dns_address_policy",
        "network",
        "peer_mismatch",
        "remote_disconnected",
        "robots_http_status",
        "timeout",
        "tls_policy",
        "transport_integrity",
        "unclassified_http_protocol",
        "unclassified_pre_response",
        "unclassified_transport",
    }
)
_DECISIONLESS_REASON_CODES = frozenset(
    {
        "budget.bytes_exhausted",
        "budget.concurrency_exhausted",
        "budget.files_exhausted",
        "budget.pages_exhausted",
        "budget.requests_exhausted",
        "gateway.budget",
        "gateway.policy",
        *{f"gateway.transport.{kind}" for kind in _KNOWN_TRANSPORT_KINDS},
    }
)
_FROZEN_GOVERNED_READ = GovernedReadGateway.read
_FROZEN_GOVERNED_SEAL_RUNTIME = GovernedReadGateway._seal_runtime
_FROZEN_GOVERNED_VALIDATE_RUNTIME = GovernedReadGateway._validate_runtime
_FROZEN_MOCK_READ = MockClientReadGateway.read
_FROZEN_MOCK_PREVIEW = MockClientReadGateway._preview_origins
_FROZEN_MOCK_COMMIT = MockClientReadGateway._commit_origins
_FROZEN_MOCK_PREPARE = MockClientReadGateway._prepare_origins
_FROZEN_MOCK_GATEWAY_FOR_ORIGIN = MockClientReadGateway._gateway_for_origin
_FROZEN_MOCK_BUILD_GATEWAY = MockClientReadGateway._build_gateway
_FROZEN_MOCK_VALIDATE_PREPARATION = MockClientReadGateway._validate_preparation_graph
_FROZEN_MOCK_PREPARATION_GRAPH = governed_read_module._mock_preparation_graph
_FROZEN_ACCESS_REQUEST = AccessGateway.request
_FROZEN_ACCESS_REQUEST_WITH_CONTEXT = AccessGateway.request_with_context
_FROZEN_ACCESS_CACHE_KEY = AccessGateway._cache_key
_FROZEN_ACCESS_NORMALIZE_AND_GATE = AccessGateway._normalize_and_gate
_FROZEN_ACCESS_GATE_ORIGIN = AccessGateway._gate_origin
_FROZEN_ACCESS_POLICY_FOR = AccessGateway._policy_for
_FROZEN_ACCESS_FETCH_POLICY = AccessGateway._fetch_policy
_FROZEN_ACCESS_AUTHORIZE_REQUEST = AccessGateway._authorize_request
_FROZEN_ACCESS_START_AUTHORIZED_REQUEST = AccessGateway._start_authorized_request
_FROZEN_ACCESS_RETIRE_AUTHORIZED_REQUEST = AccessGateway._retire_authorized_request
_FROZEN_ACCESS_CAUSAL_NOW = AccessGateway._causal_now
_FROZEN_ACCESS_FRESH_POLICY_TIME = AccessGateway._fresh_policy_time
_FROZEN_ACCESS_SEAL_RUNTIME = AccessGateway._seal_runtime
_FROZEN_ACCESS_VALIDATE_RUNTIME = AccessGateway._validate_runtime
_FROZEN_ACCESS_REQUEST_TRANSPORT = access_gateway_module._request_transport
_FROZEN_ARTIFACT_STORE = ArtifactStore.store_observation
_FROZEN_ARTIFACT_GET = ArtifactStore.get_observation
_FROZEN_SAFE_REQUEST = SafePinnedTransport.request
_FROZEN_SAFE_ADDRESSES = SafePinnedTransport._addresses
_FROZEN_SAFE_SEAL_RUNTIME = SafePinnedTransport._seal_runtime
_FROZEN_SAFE_VALIDATE_RUNTIME = SafePinnedTransport._validate_runtime
_LEDGER_TRIGGER_NAMES = (
    "guard_agentic_runs_insert",
    "guard_agentic_runs_update",
    "guard_agentic_tasks_insert",
    "guard_agentic_tasks_update",
    "guard_agentic_observations_insert",
    "guard_agentic_observations_running_insert",
    "guard_agentic_runs_terminal_evidence",
    "guard_agentic_observations_update",
    "guard_agentic_observations_delete",
    "guard_agentic_tasks_delete",
    "guard_agentic_runs_delete",
)
_LEDGER_INDEX_DEFINITIONS = {
    "idx_agentic_tasks_run": (
        "agentic_tasks",
        "CREATE INDEX idx_agentic_tasks_run ON agentic_tasks(run_id, task_ordinal)",
    ),
    "idx_agentic_observations_run": (
        "agentic_observations",
        (
            "CREATE INDEX idx_agentic_observations_run "
            "ON agentic_observations(run_id, task_id, attempt)"
        ),
    ),
}
_LEDGER_TRIGGER_DEFINITIONS = {
    "guard_agentic_observations_delete": (
        "agentic_observations",
        "27e7e05c81ab62ac6df4ef4a1b0a41654389f67b3f08b71f50d1f9514ec8d559",
    ),
    "guard_agentic_observations_insert": (
        "agentic_observations",
        "05d9e84b416494c02a62e34f14cb07e65d0f6a33293f91f4fe17d8ee221a8ade",
    ),
    "guard_agentic_observations_running_insert": (
        "agentic_observations",
        "93df431df3909ac778e4649d363062e140a601ff62568e30a3b9638d26b849f3",
    ),
    "guard_agentic_observations_update": (
        "agentic_observations",
        "92d5cee9cd5956270f7cd8dd7c047b9767ff29ca4fe3ef16f9a249efeb3d626b",
    ),
    "guard_agentic_runs_delete": (
        "agentic_runs",
        "d9e9dccd7a89e90aa36e47def6f30615f96c7e1f3106a615e5d9c784dc5fa261",
    ),
    "guard_agentic_runs_insert": (
        "agentic_runs",
        "58fa4b61aace8f992b3f57f31dfdc9048cf1115e0eba68eacdc4a1e03f16aacb",
    ),
    "guard_agentic_runs_terminal_evidence": (
        "agentic_runs",
        "747d672f246d722a4004ab5455ab1e46e9323c7f1144d4292206b12648eef3f7",
    ),
    "guard_agentic_runs_update": (
        "agentic_runs",
        "29d06c7ee6dddc906286378336784c8307645cb0485a95d7b50ab8c6f968870b",
    ),
    "guard_agentic_tasks_delete": (
        "agentic_tasks",
        "7ae23edbb08ab6cb5d924fcbf96a85ec58a5a29622f7388bb3f0ac478882aff6",
    ),
    "guard_agentic_tasks_insert": (
        "agentic_tasks",
        "8638400d31cfe37dcc73d83273315f58b020887d59291b83c01b3650926c3f17",
    ),
    "guard_agentic_tasks_update": (
        "agentic_tasks",
        "f6345cb58c6401c3c6f310450656245a3a6bc1d83178cc0df8ebc2db4c8f4d34",
    ),
}
_LEGACY_LEDGER_TRIGGER_HASHES = {
    **{name: definition[1] for name, definition in _LEDGER_TRIGGER_DEFINITIONS.items()},
    "guard_agentic_runs_update": (
        "48819317b8e1914f682130e7b81f90057dcd82795d224a419caf975e93c4eff0"
    ),
    "guard_agentic_tasks_update": (
        "57dd53ab0806942b7375e0cd34e1cdae461e1d350a46b72f112bf04fef6fb057"
    ),
}
_CURRENT_LEDGER_COLUMN_SHAPES = {
    "agentic_runs": (
        ("run_id", "TEXT", 0, None, 1),
        ("schema_version", "TEXT", 1, "'agentic-ledger.v2'", 0),
        ("parent_task_id", "TEXT", 1, None, 0),
        ("rule_id", "TEXT", 1, None, 0),
        ("rules_version", "TEXT", 1, None, 0),
        ("rules_sha256", "TEXT", 1, None, 0),
        ("site_skill_id", "TEXT", 1, None, 0),
        ("site_skill_version", "TEXT", 1, None, 0),
        ("site_skill_package_sha256", "TEXT", 1, None, 0),
        ("execution_plan_id", "TEXT", 1, None, 0),
        ("execution_plan_version", "TEXT", 1, None, 0),
        ("execution_plan_sha256", "TEXT", 1, None, 0),
        ("read_adapter_id", "TEXT", 1, None, 0),
        ("read_adapter_version", "TEXT", 1, None, 0),
        ("replay_of_run_id", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("requests_used", "INTEGER", 1, None, 0),
        ("bytes_used", "INTEGER", 1, None, 0),
        ("pages_used", "INTEGER", 1, "0", 0),
        ("files_used", "INTEGER", 1, None, 0),
        ("warnings_json", "TEXT", 1, None, 0),
        ("required_sealed", "INTEGER", 1, "0", 0),
        ("active_reads", "INTEGER", 1, "0", 0),
        ("lease_owner", "TEXT", 0, None, 0),
        ("lease_expires_at", "TEXT", 0, None, 0),
        ("lease_epoch", "INTEGER", 1, "0", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
    ),
    "agentic_tasks": (
        ("task_id", "TEXT", 0, None, 1),
        ("schema_version", "TEXT", 1, "'agentic-ledger.v2'", 0),
        ("run_id", "TEXT", 1, None, 0),
        ("task_key", "TEXT", 1, None, 0),
        ("task_ordinal", "INTEGER", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("required", "INTEGER", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("requested_url", "TEXT", 0, None, 0),
        ("query", "TEXT", 0, None, 0),
        ("depth", "INTEGER", 1, None, 0),
        ("discovery_kind", "TEXT", 1, None, 0),
        ("discovered_from_url", "TEXT", 0, None, 0),
        ("parent_artifact_id", "TEXT", 0, None, 0),
        ("adapter_id", "TEXT", 1, None, 0),
        ("adapter_version", "TEXT", 1, None, 0),
        ("discovery_adapter_id", "TEXT", 0, None, 0),
        ("discovery_adapter_version", "TEXT", 0, None, 0),
        ("attempt_count", "INTEGER", 1, None, 0),
        ("artifact_id", "TEXT", 0, None, 0),
        ("access_decision_id", "TEXT", 0, None, 0),
        ("failure_code", "TEXT", 0, None, 0),
        ("replay_of_task_id", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
    ),
    "agentic_observations": (
        ("observation_id", "TEXT", 0, None, 1),
        ("schema_version", "TEXT", 1, "'agentic-ledger.v2'", 0),
        ("run_id", "TEXT", 1, None, 0),
        ("parent_task_id", "TEXT", 1, None, 0),
        ("task_id", "TEXT", 1, None, 0),
        ("attempt", "INTEGER", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("requested_url", "TEXT", 1, None, 0),
        ("current_url", "TEXT", 0, None, 0),
        ("final_url", "TEXT", 0, None, 0),
        ("status_code", "INTEGER", 0, None, 0),
        ("access_decision_id", "TEXT", 0, None, 0),
        ("artifact_id", "TEXT", 0, None, 0),
        ("reason_code", "TEXT", 1, None, 0),
        ("redirect_chain_json", "TEXT", 1, None, 0),
        ("discovery_json", "TEXT", 1, None, 0),
        ("adapter_id", "TEXT", 1, None, 0),
        ("adapter_version", "TEXT", 1, None, 0),
        ("observed_at", "TEXT", 1, None, 0),
    ),
}
_CURRENT_LEDGER_INDEX_SHAPES = {
    "agentic_runs": frozenset(
        {
            (1, "u", 0, ("parent_task_id",)),
            (1, "pk", 0, ("run_id",)),
        }
    ),
    "agentic_tasks": frozenset(
        {
            (0, "c", 0, ("run_id", "task_ordinal")),
            (1, "u", 0, ("run_id", "task_key")),
            (1, "pk", 0, ("task_id",)),
        }
    ),
    "agentic_observations": frozenset(
        {
            (0, "c", 0, ("run_id", "task_id", "attempt")),
            (1, "u", 0, ("run_id", "task_id", "attempt")),
            (1, "pk", 0, ("observation_id",)),
        }
    ),
    "agentic_ledger_schema": frozenset({(1, "pk", 0, ("schema_name",))}),
}


def _normalize_ledger_sql(value: object) -> str:
    return " ".join(str(value).split())


def _canonical_ledger_table_sql(table: str, columns: Sequence[str]) -> str:
    shapes = (
        (
            ("schema_name", "TEXT", 0, None, 1),
            ("version", "INTEGER", 1, None, 0),
        )
        if table == "agentic_ledger_schema"
        else _CURRENT_LEDGER_COLUMN_SHAPES[table]
    )
    by_name = {str(item[0]): item for item in shapes}
    declarations: list[str] = []
    for name in columns:
        shape = by_name[name]
        declaration = f"{name} {shape[1]}"
        if shape[4]:
            declaration += " PRIMARY KEY"
        if shape[2]:
            declaration += " NOT NULL"
        if table == "agentic_runs" and name == "parent_task_id":
            declaration += " UNIQUE"
        if shape[3] is not None:
            declaration += f" DEFAULT {shape[3]}"
        declarations.append(declaration)
    if table in {"agentic_tasks", "agentic_observations"}:
        declarations.append(
            "UNIQUE(run_id, task_key)"
            if table == "agentic_tasks"
            else "UNIQUE(run_id, task_id, attempt)"
        )
    return f"CREATE TABLE {table} ( {', '.join(declarations)} )"


class AgenticOrchestrationError(ValueError):
    """Fail-closed orchestration error carrying a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"agentic orchestration rejected input ({reason_code})")


class _CandidateValidationError(ValueError):
    pass


class _StrictRulesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgenticQuery(_StrictRulesModel):
    text: str = Field(min_length=1, max_length=512)
    required: bool = False

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip() or any(
            character.isspace() and character not in {" ", "\t"} for character in value
        ):
            raise ValueError("query text must be one trimmed line")
        _validate_non_sensitive_text(value, location="query")
        return value

    @field_validator("required", mode="before")
    @classmethod
    def exact_required_bool(cls, value: bool) -> bool:
        if type(value) is not bool:
            raise ValueError("query required must be a boolean")
        return value


class AgenticScopeRules(_StrictRulesModel):
    seed_urls: tuple[str, ...] = Field(min_length=1, max_length=1000)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=100)
    allow_patterns: tuple[str, ...] = Field(min_length=1, max_length=1000)
    queries: tuple[AgenticQuery, ...] = Field(default=(), max_length=100)

    @field_validator("queries", mode="before")
    @classmethod
    def normalize_queries(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return [
                ({"text": item} if isinstance(item, str) else item) for item in value
            ]
        return value

    @field_validator("seed_urls")
    @classmethod
    def validate_seeds(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if len(value) > 2048 or canonicalize_access_url(value) != value:
                raise ValueError("seed URLs must use canonical governed URL syntax")
        if len(values) != len(set(values)):
            raise ValueError("seed URLs must be unique")
        return values

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        checked: list[str] = []
        for value in values:
            normalized_url, origin = normalize_http_url(value)
            if normalized_url not in {value, f"{value}/"}:
                raise ValueError("allowed origins must not include a path")
            rendered = origin.as_url_origin()
            if rendered != value:
                raise ValueError("allowed origins must use exact canonical syntax")
            canonicalize_access_url(f"{rendered}/")
            checked.append(rendered)
        if len(checked) != len(set(checked)):
            raise ValueError("allowed origins must be unique")
        return tuple(checked)

    @field_validator("allow_patterns")
    @classmethod
    def validate_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if (
                not value
                or value != value.strip()
                or len(value) > 2048
                or "?" in value
                or "#" in value
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in value
                )
                or not value.startswith(("/", "http://", "https://"))
            ):
                raise ValueError("allow patterns must be bounded URL or path globs")
            _validate_non_sensitive_text(value, location="allow pattern")
            if (
                "@" in value.split("/", 3)[2]
                if value.startswith(("http://", "https://"))
                else False
            ):
                raise ValueError("allow patterns must not contain userinfo")
        if len(values) != len(set(values)):
            raise ValueError("allow patterns must be unique")
        return values

    @model_validator(mode="after")
    def seeds_are_in_origin_scope(self) -> AgenticScopeRules:
        origins = set(self.allowed_origins)
        for seed in self.seed_urls:
            if _url_origin(seed) not in origins:
                raise ValueError("every seed URL must use an allowed origin")
        return self


class AgenticBudgets(_StrictRulesModel):
    max_depth: int = Field(ge=0, le=100)
    max_requests: int = Field(ge=1, le=1_000_000)
    max_bytes: int = Field(ge=1, le=9_007_199_254_740_991)
    max_files: int = Field(ge=1, le=1_000_000)
    max_concurrency: int = Field(ge=1, le=100)
    max_retries: int = Field(ge=0, le=100)

    @field_validator(
        "max_depth",
        "max_requests",
        "max_bytes",
        "max_files",
        "max_concurrency",
        "max_retries",
        mode="before",
    )
    @classmethod
    def exact_integer(cls, value: int) -> int:
        if type(value) is not int:
            raise ValueError("budgets must be exact integers")
        return value


class AgenticSiteRules(_StrictRulesModel):
    schema_version: Literal["agentic-site-rules.v1"]
    rule_id: str = Field(min_length=1, max_length=128)
    version: str
    site_key: str = Field(min_length=1, max_length=128)
    scope: AgenticScopeRules
    budgets: AgenticBudgets
    content_types: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("rule_id", "site_key")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not _IDENTITY_RE.fullmatch(value):
            raise ValueError(
                "rule and site identities must be canonical lowercase names"
            )
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER_RE.fullmatch(value):
            raise ValueError("rules version must be canonical SemVer")
        return value

    @field_validator("content_types")
    @classmethod
    def validate_content_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if (
                value != value.casefold().strip()
                or "/" not in value
                or ";" in value
                or len(value) > 127
            ):
                raise ValueError("content types must be canonical base MIME types")
        if len(values) != len(set(values)):
            raise ValueError("content types must be unique")
        return values

    @property
    def rules_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def matches(self, url: str) -> bool:
        try:
            canonicalize_access_url(url)
        except ValueError:
            return False
        if _url_origin(url) not in set(self.scope.allowed_origins):
            return False
        path = urlsplit(url).path or "/"
        return any(
            fnmatchcase(url, pattern)
            if pattern.startswith(("http://", "https://"))
            else fnmatchcase(path, pattern)
            for pattern in self.scope.allow_patterns
        )


_FROZEN_RULES_MATCHES = AgenticSiteRules.matches
_FROZEN_RULES_DIGEST = AgenticSiteRules.rules_sha256.fget


class _UniqueKeyLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "YAML aliases are not permitted",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(loader, node, deep=False):
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_agentic_site_rules(path: str | Path) -> AgenticSiteRules:
    """Load one bounded strict YAML rules artifact without external activity."""
    try:
        data = _read_bounded_regular_file(Path(path), limit=_MAX_RULE_BYTES)
        if not data or len(data) > _MAX_RULE_BYTES or data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("rules bytes are empty, oversized, or BOM-prefixed")
        text = data.decode("utf-8")
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
        if not isinstance(loaded, dict):
            raise TypeError("rules root must be a mapping")
        rules = AgenticSiteRules.model_validate(loaded)
        if not all(rules.matches(seed) for seed in rules.scope.seed_urls):
            raise ValueError("seed URL does not match an allow pattern")
        return rules
    except AgenticOrchestrationError:
        raise
    except (
        MemoryError,
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise AgenticOrchestrationError("rules.invalid") from exc


def _read_bounded_regular_file(path: Path, *, limit: int) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError("rules path must be one regular no-follow file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise OSError("rules file identity changed during open")
        remaining = limit + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class AgenticAuthority:
    site_skill_id: str
    site_skill_version: str
    site_skill_package_sha256: str
    execution_plan_id: str
    execution_plan_version: str
    execution_plan_sha256: str
    read_adapter_id: str
    read_adapter_version: str

    @classmethod
    def from_resolved(
        cls,
        site_skill: ResolvedSiteSkill,
        execution_plan: AcquisitionExecutionPlan,
        *,
        _capability: object | None = None,
    ) -> AgenticAuthority:
        if _capability is not _PREPARED_AUTHORITY_CAPABILITY:
            raise AgenticOrchestrationError("authority.binding_invalid")
        if (
            type(site_skill) is not ResolvedSiteSkill
            or type(execution_plan) is not AcquisitionExecutionPlan
        ):
            raise AgenticOrchestrationError("authority.type_invalid")
        manifest = site_skill.manifest
        if (
            execution_plan.schema_version != "acquisition-execution-plan.v1"
            or execution_plan.mode != "governed"
            or execution_plan.site_key != manifest.site_key
            or execution_plan.site_skill_id != manifest.skill_id
            or execution_plan.site_skill_version != manifest.version
            or execution_plan.site_skill_package_sha256 != site_skill.package_sha256
            or execution_plan.executor_id is None
            or execution_plan.executor_version is None
            or execution_plan.script_sha256 is None
            or site_skill.script_sha256.get(execution_plan.entrypoint or "")
            != execution_plan.script_sha256
            or not _SHA256_RE.fullmatch(execution_plan.acquisition_fingerprint)
        ):
            raise AgenticOrchestrationError("authority.binding_invalid")
        plan_json = execution_plan.to_json()
        plan_sha256 = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        return cls(
            site_skill_id=manifest.skill_id,
            site_skill_version=manifest.version,
            site_skill_package_sha256=site_skill.package_sha256,
            execution_plan_id=f"acquisition-plan-{execution_plan.acquisition_fingerprint[:24]}",
            execution_plan_version=execution_plan.schema_version,
            execution_plan_sha256=plan_sha256,
            read_adapter_id=execution_plan.executor_id,
            read_adapter_version=execution_plan.executor_version,
        )

    def __post_init__(self) -> None:
        identities = (
            self.site_skill_id,
            self.execution_plan_id,
            self.read_adapter_id,
        )
        if not all(_IDENTITY_RE.fullmatch(item) for item in identities):
            raise AgenticOrchestrationError("authority.identity_invalid")
        if not _SEMVER_RE.fullmatch(
            self.site_skill_version
        ) or not _SEMVER_RE.fullmatch(self.read_adapter_version):
            raise AgenticOrchestrationError("authority.version_invalid")
        if self.execution_plan_version != "acquisition-execution-plan.v1":
            raise AgenticOrchestrationError("authority.version_invalid")
        if not _SHA256_RE.fullmatch(
            self.site_skill_package_sha256
        ) or not _SHA256_RE.fullmatch(self.execution_plan_sha256):
            raise AgenticOrchestrationError("authority.digest_invalid")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True, init=False)
class PreparedAgenticAuthority:
    """Compiler-produced authority sealed to one exact gateway and artifact store."""

    resolved_site_skill: ResolvedSiteSkill
    execution_plan: AcquisitionExecutionPlan
    authority: AgenticAuthority
    read_gateway: GovernedReadGateway | MockClientReadGateway
    artifact_store: ArtifactStore
    scope: MonitorScopePlan
    canonical_plan_json: str
    scope_seal_sha256: str
    site_skill_seal_sha256: str
    allowed_origins: tuple[str, ...]
    artifact_mime_types: frozenset[str]
    read_gateway_identity: int
    access_gateway_identity: int | None
    artifact_store_identity: int
    artifact_storage_identity: int
    artifact_root: str
    artifact_storage_encoding: str
    artifact_store_seal: tuple[Any, ...]
    mock_client_identity: int | None
    mock_transport_identity: int | None
    read_gateway_seal: tuple[Any, ...]
    predicate_dispatch: _AgenticPredicateDispatch
    predicate_dispatch_identity: int
    predicate_dispatch_seal: tuple[int, ...]
    predicate_dispatch_validator: Callable[[_AgenticPredicateDispatch], tuple[int, ...]]
    _seal: object

    @classmethod
    @_with_agentic_predicate_roots
    def _create(
        cls,
        *,
        resolved_site_skill: ResolvedSiteSkill,
        execution_plan: AcquisitionExecutionPlan,
        authority: AgenticAuthority,
        read_gateway: GovernedReadGateway | MockClientReadGateway,
        artifact_store: ArtifactStore,
        scope: MonitorScopePlan,
        allowed_origins: tuple[str, ...],
        capability: object,
        _predicate_roots: tuple[
            _AgenticPredicateDispatch,
            Callable[[_AgenticPredicateDispatch], tuple[int, ...]],
        ],
    ) -> PreparedAgenticAuthority:
        if capability is not _PREPARED_AUTHORITY_CAPABILITY:
            raise AgenticOrchestrationError("authority.capability_invalid")
        predicate_dispatch, predicate_dispatch_validator = _predicate_roots
        if (
            _AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
            or _AGENTIC_PREDICATE_DISPATCH_VALIDATOR is not predicate_dispatch_validator
            or _FROZEN_AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
            or _FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR
            is not predicate_dispatch_validator
        ):
            raise AgenticOrchestrationError("authority.seal_invalid")
        try:
            predicate_dispatch_seal = predicate_dispatch_validator(predicate_dispatch)
        except (TypeError, ValueError) as exc:
            raise AgenticOrchestrationError("authority.seal_invalid") from exc
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "resolved_site_skill", resolved_site_skill)
        object.__setattr__(prepared, "execution_plan", execution_plan)
        object.__setattr__(prepared, "authority", authority)
        object.__setattr__(prepared, "read_gateway", read_gateway)
        object.__setattr__(prepared, "artifact_store", artifact_store)
        object.__setattr__(prepared, "scope", scope)
        object.__setattr__(prepared, "canonical_plan_json", execution_plan.to_json())
        object.__setattr__(prepared, "scope_seal_sha256", _scope_seal(scope))
        object.__setattr__(
            prepared,
            "site_skill_seal_sha256",
            _resolved_site_skill_seal(resolved_site_skill),
        )
        object.__setattr__(prepared, "allowed_origins", allowed_origins)
        object.__setattr__(
            prepared,
            "artifact_mime_types",
            frozenset(artifact_store.allowed_mime_types),
        )
        object.__setattr__(prepared, "read_gateway_identity", id(read_gateway))
        object.__setattr__(
            prepared, "read_gateway_seal", _read_gateway_seal(read_gateway)
        )
        object.__setattr__(
            prepared,
            "access_gateway_identity",
            id(read_gateway.gateway)
            if type(read_gateway) is GovernedReadGateway
            else None,
        )
        object.__setattr__(prepared, "artifact_store_identity", id(artifact_store))
        object.__setattr__(
            prepared, "artifact_storage_identity", id(artifact_store.storage)
        )
        object.__setattr__(
            prepared, "artifact_root", str(artifact_store.root.resolve())
        )
        object.__setattr__(
            prepared, "artifact_storage_encoding", artifact_store.storage_encoding
        )
        object.__setattr__(
            prepared, "artifact_store_seal", _artifact_store_seal(artifact_store)
        )
        object.__setattr__(
            prepared,
            "predicate_dispatch",
            predicate_dispatch,
        )
        object.__setattr__(
            prepared,
            "predicate_dispatch_identity",
            id(predicate_dispatch),
        )
        object.__setattr__(
            prepared,
            "predicate_dispatch_validator",
            predicate_dispatch_validator,
        )
        object.__setattr__(
            prepared,
            "predicate_dispatch_seal",
            predicate_dispatch_seal,
        )
        if type(read_gateway) is MockClientReadGateway:
            object.__setattr__(
                prepared, "mock_client_identity", id(read_gateway._transport.client)
            )
            object.__setattr__(
                prepared,
                "mock_transport_identity",
                id(read_gateway._transport.client._transport),
            )
        else:
            object.__setattr__(prepared, "mock_client_identity", None)
            object.__setattr__(prepared, "mock_transport_identity", None)
        object.__setattr__(prepared, "_seal", capability)
        return prepared

    @_with_agentic_predicate_roots
    def validate(
        self,
        *,
        _predicate_roots: tuple[
            _AgenticPredicateDispatch,
            Callable[[_AgenticPredicateDispatch], tuple[int, ...]],
        ],
    ) -> None:
        if (
            type(self) is not PreparedAgenticAuthority
            or PreparedAgenticAuthority.validate is not _FROZEN_PREPARED_VALIDATE
        ):
            raise AgenticOrchestrationError("authority.seal_invalid")
        try:
            predicate_dispatch, predicate_dispatch_validator = _predicate_roots
            if (
                _AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
                or _AGENTIC_PREDICATE_DISPATCH_VALIDATOR
                is not predicate_dispatch_validator
                or _FROZEN_AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
                or _FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR
                is not predicate_dispatch_validator
            ):
                raise AgenticOrchestrationError("authority.seal_invalid")
            try:
                predicate_seal = predicate_dispatch_validator(predicate_dispatch)
            except (TypeError, ValueError) as exc:
                raise AgenticOrchestrationError("authority.seal_invalid") from exc
            if (
                self.predicate_dispatch is not predicate_dispatch
                or self.predicate_dispatch_identity != id(predicate_dispatch)
                or self.predicate_dispatch_validator is not predicate_dispatch_validator
                or predicate_seal != self.predicate_dispatch_seal
            ):
                raise AgenticOrchestrationError("authority.seal_invalid")
            if (
                type(self.artifact_store) is not ArtifactStore
                or id(self.artifact_store) != self.artifact_store_identity
                or id(self.artifact_store.storage) != self.artifact_storage_identity
                or str(self.artifact_store.root.resolve()) != self.artifact_root
                or self.artifact_store.storage_encoding
                != self.artifact_storage_encoding
                or frozenset(self.artifact_store.allowed_mime_types)
                != self.artifact_mime_types
                or _artifact_store_seal(self.artifact_store) != self.artifact_store_seal
            ):
                raise AgenticOrchestrationError("artifact_store.seal_invalid")
            if (
                id(self.read_gateway) != self.read_gateway_identity
                or _read_gateway_seal(self.read_gateway) != self.read_gateway_seal
            ):
                raise AgenticOrchestrationError("gateway.seal_invalid")
            if (
                self._seal is not _PREPARED_AUTHORITY_CAPABILITY
                or type(self.resolved_site_skill) is not ResolvedSiteSkill
                or type(self.execution_plan) is not AcquisitionExecutionPlan
                or type(self.scope) is not MonitorScopePlan
                or _scope_seal(self.scope) != self.scope_seal_sha256
                or compute_semantic_scope_fingerprint(self.scope)
                != self.execution_plan.scope_fingerprint
                or self.execution_plan.to_json() != self.canonical_plan_json
                or _resolved_site_skill_seal(self.resolved_site_skill)
                != self.site_skill_seal_sha256
                or self.authority
                != AgenticAuthority.from_resolved(
                    self.resolved_site_skill,
                    self.execution_plan,
                    _capability=_PREPARED_AUTHORITY_CAPABILITY,
                )
            ):
                raise AgenticOrchestrationError("authority.seal_invalid")
            _validate_gateway_binding(
                self.read_gateway,
                execution_plan=self.execution_plan,
                allowed_origins=self.allowed_origins,
                expected_gateway_identity=self.access_gateway_identity,
                expected_mock_client_identity=self.mock_client_identity,
                expected_mock_transport_identity=self.mock_transport_identity,
            )
        finally:
            if PreparedAgenticAuthority.validate is not _FROZEN_PREPARED_VALIDATE:
                raise AgenticOrchestrationError("authority.seal_invalid")

    def contains_url(self, url: str) -> bool:
        return self.contains_page_url(url) or self.contains_file_url(url)

    def contains_page_url(self, url: str) -> bool:
        return self._contains_prefix(url, self.scope.allowed_page_prefixes)

    def contains_file_url(self, url: str) -> bool:
        return self._contains_prefix(url, self.scope.allowed_file_prefixes)

    def _contains_prefix(self, url: str, prefixes: Sequence[str]) -> bool:
        try:
            canonical = canonicalize_access_url(url)
        except ValueError:
            return False
        if _url_origin(canonical) not in set(self.allowed_origins):
            return False
        path = urlsplit(canonical).path or "/"
        return any(_path_within_prefix(path, prefix) for prefix in prefixes)


_FROZEN_PREPARED_VALIDATE = PreparedAgenticAuthority.validate
_FROZEN_PREPARED_CONTAINS_URL = PreparedAgenticAuthority.contains_url
_FROZEN_PREPARED_CONTAINS_PAGE_URL = PreparedAgenticAuthority.contains_page_url
_FROZEN_PREPARED_CONTAINS_FILE_URL = PreparedAgenticAuthority.contains_file_url
_FROZEN_PREPARED_CONTAINS_PREFIX = PreparedAgenticAuthority._contains_prefix


@dataclass(frozen=True, slots=True)
class _AgenticRunSnapshot:
    original_rules: AgenticSiteRules
    original_rules_identity: int
    canonical_rules_json: str
    rules_sha256: str
    rules: AgenticSiteRules
    prepared: PreparedAgenticAuthority
    prepared_identity: int

    @classmethod
    def capture(
        cls,
        rules: AgenticSiteRules,
        prepared: PreparedAgenticAuthority,
    ) -> _AgenticRunSnapshot:
        _validate_exact_rules_capability(rules)
        _validate_prepared_predicates(prepared)
        payload = canonical_json(rules.model_dump(mode="json"))
        trusted = AgenticSiteRules.model_validate_json(payload)
        snapshot = cls(
            original_rules=rules,
            original_rules_identity=id(rules),
            canonical_rules_json=payload,
            rules_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            rules=trusted,
            prepared=prepared,
            prepared_identity=id(prepared),
        )
        _FROZEN_RUN_VALIDATE(snapshot)
        return snapshot

    def validate(self) -> None:
        if (
            type(self) is not _AgenticRunSnapshot
            or _AgenticRunSnapshot.capture.__func__ is not _FROZEN_RUN_CAPTURE
            or _AgenticRunSnapshot.validate is not _FROZEN_RUN_VALIDATE
            or _AgenticRunSnapshot.matches is not _FROZEN_RUN_MATCHES
            or _AgenticRunSnapshot.contains_url is not _FROZEN_RUN_CONTAINS_URL
            or _AgenticRunSnapshot.contains_page_url
            is not _FROZEN_RUN_CONTAINS_PAGE_URL
            or _AgenticRunSnapshot.contains_file_url
            is not _FROZEN_RUN_CONTAINS_FILE_URL
            or _AgenticRunSnapshot._contains_prefix is not _FROZEN_RUN_CONTAINS_PREFIX
            or _AgenticRunSnapshot.pattern_within_scope
            is not _FROZEN_RUN_PATTERN_WITHIN_SCOPE
        ):
            raise AgenticOrchestrationError("rules.seal_invalid")
        _validate_exact_rules_capability(self.original_rules)
        if (
            id(self.original_rules) != self.original_rules_identity
            or canonical_json(self.original_rules.model_dump(mode="json"))
            != self.canonical_rules_json
            or hashlib.sha256(self.canonical_rules_json.encode("utf-8")).hexdigest()
            != self.rules_sha256
            or id(self.prepared) != self.prepared_identity
        ):
            raise AgenticOrchestrationError("rules.seal_invalid")
        _validate_prepared_predicates(self.prepared)
        _FROZEN_PREPARED_VALIDATE(self.prepared)

    def matches(self, url: str) -> bool:
        _FROZEN_RUN_VALIDATE(self)
        try:
            dispatch = self.prepared.predicate_dispatch
            try:
                dispatch.canonicalize_access_url(url)
            except ValueError:
                return False
            if dispatch.url_origin(url) not in set(self.rules.scope.allowed_origins):
                return False
            path = dispatch.urlsplit(url).path or "/"
            return any(
                dispatch.fnmatchcase(url, pattern)
                if pattern.startswith(("http://", "https://"))
                else dispatch.fnmatchcase(path, pattern)
                for pattern in self.rules.scope.allow_patterns
            )
        finally:
            _FROZEN_RUN_VALIDATE(self)

    def contains_url(self, url: str) -> bool:
        _FROZEN_RUN_VALIDATE(self)
        try:
            return _FROZEN_RUN_CONTAINS_PREFIX(
                self, url, self.prepared.scope.allowed_page_prefixes
            ) or _FROZEN_RUN_CONTAINS_PREFIX(
                self, url, self.prepared.scope.allowed_file_prefixes
            )
        finally:
            _FROZEN_RUN_VALIDATE(self)

    def contains_page_url(self, url: str) -> bool:
        _FROZEN_RUN_VALIDATE(self)
        try:
            return _FROZEN_RUN_CONTAINS_PREFIX(
                self, url, self.prepared.scope.allowed_page_prefixes
            )
        finally:
            _FROZEN_RUN_VALIDATE(self)

    def contains_file_url(self, url: str) -> bool:
        _FROZEN_RUN_VALIDATE(self)
        try:
            return _FROZEN_RUN_CONTAINS_PREFIX(
                self, url, self.prepared.scope.allowed_file_prefixes
            )
        finally:
            _FROZEN_RUN_VALIDATE(self)

    def _contains_prefix(self, url: str, prefixes: Sequence[str]) -> bool:
        dispatch = self.prepared.predicate_dispatch
        try:
            canonical = dispatch.canonicalize_access_url(url)
        except ValueError:
            return False
        if dispatch.url_origin(canonical) not in set(self.prepared.allowed_origins):
            return False
        path = dispatch.urlsplit(canonical).path or "/"
        return any(dispatch.path_within_prefix(path, prefix) for prefix in prefixes)

    def pattern_within_scope(self, pattern: str) -> bool:
        _FROZEN_RUN_VALIDATE(self)
        try:
            dispatch = self.prepared.predicate_dispatch
            if pattern.startswith(("http://", "https://")):
                parsed = dispatch.urlsplit(pattern)
                if any(marker in parsed.netloc for marker in "*?["):
                    return False
                origin = f"{parsed.scheme}://{parsed.netloc}"
                if origin not in set(self.prepared.allowed_origins):
                    return False
                path_pattern = parsed.path or "/"
            else:
                path_pattern = pattern
            wildcard = min(
                (
                    index
                    for marker in "*?["
                    if (index := path_pattern.find(marker)) >= 0
                ),
                default=len(path_pattern),
            )
            literal = path_pattern[:wildcard].rstrip("/") or "/"
            prefixes = tuple(self.prepared.scope.allowed_page_prefixes) + tuple(
                self.prepared.scope.allowed_file_prefixes
            )
            return any(
                dispatch.path_within_prefix(literal, prefix) for prefix in prefixes
            )
        finally:
            _FROZEN_RUN_VALIDATE(self)


_FROZEN_RUN_CAPTURE = _AgenticRunSnapshot.capture.__func__
_FROZEN_RUN_VALIDATE = _AgenticRunSnapshot.validate
_FROZEN_RUN_MATCHES = _AgenticRunSnapshot.matches
_FROZEN_RUN_CONTAINS_URL = _AgenticRunSnapshot.contains_url
_FROZEN_RUN_CONTAINS_PAGE_URL = _AgenticRunSnapshot.contains_page_url
_FROZEN_RUN_CONTAINS_FILE_URL = _AgenticRunSnapshot.contains_file_url
_FROZEN_RUN_CONTAINS_PREFIX = _AgenticRunSnapshot._contains_prefix
_FROZEN_RUN_PATTERN_WITHIN_SCOPE = _AgenticRunSnapshot.pattern_within_scope


def _validate_exact_rules_capability(rules: object) -> None:
    if (
        type(rules) is not AgenticSiteRules
        or type(rules.scope) is not AgenticScopeRules
        or type(rules.budgets) is not AgenticBudgets
        or any(type(query) is not AgenticQuery for query in rules.scope.queries)
        or "matches" in vars(rules)
        or "rules_sha256" in vars(rules)
        or AgenticSiteRules.matches is not _FROZEN_RULES_MATCHES
        or AgenticSiteRules.rules_sha256.fget is not _FROZEN_RULES_DIGEST
    ):
        raise AgenticOrchestrationError("rules.seal_invalid")


def _validate_prepared_predicates(prepared: object) -> None:
    if (
        type(prepared) is not PreparedAgenticAuthority
        or PreparedAgenticAuthority.validate is not _FROZEN_PREPARED_VALIDATE
        or PreparedAgenticAuthority.contains_url is not _FROZEN_PREPARED_CONTAINS_URL
        or PreparedAgenticAuthority.contains_page_url
        is not _FROZEN_PREPARED_CONTAINS_PAGE_URL
        or PreparedAgenticAuthority.contains_file_url
        is not _FROZEN_PREPARED_CONTAINS_FILE_URL
        or PreparedAgenticAuthority._contains_prefix
        is not _FROZEN_PREPARED_CONTAINS_PREFIX
    ):
        raise AgenticOrchestrationError("authority.seal_invalid")


@_with_agentic_predicate_roots
def prepare_agentic_authority(
    *,
    scope: MonitorScopePlan,
    profile: AcquisitionProfile,
    resolved_site_skill: ResolvedSiteSkill,
    executor_registry: ExecutorRegistry,
    execution_plan: AcquisitionExecutionPlan,
    read_gateway: GovernedReadGateway | MockClientReadGateway,
    artifact_store: ArtifactStore,
    _predicate_roots: tuple[
        _AgenticPredicateDispatch,
        Callable[[_AgenticPredicateDispatch], tuple[int, ...]],
    ],
) -> PreparedAgenticAuthority:
    """Recompile trusted inputs and seal their complete plan to exact I/O capabilities."""
    if (
        type(scope) is not MonitorScopePlan
        or type(profile) is not AcquisitionProfile
        or type(resolved_site_skill) is not ResolvedSiteSkill
        or type(executor_registry) is not ExecutorRegistry
        or type(execution_plan) is not AcquisitionExecutionPlan
        or type(artifact_store) is not ArtifactStore
    ):
        raise AgenticOrchestrationError("authority.type_invalid")
    predicate_dispatch, predicate_dispatch_validator = _predicate_roots
    compiled = compile_acquisition_execution_plan(
        scope, profile, resolved_site_skill, executor_registry
    )
    if execution_plan.to_json() != compiled.to_json():
        raise AgenticOrchestrationError("authority.binding_invalid")
    _validate_compiled_plan(execution_plan, executor_registry=executor_registry)
    if (
        _AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
        or _AGENTIC_PREDICATE_DISPATCH_VALIDATOR is not predicate_dispatch_validator
        or _FROZEN_AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
        or _FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR
        is not predicate_dispatch_validator
    ):
        raise AgenticOrchestrationError("authority.seal_invalid")
    try:
        predicate_seal = predicate_dispatch_validator(predicate_dispatch)
    except (TypeError, ValueError) as exc:
        raise AgenticOrchestrationError("authority.seal_invalid") from exc
    expected_origins = tuple(
        sorted(
            {
                predicate_dispatch.url_origin(scope.seed_url),
                predicate_dispatch.url_origin(scope.homepage_url),
            }
        )
    )
    mock_preparation = None
    if type(read_gateway) is MockClientReadGateway:
        try:
            mock_preparation = _FROZEN_MOCK_PREVIEW(read_gateway, expected_origins)
        except (TypeError, ValueError) as exc:
            raise AgenticOrchestrationError("gateway.seal_invalid") from exc
        _validate_mock_gateway_preparation(
            read_gateway,
            mock_preparation,
            execution_plan=execution_plan,
            allowed_origins=expected_origins,
        )
    else:
        _validate_gateway_binding(
            read_gateway,
            execution_plan=execution_plan,
            allowed_origins=expected_origins,
        )
    authority = AgenticAuthority.from_resolved(
        resolved_site_skill,
        execution_plan,
        _capability=_PREPARED_AUTHORITY_CAPABILITY,
    )
    _scope_seal(scope)
    _resolved_site_skill_seal(resolved_site_skill)
    _artifact_store_seal(artifact_store)
    str(artifact_store.root.resolve())
    frozenset(artifact_store.allowed_mime_types)
    try:
        if (
            _AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
            or _AGENTIC_PREDICATE_DISPATCH_VALIDATOR is not predicate_dispatch_validator
            or _FROZEN_AGENTIC_PREDICATE_DISPATCH is not predicate_dispatch
            or _FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR
            is not predicate_dispatch_validator
            or predicate_dispatch_validator(predicate_dispatch) != predicate_seal
        ):
            raise ValueError("Agentic predicate dispatch changed")
    except (TypeError, ValueError) as exc:
        raise AgenticOrchestrationError("authority.seal_invalid") from exc
    if mock_preparation is not None:
        (
            _normalized,
            _gateways,
            source_gateways,
            source_origins,
            source_mode,
            source_lock,
        ) = mock_preparation
        with source_lock:
            try:
                try:
                    _FROZEN_MOCK_COMMIT(read_gateway, mock_preparation)
                except (TypeError, ValueError) as exc:
                    raise AgenticOrchestrationError("gateway.seal_invalid") from exc
                return PreparedAgenticAuthority._create(
                    resolved_site_skill=resolved_site_skill,
                    execution_plan=execution_plan,
                    authority=authority,
                    read_gateway=read_gateway,
                    artifact_store=artifact_store,
                    scope=scope,
                    allowed_origins=expected_origins,
                    capability=_PREPARED_AUTHORITY_CAPABILITY,
                )
            except BaseException:
                read_gateway._gateways = source_gateways
                read_gateway._prepared_origins = source_origins
                read_gateway._transport._robots_mode = source_mode
                raise
    return PreparedAgenticAuthority._create(
        resolved_site_skill=resolved_site_skill,
        execution_plan=execution_plan,
        authority=authority,
        read_gateway=read_gateway,
        artifact_store=artifact_store,
        scope=scope,
        allowed_origins=expected_origins,
        capability=_PREPARED_AUTHORITY_CAPABILITY,
    )


def _validate_compiled_plan(
    plan: AcquisitionExecutionPlan, *, executor_registry: ExecutorRegistry
) -> None:
    if (
        plan.schema_version != "acquisition-execution-plan.v1"
        or plan.mode != "governed"
        or plan.scope_fingerprint_algorithm != "sha256:monitor-scope-semantic.v2"
        or plan.acquisition_fingerprint_algorithm
        != "sha256:acquisition-execution-plan.v1"
        or not _SHA256_RE.fullmatch(plan.scope_fingerprint)
        or not _SHA256_RE.fullmatch(plan.acquisition_fingerprint)
        or not plan.profile_id
        or not plan.steps
        or plan.warnings
    ):
        raise AgenticOrchestrationError("authority.binding_invalid")
    required_step_fields = {
        "position",
        "adapter",
        "recipe_id",
        "executor_id",
        "executor_version",
        "entrypoint",
        "script_sha256",
        "required_capabilities",
        "executor_capabilities",
        "requires_authorized_access",
        "verification_rules",
        "limits",
    }
    for position, step in enumerate(plan.steps):
        if not required_step_fields <= set(step) or step["position"] != position:
            raise AgenticOrchestrationError("authority.binding_invalid")
        metadata = executor_registry.metadata.get(step["executor_id"])
        if (
            step["adapter"] != "web_http"
            or step["executor_id"] != "web_http"
            or set(step["required_capabilities"]) != {"http_get"}
            or set(step["executor_capabilities"]) != {"http_get"}
        ):
            raise AgenticOrchestrationError("authority.http_execution_required")
        if (
            metadata is None
            or step["adapter"] != step["executor_id"]
            or step["executor_version"] != metadata.version
            or tuple(step["executor_capabilities"])
            != tuple(sorted(metadata.capabilities))
            or not set(step["required_capabilities"]) <= metadata.capabilities
            or step["requires_authorized_access"]
            is not metadata.requires_authorized_access
        ):
            raise AgenticOrchestrationError("authority.binding_invalid")
    first = plan.steps[0]
    if (
        plan.executor_id != "web_http"
        or set(plan.required_capabilities) != {"http_get"}
        or plan.recipe_id != first["recipe_id"]
        or plan.executor_id != first["executor_id"]
        or plan.executor_version != first["executor_version"]
        or plan.entrypoint != first["entrypoint"]
        or plan.script_sha256 != first["script_sha256"]
        or tuple(plan.required_capabilities) != tuple(first["required_capabilities"])
        or dict(plan.limits) != dict(first["limits"])
    ):
        raise AgenticOrchestrationError("authority.binding_invalid")


def _resolved_site_skill_seal(site_skill: ResolvedSiteSkill) -> str:
    payload = {
        "manifest": site_skill.manifest.model_dump(mode="json"),
        "package_sha256": site_skill.package_sha256,
        "script_sha256": dict(site_skill.script_sha256),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _scope_seal(scope: MonitorScopePlan) -> str:
    payload = {
        "semantic_fingerprint": compute_semantic_scope_fingerprint(scope),
        "seed_url": canonicalize_access_url(scope.seed_url),
        "homepage_url": canonicalize_access_url(scope.homepage_url),
        "allowed_page_prefixes": tuple(sorted(set(scope.allowed_page_prefixes))),
        "allowed_file_prefixes": tuple(sorted(set(scope.allowed_file_prefixes))),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _artifact_store_seal(store: ArtifactStore) -> tuple[Any, ...]:
    if (
        type(store) is not ArtifactStore
        or "store_observation" in vars(store)
        or "get_observation" in vars(store)
        or ArtifactStore.store_observation is not _FROZEN_ARTIFACT_STORE
        or ArtifactStore.get_observation is not _FROZEN_ARTIFACT_GET
    ):
        raise AgenticOrchestrationError("artifact_store.seal_invalid")
    return (
        id(ArtifactStore.store_observation),
        id(getattr(ArtifactStore.store_observation, "__code__", None)),
        id(ArtifactStore.get_observation),
        id(getattr(ArtifactStore.get_observation, "__code__", None)),
    )


def _read_gateway_seal(
    read_gateway: GovernedReadGateway | MockClientReadGateway,
) -> tuple[Any, ...]:
    if type(read_gateway) is GovernedReadGateway:
        if (
            "read" in vars(read_gateway)
            or "gateway" in vars(read_gateway)
            or "max_body_bytes" in vars(read_gateway)
            or "_seal_runtime" in vars(read_gateway)
            or "_validate_runtime" in vars(read_gateway)
            or GovernedReadGateway.read is not _FROZEN_GOVERNED_READ
            or GovernedReadGateway._seal_runtime is not _FROZEN_GOVERNED_SEAL_RUNTIME
            or GovernedReadGateway._validate_runtime
            is not _FROZEN_GOVERNED_VALIDATE_RUNTIME
        ):
            raise AgenticOrchestrationError("gateway.seal_invalid")
        gateway = read_gateway.gateway
        try:
            runtime_seal = _FROZEN_GOVERNED_SEAL_RUNTIME(read_gateway)
        except AccessGatewayError as exc:
            raise AgenticOrchestrationError("gateway.seal_invalid") from exc
        return (
            "governed",
            id(read_gateway),
            id(GovernedReadGateway.read),
            _access_gateway_seal(gateway, transport_type=SafePinnedTransport),
            read_gateway.max_body_bytes,
            runtime_seal,
        )
    if type(read_gateway) is not MockClientReadGateway:
        raise AgenticOrchestrationError("gateway.type_invalid")
    if (
        "read" in vars(read_gateway)
        or "_preview_origins" in vars(read_gateway)
        or "_commit_origins" in vars(read_gateway)
        or "_gateway_for_origin" in vars(read_gateway)
        or "_build_gateway" in vars(read_gateway)
        or "_validate_preparation_graph" in vars(read_gateway)
        or MockClientReadGateway.read is not _FROZEN_MOCK_READ
        or MockClientReadGateway._preview_origins is not _FROZEN_MOCK_PREVIEW
        or MockClientReadGateway._commit_origins is not _FROZEN_MOCK_COMMIT
        or MockClientReadGateway._gateway_for_origin
        is not _FROZEN_MOCK_GATEWAY_FOR_ORIGIN
        or MockClientReadGateway._prepare_origins is not _FROZEN_MOCK_PREPARE
        or MockClientReadGateway._build_gateway is not _FROZEN_MOCK_BUILD_GATEWAY
        or MockClientReadGateway._validate_preparation_graph
        is not _FROZEN_MOCK_VALIDATE_PREPARATION
        or governed_read_module._mock_preparation_graph
        is not _FROZEN_MOCK_PREPARATION_GRAPH
        or read_gateway._preparation_graph != _FROZEN_MOCK_PREPARATION_GRAPH()
        or GovernedReadGateway.read is not _FROZEN_GOVERNED_READ
        or GovernedReadGateway._seal_runtime is not _FROZEN_GOVERNED_SEAL_RUNTIME
        or GovernedReadGateway._validate_runtime
        is not _FROZEN_GOVERNED_VALIDATE_RUNTIME
        or type(read_gateway._state_lock) is not _MOCK_STATE_LOCK_TYPE
    ):
        raise AgenticOrchestrationError("gateway.seal_invalid")
    with read_gateway._state_lock:
        wrapper = read_gateway._transport
        if type(wrapper) is not _MockClientTransport:
            raise AgenticOrchestrationError("gateway.seal_invalid")
        client = wrapper.client
        transport = getattr(client, "_transport", None)
        handler = getattr(transport, wrapper._handler_attribute, None)
        gateways: list[tuple[Any, ...]] = []
        if type(read_gateway._gateways) is not dict:
            raise AgenticOrchestrationError("gateway.seal_invalid")
        for origin, governed in sorted(read_gateway._gateways.items()):
            if type(governed) is not GovernedReadGateway:
                raise AgenticOrchestrationError("gateway.seal_invalid")
            gateway = governed.gateway
            try:
                runtime_seal = _FROZEN_GOVERNED_SEAL_RUNTIME(governed)
            except AccessGatewayError as exc:
                raise AgenticOrchestrationError("gateway.seal_invalid") from exc
            gateways.append(
                (
                    origin,
                    id(governed),
                    _access_gateway_seal(gateway, transport_type=_MockClientTransport),
                    governed.max_body_bytes,
                    runtime_seal,
                )
            )
            if gateway.transport is not wrapper:
                raise AgenticOrchestrationError("gateway.seal_invalid")
        return (
            "mock",
            id(read_gateway),
            id(MockClientReadGateway.read),
            id(MockClientReadGateway._preview_origins),
            id(MockClientReadGateway._commit_origins),
            id(MockClientReadGateway._gateway_for_origin),
            id(MockClientReadGateway._prepare_origins),
            id(MockClientReadGateway._build_gateway),
            id(MockClientReadGateway._validate_preparation_graph),
            read_gateway._preparation_graph,
            id(GovernedReadGateway.read),
            type(read_gateway._state_lock),
            id(read_gateway._state_lock),
            type(wrapper),
            id(wrapper),
            id(client),
            type(transport),
            id(transport),
            wrapper._handler_attribute,
            id(handler),
            type(handler),
            id(type(handler).__call__) if handler is not None else None,
            id(getattr(handler, "__code__", None)),
            id(type(transport).handle_request)
            if type(transport) is MockTransport
            else None,
            wrapper._client_identity,
            wrapper._transport_identity,
            wrapper._handler_identity,
            wrapper._handler_type,
            wrapper._handler_call_identity,
            wrapper._handler_code_identity,
            id(wrapper._transport_capability),
            id(wrapper._handler_capability),
            wrapper._handle_request_identity,
            wrapper._handle_request_code_identity,
            id(wrapper._robots_mode),
            wrapper._legacy_robots_mode_identity,
            wrapper._agentic_robots_mode_identity,
            id(_MockClientTransport._LEGACY_ROBOTS_MODE),
            id(_MockClientTransport._AGENTIC_ROBOTS_MODE),
            read_gateway._user_agent,
            read_gateway._max_body_bytes,
            id(read_gateway._gateways),
            tuple(
                item.as_url_origin() for item in (read_gateway._prepared_origins or ())
            ),
            tuple(gateways),
        )


def _access_gateway_seal(
    gateway: AccessGateway, *, transport_type: type[object]
) -> tuple[Any, ...]:
    if (
        type(gateway) is not AccessGateway
        or type(gateway.config) is not AccessGatewayConfig
        or type(gateway.transport) is not transport_type
        or "request" in vars(gateway)
        or "request_with_context" in vars(gateway)
        or any(
            name in vars(gateway)
            for name in (
                "_cache_key",
                "_normalize_and_gate",
                "_gate_origin",
                "_policy_for",
                "_fetch_policy",
                "_authorize_request",
                "_start_authorized_request",
                "_retire_authorized_request",
                "_causal_now",
                "_fresh_policy_time",
                "_seal_runtime",
                "_validate_runtime",
            )
        )
        or AccessGateway.request is not _FROZEN_ACCESS_REQUEST
        or AccessGateway.request_with_context is not _FROZEN_ACCESS_REQUEST_WITH_CONTEXT
        or AccessGateway._cache_key is not _FROZEN_ACCESS_CACHE_KEY
        or AccessGateway._normalize_and_gate is not _FROZEN_ACCESS_NORMALIZE_AND_GATE
        or AccessGateway._gate_origin is not _FROZEN_ACCESS_GATE_ORIGIN
        or AccessGateway._policy_for is not _FROZEN_ACCESS_POLICY_FOR
        or AccessGateway._fetch_policy is not _FROZEN_ACCESS_FETCH_POLICY
        or AccessGateway._authorize_request is not _FROZEN_ACCESS_AUTHORIZE_REQUEST
        or AccessGateway._start_authorized_request
        is not _FROZEN_ACCESS_START_AUTHORIZED_REQUEST
        or AccessGateway._retire_authorized_request
        is not _FROZEN_ACCESS_RETIRE_AUTHORIZED_REQUEST
        or AccessGateway._causal_now is not _FROZEN_ACCESS_CAUSAL_NOW
        or AccessGateway._fresh_policy_time is not _FROZEN_ACCESS_FRESH_POLICY_TIME
        or AccessGateway._seal_runtime is not _FROZEN_ACCESS_SEAL_RUNTIME
        or AccessGateway._validate_runtime is not _FROZEN_ACCESS_VALIDATE_RUNTIME
        or access_gateway_module._request_transport
        is not _FROZEN_ACCESS_REQUEST_TRANSPORT
        or "request" in vars(gateway.transport)
        or (
            type(gateway.transport) is SafePinnedTransport
            and (
                "_addresses" in vars(gateway.transport)
                or "_seal_runtime" in vars(gateway.transport)
                or "_validate_runtime" in vars(gateway.transport)
                or SafePinnedTransport.request is not _FROZEN_SAFE_REQUEST
                or SafePinnedTransport._addresses is not _FROZEN_SAFE_ADDRESSES
                or SafePinnedTransport._seal_runtime is not _FROZEN_SAFE_SEAL_RUNTIME
                or SafePinnedTransport._validate_runtime
                is not _FROZEN_SAFE_VALIDATE_RUNTIME
            )
        )
        or type(gateway._origin_states) is not dict
        or type(gateway._policy_cache) is not dict
        or type(gateway._inflight_policy_keys) is not set
        or type(gateway._cache_generations) is not dict
    ):
        raise AgenticOrchestrationError("gateway.seal_invalid")
    try:
        runtime_seal = _FROZEN_ACCESS_SEAL_RUNTIME(gateway)
        safe_runtime_seal = (
            _FROZEN_SAFE_SEAL_RUNTIME(gateway.transport)
            if type(gateway.transport) is SafePinnedTransport
            else None
        )
    except (AccessGatewayError, TransportFailure) as exc:
        raise AgenticOrchestrationError("gateway.seal_invalid") from exc
    origin_states = tuple(
        sorted(
            (
                origin.as_url_origin(),
                type(state),
                id(state),
                id(state.lock),
            )
            for origin, state in gateway._origin_states.items()
        )
    )
    return (
        id(gateway),
        id(AccessGateway.request),
        id(AccessGateway.request_with_context),
        id(AccessGateway._policy_for),
        id(AccessGateway._authorize_request),
        id(AccessGateway._start_authorized_request),
        id(AccessGateway._retire_authorized_request),
        safe_runtime_seal,
        id(AccessGateway._gate_origin),
        id(access_gateway_module._request_transport),
        runtime_seal,
        id(gateway.config),
        gateway.config,
        type(gateway.transport),
        id(gateway.transport),
        id(type(gateway.transport).request),
        id(getattr(type(gateway.transport).request, "__code__", None)),
        (
            (
                id(SafePinnedTransport._addresses),
                id(getattr(SafePinnedTransport._addresses, "__code__", None)),
            )
            if type(gateway.transport) is SafePinnedTransport
            else None
        ),
        (
            (gateway.transport.timeout, gateway.transport.chunk_size)
            if type(gateway.transport) is SafePinnedTransport
            else None
        ),
        id(gateway._clock),
        id(gateway._sleep),
        id(gateway._origin_states),
        origin_states,
        type(gateway._cache_condition),
        id(gateway._cache_condition),
        id(gateway._policy_cache),
        id(gateway._inflight_policy_keys),
        id(gateway._cache_generations),
    )


def _validate_concrete_read_gateway(
    read_gateway: GovernedReadGateway | MockClientReadGateway,
) -> None:
    if type(read_gateway) not in {GovernedReadGateway, MockClientReadGateway}:
        raise AgenticOrchestrationError("gateway.type_invalid")
    if (
        type(read_gateway) is GovernedReadGateway
        and type(read_gateway.gateway) is not AccessGateway
    ):
        raise AgenticOrchestrationError("gateway.type_invalid")


def _validate_mock_gateway_preparation(
    read_gateway: MockClientReadGateway,
    preparation,
    *,
    execution_plan: AcquisitionExecutionPlan,
    allowed_origins: Sequence[str],
) -> None:
    _validate_concrete_read_gateway(read_gateway)
    (
        normalized,
        gateways,
        _source_gateways,
        _source_origins,
        source_mode,
        source_lock,
    ) = preparation
    try:
        read_gateway._transport._validate_identity()
    except (TypeError, ValueError) as exc:
        raise AgenticOrchestrationError("gateway.seal_invalid") from exc
    if (
        "read" in vars(read_gateway)
        or "_preview_origins" in vars(read_gateway)
        or "_commit_origins" in vars(read_gateway)
        or "_gateway_for_origin" in vars(read_gateway)
        or "_build_gateway" in vars(read_gateway)
        or "_validate_preparation_graph" in vars(read_gateway)
        or MockClientReadGateway.read is not _FROZEN_MOCK_READ
        or MockClientReadGateway._preview_origins is not _FROZEN_MOCK_PREVIEW
        or MockClientReadGateway._commit_origins is not _FROZEN_MOCK_COMMIT
        or MockClientReadGateway._gateway_for_origin
        is not _FROZEN_MOCK_GATEWAY_FOR_ORIGIN
        or MockClientReadGateway._prepare_origins is not _FROZEN_MOCK_PREPARE
        or MockClientReadGateway._build_gateway is not _FROZEN_MOCK_BUILD_GATEWAY
        or MockClientReadGateway._validate_preparation_graph
        is not _FROZEN_MOCK_VALIDATE_PREPARATION
        or governed_read_module._mock_preparation_graph
        is not _FROZEN_MOCK_PREPARATION_GRAPH
        or read_gateway._preparation_graph != _FROZEN_MOCK_PREPARATION_GRAPH()
        or GovernedReadGateway.read is not _FROZEN_GOVERNED_READ
        or type(read_gateway._state_lock) is not _MOCK_STATE_LOCK_TYPE
        or source_lock is not read_gateway._state_lock
        or type(read_gateway._gateways) is not dict
        or read_gateway._max_body_bytes != execution_plan.limits["stdout_bytes"]
        or source_mode
        not in {
            _MockClientTransport._LEGACY_ROBOTS_MODE,
            _MockClientTransport._AGENTIC_ROBOTS_MODE,
        }
        or tuple(item.as_url_origin() for item in normalized) != tuple(allowed_origins)
        or set(gateways) != set(allowed_origins)
    ):
        raise AgenticOrchestrationError("gateway.seal_invalid")
    for governed in gateways.values():
        if (
            type(governed) is not GovernedReadGateway
            or governed.max_body_bytes != read_gateway._max_body_bytes
            or governed.gateway.transport is not read_gateway._transport
            or {
                item.as_url_origin() for item in governed.gateway.config.allowed_origins
            }
            != set(allowed_origins)
        ):
            raise AgenticOrchestrationError("gateway.seal_invalid")
        _access_gateway_seal(
            governed.gateway,
            transport_type=_MockClientTransport,
        )


def _validate_gateway_binding(
    read_gateway: GovernedReadGateway | MockClientReadGateway,
    *,
    execution_plan: AcquisitionExecutionPlan,
    allowed_origins: Sequence[str],
    expected_gateway_identity: int | None = None,
    expected_mock_client_identity: int | None = None,
    expected_mock_transport_identity: int | None = None,
) -> None:
    _validate_concrete_read_gateway(read_gateway)
    if type(read_gateway) is MockClientReadGateway:
        client = read_gateway._transport.client
        transport = getattr(client, "_transport", None)
        if (
            read_gateway._max_body_bytes != execution_plan.limits["stdout_bytes"]
            or (
                expected_mock_client_identity is not None
                and id(client) != expected_mock_client_identity
            )
            or (
                expected_mock_transport_identity is not None
                and id(transport) != expected_mock_transport_identity
            )
            or type(transport) is not MockTransport
            or id(
                getattr(
                    transport,
                    read_gateway._transport._handler_attribute,
                    None,
                )
            )
            != read_gateway._transport._handler_identity
            or id(type(transport).handle_request)
            != read_gateway._transport._handle_request_identity
            or type(
                getattr(
                    transport,
                    read_gateway._transport._handler_attribute,
                    None,
                )
            )
            is not read_gateway._transport._handler_type
            or id(
                type(
                    getattr(
                        transport,
                        read_gateway._transport._handler_attribute,
                    )
                ).__call__
            )
            != read_gateway._transport._handler_call_identity
            or id(
                getattr(
                    getattr(
                        transport,
                        read_gateway._transport._handler_attribute,
                    ),
                    "__code__",
                    None,
                )
            )
            != read_gateway._transport._handler_code_identity
            or id(getattr(type(transport).handle_request, "__code__", None))
            != read_gateway._transport._handle_request_code_identity
            or "handle_request" in vars(transport)
            or read_gateway._transport._robots_mode
            is not _MockClientTransport._AGENTIC_ROBOTS_MODE
            or read_gateway._transport._legacy_robots_mode_identity
            != id(_MockClientTransport._LEGACY_ROBOTS_MODE)
            or read_gateway._transport._agentic_robots_mode_identity
            != id(_MockClientTransport._AGENTIC_ROBOTS_MODE)
            or tuple(
                item.as_url_origin() for item in (read_gateway._prepared_origins or ())
            )
            != tuple(allowed_origins)
            or any(
                {
                    item.as_url_origin()
                    for item in governed.gateway.config.allowed_origins
                }
                != set(allowed_origins)
                for governed in read_gateway._gateways.values()
            )
        ):
            raise AgenticOrchestrationError("gateway.seal_invalid")
        return
    gateway = read_gateway.gateway
    config = gateway.config
    actual_origins = {item.as_url_origin() for item in config.allowed_origins}
    if (
        type(gateway.transport) is not SafePinnedTransport
        or "request" in vars(gateway.transport)
        or (
            expected_gateway_identity is not None
            and id(gateway) != expected_gateway_identity
        )
        or actual_origins != set(allowed_origins)
        or config.diagnostic_artifact_sha256 != execution_plan.acquisition_fingerprint
        or read_gateway.max_body_bytes != execution_plan.limits["stdout_bytes"]
        or gateway.transport.timeout != execution_plan.limits["timeout_seconds"]
        or type(gateway.transport.chunk_size) is not int
        or not 1
        <= gateway.transport.chunk_size
        <= min(
            _MAX_SAFE_TRANSPORT_CHUNK_BYTES,
            read_gateway.max_body_bytes,
        )
    ):
        raise AgenticOrchestrationError(
            "gateway.transport_invalid"
            if type(gateway.transport) is not SafePinnedTransport
            or "request" in vars(gateway.transport)
            else "gateway.authority_mismatch"
        )


@dataclass(frozen=True, slots=True)
class AgenticCandidate:
    url: str
    discovery_kind: Literal["search", "link", "crawler"]
    discovered_from_url: str | None
    required: bool = False

    def __post_init__(self) -> None:
        try:
            canonicalize_access_url(self.url)
            if self.discovered_from_url is not None:
                canonicalize_access_url(self.discovered_from_url)
        except ValueError as exc:
            raise AgenticOrchestrationError("candidate.invalid") from exc
        if type(self.required) is not bool:
            raise AgenticOrchestrationError("candidate.invalid")
        if self.discovery_kind not in {"search", "link", "crawler"}:
            raise AgenticOrchestrationError("candidate.discovery_invalid")
        if self.discovered_from_url is None:
            raise AgenticOrchestrationError("candidate.discovery_invalid")


class CrawlerDiscoveryAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def discover(
        self,
        *,
        body: bytes,
        final_url: str,
        parent_artifact_id: str,
        depth: int,
    ) -> Iterable[AgenticCandidate]: ...


class AuthorizedSearchAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    authorized: bool

    def search(self, query: str) -> Iterable[AgenticCandidate]: ...


class ReadGateway(Protocol):
    def read(
        self, url: str, *, max_body_bytes: int | None = None
    ) -> GovernedReadResult: ...


@dataclass(frozen=True, slots=True)
class _AdapterSnapshot:
    adapter: object
    object_identity: int
    adapter_id: str
    adapter_version: str
    method_name: str
    callable_object: object
    callable_function: object
    bound_self_identity: int | None
    authorized: bool | None

    def validate(self) -> None:
        if (
            type(self) is not _AdapterSnapshot
            or _AdapterSnapshot.validate is not _FROZEN_ADAPTER_VALIDATE
            or _AdapterSnapshot.invoke is not _FROZEN_ADAPTER_INVOKE
        ):
            raise AgenticOrchestrationError("adapter.identity_changed")
        method = getattr(self.adapter, self.method_name, None)
        function = getattr(method, "__func__", method)
        bound_self = getattr(method, "__self__", None)
        if (
            id(self.adapter) != self.object_identity
            or getattr(self.adapter, "adapter_id", None) != self.adapter_id
            or getattr(self.adapter, "adapter_version", None) != self.adapter_version
            or not callable(method)
            or function is not self.callable_function
            or (id(bound_self) if bound_self is not None else None)
            != self.bound_self_identity
            or (
                self.authorized is not None
                and getattr(self.adapter, "authorized", None) is not self.authorized
            )
        ):
            raise AgenticOrchestrationError("adapter.identity_changed")
        if (
            _AdapterSnapshot.validate is not _FROZEN_ADAPTER_VALIDATE
            or _AdapterSnapshot.invoke is not _FROZEN_ADAPTER_INVOKE
        ):
            raise AgenticOrchestrationError("adapter.identity_changed")

    def invoke(self, *args, **kwargs):
        _FROZEN_ADAPTER_VALIDATE(self)
        try:
            if self.bound_self_identity is None:
                return self.callable_object(*args, **kwargs)
            return self.callable_function(self.adapter, *args, **kwargs)
        finally:
            _FROZEN_ADAPTER_VALIDATE(self)


_FROZEN_ADAPTER_VALIDATE = _AdapterSnapshot.validate
_FROZEN_ADAPTER_INVOKE = _AdapterSnapshot.invoke


class HtmlLinkCrawlerAdapter:
    """Pure HTML candidate discovery; it has no transport or read capability."""

    adapter_id = "html_link_crawler"
    adapter_version = "1.0.0"

    def discover(
        self,
        *,
        body: bytes,
        final_url: str,
        parent_artifact_id: str,
        depth: int,
    ) -> tuple[AgenticCandidate, ...]:
        del parent_artifact_id, depth
        html = body.decode("utf-8", errors="replace")
        candidates: list[AgenticCandidate] = []
        for url in sorted(set(extract_links(html, final_url))):
            try:
                candidates.append(
                    AgenticCandidate(
                        url=url,
                        discovery_kind="link",
                        discovered_from_url=final_url,
                    )
                )
            except AgenticOrchestrationError:
                continue
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class AgenticParentTask:
    run_id: str
    parent_task_id: str
    status: str
    rule_id: str
    rules_version: str
    rules_sha256: str
    site_skill_id: str
    site_skill_version: str
    site_skill_package_sha256: str
    execution_plan_id: str
    execution_plan_version: str
    execution_plan_sha256: str
    read_adapter_id: str
    read_adapter_version: str
    replay_of_run_id: str | None
    requests_used: int
    bytes_used: int
    pages_used: int
    files_used: int
    warnings: tuple[str, ...]
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class AgenticChildTask:
    task_id: str
    run_id: str
    task_key: str
    task_ordinal: int
    kind: str
    required: bool
    status: str
    requested_url: str | None
    query: str | None
    depth: int
    discovery_kind: str
    discovered_from_url: str | None
    parent_artifact_id: str | None
    adapter_id: str
    adapter_version: str
    discovery_adapter_id: str | None
    discovery_adapter_version: str | None
    attempt_count: int
    artifact_id: str | None
    access_decision_id: str | None
    failure_code: str | None
    replay_of_task_id: str | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class AgenticReadObservation:
    observation_id: str
    run_id: str
    parent_task_id: str
    task_id: str
    attempt: int
    status: str
    requested_url: str
    current_url: str | None
    final_url: str | None
    status_code: int | None
    access_decision_id: str | None
    artifact_id: str | None
    reason_code: str
    redirect_chain: tuple[Mapping[str, Any], ...]
    discovery: Mapping[str, Any]
    adapter_id: str
    adapter_version: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class AgenticRunResult:
    parent: AgenticParentTask
    tasks: tuple[AgenticChildTask, ...]
    observations: tuple[AgenticReadObservation, ...]
    artifacts: tuple[StoredArtifact, ...]

    def to_acquisition_batch_result(self) -> dict[str, Any]:
        """Project terminal read lineage into the versioned batch contract."""
        read_tasks = tuple(task for task in self.tasks if task.kind == "read")
        requested_tasks = tuple(
            task for task in read_tasks if task.discovery_kind == "seed"
        )
        read_task_ids = frozenset(task.task_id for task in read_tasks)
        last_observation = {
            observation.task_id: observation for observation in self.observations
        }
        dispositions: list[AcquisitionDisposition] = []
        for task in requested_tasks:
            if task.status == "completed":
                disposition = "succeeded"
                reason = "read.completed"
                artifact_id = task.artifact_id
            elif task.status == "cancelled":
                disposition = "unresolved"
                reason = task.failure_code or "task.cancelled"
                artifact_id = None
            elif task.status not in _TERMINAL_TASK_STATUSES:
                disposition = "unresolved"
                reason = task.failure_code or f"task.{task.status}"
                artifact_id = None
            else:
                disposition = "failed"
                observation = last_observation.get(task.task_id)
                reason = task.failure_code or (
                    observation.reason_code
                    if observation is not None
                    else f"task.{task.status}"
                )
                artifact_id = None
            dispositions.append(
                AcquisitionDisposition(
                    task_id=task.task_id,
                    requested_url=task.requested_url or "",
                    disposition=disposition,
                    reason=reason,
                    artifact_id=artifact_id,
                )
            )

        succeeded = sum(item.disposition == "succeeded" for item in dispositions)
        failed = sum(item.disposition == "failed" for item in dispositions)
        unresolved = sum(item.disposition == "unresolved" for item in dispositions)
        failed_evidence = sum(
            observation.task_id in read_task_ids
            and observation.status != "completed"
            and observation.artifact_id is None
            for observation in self.observations
        )
        counts = AcquisitionBatchCounts(
            requested=len(dispositions),
            succeeded=succeeded,
            failed=failed,
            unresolved=unresolved,
            valid_snapshots=len(self.artifacts),
            failed_evidence=failed_evidence,
        )
        if (
            self.parent.status == "completed"
            and counts.requested > 0
            and counts.succeeded == counts.requested
        ):
            status = "succeeded"
        elif counts.succeeded > 0 or counts.valid_snapshots > 0:
            status = "partial"
        elif counts.failed > 0:
            status = "failed"
        else:
            status = "unresolved"
        result = AcquisitionBatchResult(
            schema_version=ACQUISITION_BATCH_RESULT_VERSION,
            run_id=self.parent.run_id,
            authoritative_status=self.parent.status,
            status=status,
            full_success=status == "succeeded",
            counts=counts,
            dispositions=tuple(dispositions),
        )
        return result.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _TargetSendClaim:
    run_id: str
    owner: str
    claim_owner: str
    epoch: int
    lease_expires_at: str


_TARGET_SEND_LOCK_TYPE = type(threading.Lock())


class _TargetSendLease:
    """Callable release plus epoch-conditioned progress renewal for one send."""

    __slots__ = (
        "_claim",
        "_claim_binding",
        "_gateway_progress_capability",
        "_gateway_progress_capability_identity",
        "_lock",
        "_lock_identity",
        "_released",
        "_repository",
        "_repository_clock_identity",
        "_repository_fences_identity",
        "_repository_identity",
        "_repository_release",
        "_repository_renew",
        "_repository_storage_identity",
        "_timeout_seal",
        "_timeout_seconds",
    )

    def __init__(
        self,
        repository: AgenticTaskRepository,
        claim: _TargetSendClaim,
        *,
        timeout_seconds: float,
    ) -> None:
        self._repository = repository
        self._repository_identity = id(repository)
        self._repository_storage_identity = id(repository.storage)
        self._repository_clock_identity = id(repository.clock)
        self._repository_fences_identity = id(repository._lease_fences)
        self._repository_renew = _FROZEN_REPOSITORY_RENEW_TARGET_SEND
        self._repository_release = _FROZEN_REPOSITORY_RELEASE_TARGET_SEND
        self._claim = claim
        self._claim_binding = (
            claim.run_id,
            claim.owner,
            claim.claim_owner,
            claim.epoch,
        )
        self._timeout_seconds = timeout_seconds
        self._timeout_seal = timeout_seconds
        self._lock = threading.Lock()
        self._lock_identity = id(self._lock)
        self._released = False
        capability = access_gateway_module._TargetSendProgressCapability(
            state=self,
            state_type=_TargetSendLease,
            validate_state=_FROZEN_TARGET_SEND_LEASE_VALIDATE,
            renew_state=_FROZEN_TARGET_SEND_LEASE_RENEW,
            release_state=_FROZEN_TARGET_SEND_LEASE_RELEASE,
        )
        self._gateway_progress_capability = capability
        self._gateway_progress_capability_identity = id(capability)

    def validate(self) -> None:
        capability = self._gateway_progress_capability
        if (
            type(self) is not _TargetSendLease
            or _TargetSendLease.validate is not _FROZEN_TARGET_SEND_LEASE_VALIDATE
            or _TargetSendLease.renew is not _FROZEN_TARGET_SEND_LEASE_RENEW
            or _TargetSendLease.__call__ is not _FROZEN_TARGET_SEND_LEASE_RELEASE
            or type(self._repository) is not AgenticTaskRepository
            or id(self._repository) != self._repository_identity
            or id(self._repository.storage) != self._repository_storage_identity
            or id(self._repository.clock) != self._repository_clock_identity
            or id(self._repository._lease_fences) != self._repository_fences_identity
            or AgenticTaskRepository.renew_target_send
            is not _FROZEN_REPOSITORY_RENEW_TARGET_SEND
            or AgenticTaskRepository.release_target_send
            is not _FROZEN_REPOSITORY_RELEASE_TARGET_SEND
            or self._repository_renew is not _FROZEN_REPOSITORY_RENEW_TARGET_SEND
            or self._repository_release is not _FROZEN_REPOSITORY_RELEASE_TARGET_SEND
            or type(self._claim) is not _TargetSendClaim
            or (
                self._claim.run_id,
                self._claim.owner,
                self._claim.claim_owner,
                self._claim.epoch,
            )
            != self._claim_binding
            or self._timeout_seconds != self._timeout_seal
            or type(self._lock) is not _TARGET_SEND_LOCK_TYPE
            or id(self._lock) != self._lock_identity
            or type(self._released) is not bool
            or type(capability)
            is not access_gateway_module._TargetSendProgressCapability
            or id(capability) != self._gateway_progress_capability_identity
            or capability.state is not self
            or capability.state_type is not _TargetSendLease
            or capability.validate_state is not _FROZEN_TARGET_SEND_LEASE_VALIDATE
            or capability.renew_state is not _FROZEN_TARGET_SEND_LEASE_RENEW
            or capability.release_state is not _FROZEN_TARGET_SEND_LEASE_RELEASE
        ):
            raise AgenticOrchestrationError("run.lease_lost")

    def renew(self) -> None:
        _FROZEN_TARGET_SEND_LEASE_VALIDATE(self)
        with self._lock:
            if self._released:
                raise AgenticOrchestrationError("run.lease_lost")
            self._claim = self._repository_renew(
                self._repository,
                self._claim,
                timeout_seconds=self._timeout_seconds,
            )
        _FROZEN_TARGET_SEND_LEASE_VALIDATE(self)

    def __call__(self) -> None:
        _FROZEN_TARGET_SEND_LEASE_VALIDATE(self)
        with self._lock:
            if self._released:
                return
            self._repository_release(self._repository, self._claim)
            self._released = True
        _FROZEN_TARGET_SEND_LEASE_VALIDATE(self)


class AgenticTaskRepository:
    """Durable deterministic task state and read-observation ledger."""

    def __init__(
        self,
        storage,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lease_fences: dict[str, tuple[str, int]] = {}
        self._create_tables()

    def _turn(self):
        return _StorageTurn(self.storage)

    def _create_tables(self) -> None:
        if self.storage.execution_transaction_active:
            raise AgenticOrchestrationError("storage.transaction_active")
        with self._turn():
            version = self._preflight_ledger_version()
            self.storage.conn.execute("BEGIN IMMEDIATE")
            try:
                if version is None:
                    _execute_sql_script(
                        self.storage.conn,
                        """
                CREATE TABLE agentic_runs (
                    run_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                    parent_task_id TEXT NOT NULL UNIQUE,
                    rule_id TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    rules_sha256 TEXT NOT NULL,
                    site_skill_id TEXT NOT NULL,
                    site_skill_version TEXT NOT NULL,
                    site_skill_package_sha256 TEXT NOT NULL,
                    execution_plan_id TEXT NOT NULL,
                    execution_plan_version TEXT NOT NULL,
                    execution_plan_sha256 TEXT NOT NULL,
                    read_adapter_id TEXT NOT NULL,
                    read_adapter_version TEXT NOT NULL,
                    replay_of_run_id TEXT,
                    status TEXT NOT NULL,
                    requests_used INTEGER NOT NULL,
                    bytes_used INTEGER NOT NULL,
                    pages_used INTEGER NOT NULL DEFAULT 0,
                    files_used INTEGER NOT NULL,
                    warnings_json TEXT NOT NULL,
                    required_sealed INTEGER NOT NULL DEFAULT 0,
                    active_reads INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE agentic_tasks (
                    task_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                    run_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    task_ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    requested_url TEXT,
                    query TEXT,
                    depth INTEGER NOT NULL,
                    discovery_kind TEXT NOT NULL,
                    discovered_from_url TEXT,
                    parent_artifact_id TEXT,
                    adapter_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    discovery_adapter_id TEXT,
                    discovery_adapter_version TEXT,
                    attempt_count INTEGER NOT NULL,
                    artifact_id TEXT,
                    access_decision_id TEXT,
                    failure_code TEXT,
                    replay_of_task_id TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(run_id, task_key)
                );
                CREATE TABLE agentic_observations (
                    observation_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                    run_id TEXT NOT NULL,
                    parent_task_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    requested_url TEXT NOT NULL,
                    current_url TEXT,
                    final_url TEXT,
                    status_code INTEGER,
                    access_decision_id TEXT,
                    artifact_id TEXT,
                    reason_code TEXT NOT NULL,
                    redirect_chain_json TEXT NOT NULL,
                    discovery_json TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(run_id, task_id, attempt)
                );
                CREATE TABLE agentic_ledger_schema (
                    schema_name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                """,
                    )
                for trigger_name in _LEDGER_TRIGGER_NAMES:
                    self.storage.conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                self._migrate_ledger(version)
                self.storage.conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_agentic_tasks_run
                       ON agentic_tasks(run_id, task_ordinal)"""
                )
                self.storage.conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_agentic_observations_run
                       ON agentic_observations(run_id, task_id, attempt)"""
                )
                self._create_ledger_guards()
                if self._preflight_ledger_version() != (AGENTIC_LEDGER_VERSION, 2):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            except BaseException:
                self.storage.conn.rollback()
                raise
            else:
                self.storage.conn.commit()

    def _preflight_ledger_version(self) -> tuple[str, int] | None:
        current_columns = {
            "agentic_runs": {
                "run_id",
                "schema_version",
                "parent_task_id",
                "rule_id",
                "rules_version",
                "rules_sha256",
                "site_skill_id",
                "site_skill_version",
                "site_skill_package_sha256",
                "execution_plan_id",
                "execution_plan_version",
                "execution_plan_sha256",
                "read_adapter_id",
                "read_adapter_version",
                "replay_of_run_id",
                "status",
                "requests_used",
                "bytes_used",
                "pages_used",
                "files_used",
                "warnings_json",
                "required_sealed",
                "active_reads",
                "lease_owner",
                "lease_expires_at",
                "lease_epoch",
                "created_at",
                "finished_at",
            },
            "agentic_tasks": {
                "task_id",
                "schema_version",
                "run_id",
                "task_key",
                "task_ordinal",
                "kind",
                "required",
                "status",
                "requested_url",
                "query",
                "depth",
                "discovery_kind",
                "discovered_from_url",
                "parent_artifact_id",
                "adapter_id",
                "adapter_version",
                "discovery_adapter_id",
                "discovery_adapter_version",
                "attempt_count",
                "artifact_id",
                "access_decision_id",
                "failure_code",
                "replay_of_task_id",
                "created_at",
                "finished_at",
            },
            "agentic_observations": {
                "observation_id",
                "schema_version",
                "run_id",
                "parent_task_id",
                "task_id",
                "attempt",
                "status",
                "requested_url",
                "current_url",
                "final_url",
                "status_code",
                "access_decision_id",
                "artifact_id",
                "reason_code",
                "redirect_chain_json",
                "discovery_json",
                "adapter_id",
                "adapter_version",
                "observed_at",
            },
        }
        legacy_optional = {
            "agentic_runs": {
                "required_sealed",
                "pages_used",
                "active_reads",
                "lease_owner",
                "lease_expires_at",
                "lease_epoch",
            },
            "agentic_tasks": set(),
            "agentic_observations": {"current_url", "status_code"},
        }
        try:
            objects = self.storage.conn.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_master
                   WHERE name = 'agentic_ledger_schema' OR name GLOB 'agentic_*'
                      OR name GLOB 'idx_agentic_*' OR name GLOB 'guard_agentic_*'
                      OR (
                          type IN ('index', 'trigger')
                          AND tbl_name IN (
                              'agentic_runs', 'agentic_tasks',
                              'agentic_observations', 'agentic_ledger_schema'
                          )
                          AND name NOT GLOB 'sqlite_autoindex_*'
                      )"""
            ).fetchall()
            marker = next(
                (row for row in objects if row["name"] == "agentic_ledger_schema"),
                None,
            )
            if marker is None:
                if objects:
                    raise AgenticOrchestrationError("ledger.version_unsupported")
                return None
            if marker["type"] != "table":
                raise AgenticOrchestrationError("ledger.version_unsupported")
            marker_info = self.storage.conn.execute(
                "PRAGMA table_xinfo(agentic_ledger_schema)"
            ).fetchall()
            if [
                (
                    row["name"],
                    row["type"].upper(),
                    row["notnull"],
                    row["dflt_value"],
                    row["pk"],
                    row["hidden"],
                )
                for row in marker_info
            ] != [
                ("schema_name", "TEXT", 0, None, 1, 0),
                ("version", "INTEGER", 1, None, 0, 0),
            ]:
                raise AgenticOrchestrationError("ledger.version_unsupported")
            marker_sql = _normalize_ledger_sql(marker["sql"])
            if marker_sql != _canonical_ledger_table_sql(
                "agentic_ledger_schema",
                tuple(str(row["name"]) for row in marker_info),
            ):
                raise AgenticOrchestrationError("ledger.version_unsupported")
            marker_options = next(
                (
                    row
                    for row in self.storage.conn.execute("PRAGMA table_list")
                    if row["name"] == "agentic_ledger_schema"
                ),
                None,
            )
            if (
                marker_options is None
                or marker_options["type"] != "table"
                or marker_options["ncol"] != len(marker_info)
                or marker_options["wr"] != 0
                or marker_options["strict"] != 0
                or self.storage.conn.execute(
                    "PRAGMA foreign_key_list(agentic_ledger_schema)"
                ).fetchall()
            ):
                raise AgenticOrchestrationError("ledger.version_unsupported")
            versions = self.storage.conn.execute(
                "SELECT schema_name, version FROM agentic_ledger_schema"
            ).fetchall()
            if len(versions) != 1:
                raise AgenticOrchestrationError("ledger.version_unsupported")
            version = tuple(versions[0])
            if version not in {
                (AGENTIC_LEDGER_VERSION, 2),
                ("agentic-ledger.v1", 1),
            }:
                raise AgenticOrchestrationError("ledger.version_unsupported")
            actual_objects = {(row["type"], row["name"]) for row in objects}
            objects_by_name = {str(row["name"]): row for row in objects}
            table_objects = {
                ("table", name)
                for name in (
                    "agentic_runs",
                    "agentic_tasks",
                    "agentic_observations",
                    "agentic_ledger_schema",
                )
            }
            if version == (AGENTIC_LEDGER_VERSION, 2):
                expected_objects = {
                    *table_objects,
                    ("index", "idx_agentic_tasks_run"),
                    ("index", "idx_agentic_observations_run"),
                    *(("trigger", name) for name in _LEDGER_TRIGGER_NAMES),
                }
                if actual_objects != expected_objects:
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            else:
                known_legacy_objects = {
                    *table_objects,
                    ("index", "idx_agentic_tasks_run"),
                    ("index", "idx_agentic_observations_run"),
                    *(("trigger", name) for name in _LEDGER_TRIGGER_NAMES),
                }
                if not table_objects <= actual_objects or not actual_objects <= (
                    known_legacy_objects
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            for _object_type, table in table_objects:
                row = objects_by_name.get(table)
                if row is None or row["type"] != "table" or row["tbl_name"] != table:
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            for name, (table, expected_sql) in _LEDGER_INDEX_DEFINITIONS.items():
                row = objects_by_name.get(name)
                if row is None and version == ("agentic-ledger.v1", 1):
                    continue
                sql = _normalize_ledger_sql(row["sql"]) if row is not None else ""
                if (
                    row is None
                    or row["type"] != "index"
                    or row["tbl_name"] != table
                    or sql != expected_sql
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            for name, (table, expected_sha256) in _LEDGER_TRIGGER_DEFINITIONS.items():
                row = objects_by_name.get(name)
                if row is None and version == ("agentic-ledger.v1", 1):
                    continue
                sql = _normalize_ledger_sql(row["sql"]) if row is not None else ""
                actual_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                expected_hashes = {expected_sha256}
                if version == ("agentic-ledger.v1", 1):
                    expected_hashes.add(_LEGACY_LEDGER_TRIGGER_HASHES[name])
                if (
                    row is None
                    or row["type"] != "trigger"
                    or row["tbl_name"] != table
                    or not sql.startswith(f"CREATE TRIGGER {name} ")
                    or actual_sha256 not in expected_hashes
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            for table, expected in current_columns.items():
                table_info = self.storage.conn.execute(
                    f"PRAGMA table_xinfo({table})"
                ).fetchall()
                actual = {str(row["name"]) for row in table_info}
                required = (
                    expected
                    if version == (AGENTIC_LEDGER_VERSION, 2)
                    else expected - legacy_optional[table]
                )
                if (
                    not required <= actual
                    or (version == (AGENTIC_LEDGER_VERSION, 2) and actual != expected)
                    or (version == ("agentic-ledger.v1", 1) and not actual <= expected)
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
                actual_shape = tuple(
                    (
                        row["name"],
                        row["type"].upper(),
                        row["notnull"],
                        row["dflt_value"],
                        row["pk"],
                        row["hidden"],
                    )
                    for row in table_info
                )
                expected_shape = tuple(
                    (*column, 0) for column in _CURRENT_LEDGER_COLUMN_SHAPES[table]
                )
                if version == ("agentic-ledger.v1", 1):
                    expected_shape = tuple(
                        column for column in expected_shape if column[0] in actual
                    )
                if actual_shape != expected_shape:
                    raise AgenticOrchestrationError("ledger.version_unsupported")
                table_row = objects_by_name[table]
                if _normalize_ledger_sql(
                    table_row["sql"]
                ) != _canonical_ledger_table_sql(
                    table,
                    tuple(str(row["name"]) for row in table_info),
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
                table_options = next(
                    (
                        row
                        for row in self.storage.conn.execute("PRAGMA table_list")
                        if row["name"] == table
                    ),
                    None,
                )
                if (
                    table_options is None
                    or table_options["type"] != "table"
                    or table_options["ncol"] != len(table_info)
                    or table_options["wr"] != 0
                    or table_options["strict"] != 0
                    or self.storage.conn.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            for table, expected_indexes in _CURRENT_LEDGER_INDEX_SHAPES.items():
                actual_indexes = set()
                for index in self.storage.conn.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall():
                    columns = tuple(
                        row["name"]
                        for row in self.storage.conn.execute(
                            f"PRAGMA index_info({index['name']})"
                        ).fetchall()
                    )
                    actual_indexes.add(
                        (
                            index["unique"],
                            index["origin"],
                            index["partial"],
                            columns,
                        )
                    )
                if (
                    version == (AGENTIC_LEDGER_VERSION, 2)
                    and actual_indexes != expected_indexes
                ) or (
                    version == ("agentic-ledger.v1", 1)
                    and not actual_indexes <= expected_indexes
                ):
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            return version
        except AgenticOrchestrationError:
            raise
        except sqlite3.Error as exc:
            raise AgenticOrchestrationError("ledger.version_unsupported") from exc

    def _migrate_ledger(self, version: tuple[str, int] | None) -> None:
        migrations = {
            "agentic_runs": {
                "schema_version": "TEXT NOT NULL DEFAULT 'agentic-ledger.v2'",
                "required_sealed": "INTEGER NOT NULL DEFAULT 0",
                "pages_used": "INTEGER NOT NULL DEFAULT 0",
                "active_reads": "INTEGER NOT NULL DEFAULT 0",
                "lease_owner": "TEXT",
                "lease_expires_at": "TEXT",
                "lease_epoch": "INTEGER NOT NULL DEFAULT 0",
            },
            "agentic_tasks": {
                "schema_version": "TEXT NOT NULL DEFAULT 'agentic-ledger.v2'",
            },
            "agentic_observations": {
                "schema_version": "TEXT NOT NULL DEFAULT 'agentic-ledger.v2'",
                "current_url": "TEXT",
                "status_code": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = {
                str(row["name"])
                for row in self.storage.conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self.storage.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        if version is not None:
            self._normalize_ledger_times(version)
        if version is None:
            self.storage.conn.execute(
                "INSERT INTO agentic_ledger_schema (schema_name, version) VALUES (?, 2)",
                (AGENTIC_LEDGER_VERSION,),
            )
            return
        if version == (AGENTIC_LEDGER_VERSION, 2):
            for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
                invalid = self.storage.conn.execute(
                    f"SELECT 1 FROM {table} WHERE schema_version != ? LIMIT 1",
                    (AGENTIC_LEDGER_VERSION,),
                ).fetchone()
                if invalid is not None:
                    raise AgenticOrchestrationError("ledger.version_unsupported")
            return
        assert version == ("agentic-ledger.v1", 1)

        self.storage.conn.execute(
            """UPDATE agentic_runs SET required_sealed = 1
               WHERE required_sealed = 0
                 AND EXISTS (SELECT 1 FROM agentic_tasks AS tasks
                             WHERE tasks.run_id = agentic_runs.run_id AND tasks.required = 1)"""
        )
        observation_rows = self.storage.conn.execute(
            "SELECT * FROM agentic_observations ORDER BY run_id, task_id, attempt"
        ).fetchall()
        for row in observation_rows:
            try:
                redirects = json.loads(row["redirect_chain_json"])
                if not isinstance(redirects, list) or any(
                    not isinstance(item, dict) for item in redirects
                ):
                    raise ValueError("invalid legacy redirect evidence")
                current_url = (
                    redirects[-1]["to_url"] if redirects else row["requested_url"]
                )
                status_code = row["status_code"]
                if row["status"] == "completed":
                    artifact = self.storage.conn.execute(
                        """SELECT final_url, http_status FROM artifact_observations
                           WHERE artifact_id = ?""",
                        (row["artifact_id"],),
                    ).fetchone()
                    if artifact is None or artifact["final_url"] != row["final_url"]:
                        raise ValueError("legacy completed artifact is missing")
                    current_url = artifact["final_url"]
                    status_code = artifact["http_status"]
                self.storage.conn.execute(
                    """UPDATE agentic_observations
                       SET current_url = ?, status_code = ?
                       WHERE observation_id = ?""",
                    (current_url, status_code, row["observation_id"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise AgenticOrchestrationError("ledger.migration_invalid") from exc
        usage = self.storage.conn.execute(
            """SELECT artifacts.source_run_id AS run_id,
                      SUM(CASE WHEN artifacts.mime_type IN
                          ('text/html','application/xhtml+xml') THEN 1 ELSE 0 END) AS pages,
                      SUM(CASE WHEN artifacts.mime_type NOT IN
                          ('text/html','application/xhtml+xml') THEN 1 ELSE 0 END) AS files
               FROM artifact_observations AS artifacts
               JOIN agentic_tasks AS tasks ON tasks.artifact_id = artifacts.artifact_id
               GROUP BY artifacts.source_run_id"""
        ).fetchall()
        for row in usage:
            self.storage.conn.execute(
                """UPDATE agentic_runs SET pages_used = ?, files_used = ?
                   WHERE run_id = ?""",
                (row["pages"], row["files"], row["run_id"]),
            )
        for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
            self.storage.conn.execute(
                f"UPDATE {table} SET schema_version = ?", (AGENTIC_LEDGER_VERSION,)
            )
        try:
            parents = tuple(
                self._parent_from_row(row)
                for row in self.storage.conn.execute(
                    "SELECT * FROM agentic_runs ORDER BY run_id"
                ).fetchall()
            )
            for parent in parents:
                tasks = self.list_tasks(parent.run_id)
                self.list_observations(parent.run_id)
                self._validate_terminal_run_evidence(parent.run_id)
                if parent.status == "running":
                    raise AgenticOrchestrationError("ledger.migration_invalid")
                if (
                    _derive_parent_outcome(tasks, warnings=parent.warnings)
                    != parent.status
                ):
                    raise AgenticOrchestrationError("ledger.migration_invalid")
        except AgenticOrchestrationError as exc:
            if exc.reason_code == "ledger.migration_invalid":
                raise
            raise AgenticOrchestrationError("ledger.migration_invalid") from exc
        self._rebuild_migrated_v2_tables()
        self.storage.conn.execute("DELETE FROM agentic_ledger_schema")
        self.storage.conn.execute(
            "INSERT INTO agentic_ledger_schema (schema_name, version) VALUES (?, 2)",
            (AGENTIC_LEDGER_VERSION,),
        )

    def _normalize_ledger_times(self, version: tuple[str, int]) -> None:
        columns = {
            "agentic_runs": ("created_at", "finished_at", "lease_expires_at"),
            "agentic_tasks": ("created_at", "finished_at"),
            "agentic_observations": ("observed_at",),
        }
        try:
            for table, names in columns.items():
                for name in names:
                    rows = self.storage.conn.execute(
                        f"SELECT rowid, {name} FROM {table} WHERE {name} IS NOT NULL"
                    ).fetchall()
                    for row in rows:
                        normalized = _normalize_persisted_time(row[name])
                        if normalized != row[name]:
                            self.storage.conn.execute(
                                f"UPDATE {table} SET {name} = ? WHERE rowid = ?",
                                (normalized, row["rowid"]),
                            )
        except (TypeError, ValueError) as exc:
            reason = (
                "ledger.version_unsupported"
                if version == (AGENTIC_LEDGER_VERSION, 2)
                else "ledger.migration_invalid"
            )
            raise AgenticOrchestrationError(reason) from exc

    def _rebuild_migrated_v2_tables(self) -> None:
        _execute_sql_script(
            self.storage.conn,
            """
            CREATE TABLE agentic_runs_v2_migration (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                parent_task_id TEXT NOT NULL UNIQUE,
                rule_id TEXT NOT NULL,
                rules_version TEXT NOT NULL,
                rules_sha256 TEXT NOT NULL,
                site_skill_id TEXT NOT NULL,
                site_skill_version TEXT NOT NULL,
                site_skill_package_sha256 TEXT NOT NULL,
                execution_plan_id TEXT NOT NULL,
                execution_plan_version TEXT NOT NULL,
                execution_plan_sha256 TEXT NOT NULL,
                read_adapter_id TEXT NOT NULL,
                read_adapter_version TEXT NOT NULL,
                replay_of_run_id TEXT,
                status TEXT NOT NULL,
                requests_used INTEGER NOT NULL,
                bytes_used INTEGER NOT NULL,
                pages_used INTEGER NOT NULL DEFAULT 0,
                files_used INTEGER NOT NULL,
                warnings_json TEXT NOT NULL,
                required_sealed INTEGER NOT NULL DEFAULT 0,
                active_reads INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_expires_at TEXT,
                lease_epoch INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE agentic_tasks_v2_migration (
                task_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                run_id TEXT NOT NULL,
                task_key TEXT NOT NULL,
                task_ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                required INTEGER NOT NULL,
                status TEXT NOT NULL,
                requested_url TEXT,
                query TEXT,
                depth INTEGER NOT NULL,
                discovery_kind TEXT NOT NULL,
                discovered_from_url TEXT,
                parent_artifact_id TEXT,
                adapter_id TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                discovery_adapter_id TEXT,
                discovery_adapter_version TEXT,
                attempt_count INTEGER NOT NULL,
                artifact_id TEXT,
                access_decision_id TEXT,
                failure_code TEXT,
                replay_of_task_id TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(run_id, task_key)
            );
            CREATE TABLE agentic_observations_v2_migration (
                observation_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL DEFAULT 'agentic-ledger.v2',
                run_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                requested_url TEXT NOT NULL,
                current_url TEXT,
                final_url TEXT,
                status_code INTEGER,
                access_decision_id TEXT,
                artifact_id TEXT,
                reason_code TEXT NOT NULL,
                redirect_chain_json TEXT NOT NULL,
                discovery_json TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(run_id, task_id, attempt)
            );
            """,
        )
        for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
            columns = ", ".join(
                str(column[0]) for column in _CURRENT_LEDGER_COLUMN_SHAPES[table]
            )
            self.storage.conn.execute(
                f"""INSERT INTO {table}_v2_migration ({columns})
                    SELECT {columns} FROM {table}"""
            )
        for table in (
            "agentic_observations",
            "agentic_tasks",
            "agentic_runs",
        ):
            self.storage.conn.execute(f"DROP TABLE {table}")
        for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
            columns = tuple(
                str(column[0]) for column in _CURRENT_LEDGER_COLUMN_SHAPES[table]
            )
            self.storage.conn.execute(_canonical_ledger_table_sql(table, columns))
            rendered_columns = ", ".join(columns)
            self.storage.conn.execute(
                f"""INSERT INTO {table} ({rendered_columns})
                    SELECT {rendered_columns} FROM {table}_v2_migration"""
            )
            self.storage.conn.execute(f"DROP TABLE {table}_v2_migration")

    def _create_ledger_guards(self) -> None:
        self._drop_ledger_guards()

    def _drop_ledger_guards(self) -> None:
        for name in _LEDGER_TRIGGER_NAMES:
            self.storage.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        _execute_sql_script(
            self.storage.conn,
            """
            CREATE TRIGGER guard_agentic_runs_insert
            BEFORE INSERT ON agentic_runs
            WHEN NEW.schema_version != 'agentic-ledger.v2'
              OR NEW.status != 'running'
              OR NEW.requests_used < 0 OR NEW.bytes_used < 0 OR NEW.pages_used < 0 OR NEW.files_used < 0
              OR NEW.required_sealed NOT IN (0, 1) OR NEW.active_reads < 0
              OR NEW.lease_epoch < 0
              OR (NEW.lease_owner IS NULL) != (NEW.lease_expires_at IS NULL)
            BEGIN SELECT RAISE(ABORT, 'agentic ledger guard'); END;

            CREATE TRIGGER guard_agentic_runs_update
            BEFORE UPDATE ON agentic_runs
            WHEN NEW.schema_version != 'agentic-ledger.v2'
              OR NEW.run_id != OLD.run_id
              OR NEW.status NOT IN ('running','completed','partial','rejected','failed','cancelled')
              OR NEW.requests_used < 0 OR NEW.bytes_used < 0 OR NEW.pages_used < 0 OR NEW.files_used < 0
              OR NEW.required_sealed NOT IN (0, 1) OR NEW.active_reads < 0
              OR NEW.lease_epoch < OLD.lease_epoch
              OR (NEW.lease_owner IS NULL) != (NEW.lease_expires_at IS NULL)
              OR NEW.parent_task_id != OLD.parent_task_id OR NEW.rule_id != OLD.rule_id
              OR NEW.rules_version != OLD.rules_version OR NEW.rules_sha256 != OLD.rules_sha256
              OR NEW.site_skill_id != OLD.site_skill_id
              OR NEW.site_skill_version != OLD.site_skill_version
              OR NEW.site_skill_package_sha256 != OLD.site_skill_package_sha256
              OR NEW.execution_plan_id != OLD.execution_plan_id
              OR NEW.execution_plan_version != OLD.execution_plan_version
              OR NEW.execution_plan_sha256 != OLD.execution_plan_sha256
              OR NEW.read_adapter_id != OLD.read_adapter_id
              OR NEW.read_adapter_version != OLD.read_adapter_version
              OR NEW.replay_of_run_id IS NOT OLD.replay_of_run_id
              OR NEW.created_at != OLD.created_at
              OR (OLD.status IN ('completed','partial','rejected','failed','cancelled') AND (
                    NEW.status IS NOT OLD.status OR NEW.warnings_json IS NOT OLD.warnings_json
                    OR NEW.finished_at IS NOT OLD.finished_at OR NEW.requests_used != OLD.requests_used
                    OR NEW.bytes_used != OLD.bytes_used OR NEW.pages_used != OLD.pages_used
                    OR NEW.files_used != OLD.files_used
                    OR NEW.required_sealed != OLD.required_sealed OR NEW.active_reads != OLD.active_reads
                    OR NEW.lease_owner IS NOT OLD.lease_owner OR NEW.lease_expires_at IS NOT OLD.lease_expires_at
                    OR NEW.lease_epoch != OLD.lease_epoch))
            BEGIN SELECT RAISE(ABORT, 'agentic ledger guard'); END;

            CREATE TRIGGER guard_agentic_tasks_insert
            BEFORE INSERT ON agentic_tasks
            WHEN NEW.schema_version != 'agentic-ledger.v2'
              OR NEW.required NOT IN (0, 1) OR NEW.status != 'queued'
              OR NEW.task_ordinal < 0 OR NEW.depth < 0 OR NEW.attempt_count != 0
              OR NOT EXISTS (SELECT 1 FROM agentic_runs AS runs
                             WHERE runs.run_id = NEW.run_id AND runs.status = 'running')
              OR (NEW.required = 1 AND EXISTS (SELECT 1 FROM agentic_runs AS runs
                                               WHERE runs.run_id = NEW.run_id AND runs.required_sealed = 1))
            BEGIN SELECT RAISE(ABORT, 'agentic ledger guard'); END;

            CREATE TRIGGER guard_agentic_tasks_update
            BEFORE UPDATE ON agentic_tasks
            WHEN NEW.schema_version != 'agentic-ledger.v2'
              OR NEW.task_id != OLD.task_id
              OR NEW.required NOT IN (0, 1)
              OR NEW.status NOT IN ('queued','running','completed','partial','rejected','failed','cancelled')
              OR NEW.task_ordinal < 0 OR NEW.depth < 0 OR NEW.attempt_count < 0
              OR NEW.run_id != OLD.run_id OR NEW.task_key != OLD.task_key
              OR NEW.task_ordinal != OLD.task_ordinal OR NEW.kind != OLD.kind
              OR NEW.required != OLD.required OR NEW.requested_url IS NOT OLD.requested_url
              OR NEW.query IS NOT OLD.query OR NEW.depth != OLD.depth
              OR NEW.discovery_kind != OLD.discovery_kind
              OR NEW.discovered_from_url IS NOT OLD.discovered_from_url
              OR NEW.parent_artifact_id IS NOT OLD.parent_artifact_id
              OR NEW.adapter_id != OLD.adapter_id OR NEW.adapter_version != OLD.adapter_version
              OR NEW.discovery_adapter_id IS NOT OLD.discovery_adapter_id
              OR NEW.discovery_adapter_version IS NOT OLD.discovery_adapter_version
              OR NEW.replay_of_task_id IS NOT OLD.replay_of_task_id OR NEW.created_at != OLD.created_at
              OR (OLD.status IN ('completed','partial','rejected','failed','cancelled') AND (
                    NEW.status IS NOT OLD.status OR NEW.attempt_count != OLD.attempt_count
                    OR NEW.artifact_id IS NOT OLD.artifact_id
                    OR NEW.access_decision_id IS NOT OLD.access_decision_id
                    OR NEW.failure_code IS NOT OLD.failure_code OR NEW.finished_at IS NOT OLD.finished_at))
            BEGIN SELECT RAISE(ABORT, 'agentic ledger guard'); END;

            CREATE TRIGGER guard_agentic_observations_insert
            BEFORE INSERT ON agentic_observations
            WHEN NEW.schema_version != 'agentic-ledger.v2'
              OR NEW.status NOT IN ('completed','partial','rejected','failed','cancelled')
              OR NEW.attempt < 1
              OR NOT EXISTS (SELECT 1 FROM agentic_runs AS runs
                             WHERE runs.run_id = NEW.run_id AND runs.parent_task_id = NEW.parent_task_id)
              OR NOT EXISTS (SELECT 1 FROM agentic_tasks AS tasks
                             WHERE tasks.task_id = NEW.task_id AND tasks.run_id = NEW.run_id)
            BEGIN SELECT RAISE(ABORT, 'agentic ledger guard'); END;

            CREATE TRIGGER guard_agentic_observations_running_insert
            BEFORE INSERT ON agentic_observations
            WHEN NOT EXISTS (SELECT 1 FROM agentic_runs AS runs
                             WHERE runs.run_id = NEW.run_id AND runs.status = 'running')
              OR NOT EXISTS (SELECT 1 FROM agentic_tasks AS tasks
                             WHERE tasks.task_id = NEW.task_id
                               AND tasks.run_id = NEW.run_id
                               AND tasks.status = 'running')
            BEGIN SELECT RAISE(ABORT, 'agentic observation requires running state'); END;

            CREATE TRIGGER guard_agentic_runs_terminal_evidence
            BEFORE UPDATE OF status ON agentic_runs
            WHEN OLD.status = 'running'
              AND NEW.status IN ('completed','partial','rejected','failed','cancelled')
              AND (
                NEW.required_sealed != 1 OR NEW.active_reads != 0
                OR EXISTS (SELECT 1 FROM agentic_tasks AS tasks
                           WHERE tasks.run_id = NEW.run_id
                             AND tasks.status NOT IN ('completed','partial','rejected','failed','cancelled'))
                OR (NEW.status = 'completed' AND EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.status != 'completed'
                ))
                OR (NEW.status = 'failed' AND NEW.warnings_json != '["run.interrupted"]'
                    AND NOT EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.required = 1
                      AND tasks.status = 'failed'
                ))
                OR (NEW.status = 'rejected' AND NOT EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.required = 1
                      AND tasks.status = 'rejected'
                ))
                OR (NEW.status = 'cancelled' AND NEW.warnings_json != '["run.cancelled"]'
                    AND NOT EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.status = 'cancelled'
                ))
                OR (NEW.status = 'partial' AND NOT EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.status = 'completed'
                ))
                OR EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.kind = 'read'
                      AND tasks.attempt_count != (
                          SELECT COUNT(*) FROM agentic_observations AS observations
                          WHERE observations.task_id = tasks.task_id
                      )
                )
                OR EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.kind = 'search'
                      AND EXISTS (SELECT 1 FROM agentic_observations AS observations
                                  WHERE observations.task_id = tasks.task_id)
                )
                OR EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.kind = 'read'
                      AND tasks.status = 'completed'
                      AND (
                        tasks.attempt_count < 1 OR tasks.artifact_id IS NULL
                        OR tasks.access_decision_id IS NULL OR tasks.failure_code IS NOT NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM agentic_observations AS observations
                            WHERE observations.task_id = tasks.task_id
                              AND observations.attempt = tasks.attempt_count
                              AND observations.status = 'completed'
                              AND observations.artifact_id = tasks.artifact_id
                              AND observations.access_decision_id = tasks.access_decision_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM artifact_observations AS artifacts
                            WHERE artifacts.artifact_id = tasks.artifact_id
                              AND artifacts.source_run_id = tasks.run_id
                              AND artifacts.normalized_source_identity = tasks.requested_url
                              AND artifacts.access_decision_id = tasks.access_decision_id
                              AND artifacts.adapter_id = tasks.adapter_id
                              AND artifacts.adapter_version = tasks.adapter_version
                        )
                      )
                )
                OR EXISTS (
                    SELECT 1 FROM agentic_tasks AS tasks
                    WHERE tasks.run_id = NEW.run_id AND tasks.kind = 'read'
                      AND tasks.status IN ('failed','rejected') AND tasks.attempt_count > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM agentic_observations AS observations
                          WHERE observations.task_id = tasks.task_id
                            AND observations.attempt = tasks.attempt_count
                            AND observations.status = tasks.status
                            AND observations.artifact_id IS NULL
                      )
                )
              )
            BEGIN SELECT RAISE(ABORT, 'agentic terminal evidence invalid'); END;

            CREATE TRIGGER guard_agentic_observations_update
            BEFORE UPDATE ON agentic_observations
            BEGIN SELECT RAISE(ABORT, 'agentic observation immutable'); END;
            CREATE TRIGGER guard_agentic_observations_delete
            BEFORE DELETE ON agentic_observations
            BEGIN SELECT RAISE(ABORT, 'agentic observation immutable'); END;
            CREATE TRIGGER guard_agentic_tasks_delete
            BEFORE DELETE ON agentic_tasks
            BEGIN SELECT RAISE(ABORT, 'agentic task immutable'); END;
            CREATE TRIGGER guard_agentic_runs_delete
            BEFORE DELETE ON agentic_runs
            BEGIN SELECT RAISE(ABORT, 'agentic run immutable'); END;
            """,
        )

    def _commit_if_standalone(self) -> None:
        if not self.storage.execution_transaction_active:
            self.storage.conn.commit()

    def transaction(self):
        return _ExecutionTransaction(self.storage)

    def _assert_run_fence(self, run_id: str) -> tuple[str, int] | None:
        row = self.storage.conn.execute(
            """SELECT lease_owner, lease_expires_at, lease_epoch
               FROM agentic_runs WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise AgenticOrchestrationError("run.not_found")
        owner = row["lease_owner"]
        epoch = row["lease_epoch"]
        if owner is None:
            return None
        expected = self._lease_fences.get(run_id)
        if (
            expected != (owner, epoch)
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= _format_time(self.clock())
        ):
            raise AgenticOrchestrationError("run.lease_lost")
        return expected

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        owner: str,
        ttl_seconds: int = 300,
    ) -> int | None:
        if (
            not isinstance(owner, str)
            or not _IDENTITY_RE.fullmatch(owner)
            or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= 3600
        ):
            raise AgenticOrchestrationError("run.lease_invalid")
        now = self.clock()
        acquired_at = _format_time(now)
        expires_at = _format_time(now + timedelta(seconds=ttl_seconds))
        with self._turn():
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs
                   SET lease_owner = ?, lease_expires_at = ?,
                       lease_epoch = lease_epoch + 1
                   WHERE run_id = ? AND status = 'running'
                     AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at <= ?)""",
                (owner, expires_at, run_id, owner, acquired_at),
            ).rowcount
            fence = None
            if changed == 1:
                row = self.storage.conn.execute(
                    "SELECT lease_epoch FROM agentic_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                assert row is not None
                fence = int(row["lease_epoch"])
                self._lease_fences[run_id] = (owner, fence)
            self._commit_if_standalone()
        return fence

    def release_run_lease(self, run_id: str, *, owner: str) -> None:
        with self._turn():
            row = self.storage.conn.execute(
                "SELECT status, lease_owner, lease_epoch FROM agentic_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("run.not_found")
            expected = self._lease_fences.get(run_id)
            if row["status"] == "running" and (
                row["lease_owner"] not in {None, owner}
                or (
                    row["lease_owner"] is not None
                    and expected != (owner, row["lease_epoch"])
                )
            ):
                raise AgenticOrchestrationError("run.lease_conflict")
            if row["status"] == "running":
                changed = self.storage.conn.execute(
                    """UPDATE agentic_runs SET lease_owner = NULL, lease_expires_at = NULL
                       WHERE run_id = ? AND lease_owner = ? AND lease_epoch = ?""",
                    (run_id, owner, expected[1] if expected is not None else -1),
                ).rowcount
                if row["lease_owner"] is not None and changed != 1:
                    raise AgenticOrchestrationError("run.lease_conflict")
                self._commit_if_standalone()
            self._lease_fences.pop(run_id, None)

    def reserve_read_budget(
        self,
        run_id: str,
        *,
        max_requests: int,
        max_bytes: int,
        max_files: int,
        max_concurrency: int,
    ) -> int:
        limits = (max_requests, max_bytes, max_files, max_concurrency)
        if any(type(value) is not int or value < 1 for value in limits):
            raise AgenticOrchestrationError("budget.limit_invalid")
        with self._turn():
            fence = self._assert_run_fence(run_id)
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("run.not_found")
            if row["status"] != "running":
                raise AgenticOrchestrationError("run.transition_invalid")
            if row["requests_used"] >= max_requests:
                raise AgenticOrchestrationError("budget.requests_exhausted")
            if row["bytes_used"] >= max_bytes:
                raise AgenticOrchestrationError("budget.bytes_exhausted")
            if row["files_used"] >= max_files:
                raise AgenticOrchestrationError("budget.files_exhausted")
            if row["active_reads"] >= max_concurrency:
                raise AgenticOrchestrationError("budget.concurrency_exhausted")
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs
                   SET requests_used = requests_used + 1, active_reads = active_reads + 1
                   WHERE run_id = ? AND status = 'running'
                     AND requests_used < ? AND bytes_used < ? AND files_used < ?
                     AND active_reads < ? AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    run_id,
                    max_requests,
                    max_bytes,
                    max_files,
                    max_concurrency,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("budget.reservation_conflict")
            remaining = max_bytes - int(row["bytes_used"])
            self._commit_if_standalone()
        return remaining

    def begin_read_budget(
        self,
        run_id: str,
        *,
        max_requests: int,
        max_bytes: int,
        max_files: int,
        max_concurrency: int,
    ) -> int:
        limits = (max_requests, max_bytes, max_files, max_concurrency)
        if any(type(value) is not int or value < 1 for value in limits):
            raise AgenticOrchestrationError("budget.limit_invalid")
        with self._turn():
            fence = self._assert_run_fence(run_id)
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("run.not_found")
            if row["status"] != "running":
                raise AgenticOrchestrationError("run.transition_invalid")
            for used, maximum, reason in (
                (row["requests_used"], max_requests, "budget.requests_exhausted"),
                (row["bytes_used"], max_bytes, "budget.bytes_exhausted"),
                (row["active_reads"], max_concurrency, "budget.concurrency_exhausted"),
            ):
                if used >= maximum:
                    raise AgenticOrchestrationError(reason)
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET active_reads = active_reads + 1
                   WHERE run_id = ? AND status = 'running'
                     AND requests_used < ? AND bytes_used < ? AND active_reads < ?
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    run_id,
                    max_requests,
                    max_bytes,
                    max_concurrency,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("budget.reservation_conflict")
            remaining = max_bytes - int(row["bytes_used"])
            self._commit_if_standalone()
        return remaining

    def consume_target_request(self, run_id: str, *, max_requests: int) -> None:
        if type(max_requests) is not int or max_requests < 1:
            raise AgenticOrchestrationError("budget.limit_invalid")
        with self._turn():
            fence = self._assert_run_fence(run_id)
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET requests_used = requests_used + 1
                   WHERE run_id = ? AND status = 'running'
                     AND active_reads > 0 AND requests_used < ?
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    run_id,
                    max_requests,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                row = self.storage.conn.execute(
                    "SELECT requests_used FROM agentic_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                self._commit_if_standalone()
                if row is not None and row["requests_used"] >= max_requests:
                    raise AgenticOrchestrationError("budget.requests_exhausted")
                raise AgenticOrchestrationError("budget.reservation_missing")
            self._commit_if_standalone()

    def claim_target_send(
        self,
        run_id: str,
        *,
        max_requests: int,
        timeout_seconds: float,
    ) -> _TargetSendClaim:
        if (
            type(max_requests) is not int
            or max_requests < 1
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise AgenticOrchestrationError("budget.limit_invalid")
        with self._turn():
            fence = self._assert_run_fence(run_id)
            if fence is None:
                raise AgenticOrchestrationError("run.lease_lost")
            row = self.storage.conn.execute(
                """SELECT lease_expires_at, requests_used FROM agentic_runs
                   WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            assert row is not None
            if row["requests_used"] >= max_requests:
                raise AgenticOrchestrationError("budget.requests_exhausted")
            prior_expiry = str(row["lease_expires_at"])
            claim_expiry = max(
                prior_expiry,
                _format_time(self.clock() + timedelta(seconds=timeout_seconds)),
            )
            claim_owner = f"send-claim-{fence[1]}-{hashlib.sha256(fence[0].encode()).hexdigest()[:16]}"
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs
                   SET requests_used = requests_used + 1,
                       lease_owner = ?, lease_expires_at = ?
                   WHERE run_id = ? AND status = 'running' AND active_reads > 0
                     AND requests_used < ? AND lease_owner = ? AND lease_epoch = ?
                     AND lease_expires_at > ?""",
                (
                    claim_owner,
                    claim_expiry,
                    run_id,
                    max_requests,
                    fence[0],
                    fence[1],
                    _format_time(self.clock()),
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")
            self._lease_fences[run_id] = (claim_owner, fence[1])
            self._commit_if_standalone()
        return _TargetSendClaim(
            run_id=run_id,
            owner=fence[0],
            claim_owner=claim_owner,
            epoch=fence[1],
            lease_expires_at=claim_expiry,
        )

    def release_target_send(self, claim: _TargetSendClaim) -> None:
        if type(claim) is not _TargetSendClaim:
            raise AgenticOrchestrationError("run.lease_lost")
        with self._turn():
            row = self.storage.conn.execute(
                """SELECT status, lease_owner, lease_expires_at, lease_epoch
                   FROM agentic_runs WHERE run_id = ?""",
                (claim.run_id,),
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("run.not_found")
            if (
                row["lease_epoch"] == claim.epoch
                and row["lease_owner"] == claim.owner
                and row["lease_expires_at"] == claim.lease_expires_at
            ):
                self._lease_fences[claim.run_id] = (claim.owner, claim.epoch)
                return
            if row["lease_epoch"] > claim.epoch:
                if self._lease_fences.get(claim.run_id) == (
                    claim.claim_owner,
                    claim.epoch,
                ):
                    self._lease_fences.pop(claim.run_id, None)
                return
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET lease_owner = ?, lease_expires_at = ?
                   WHERE run_id = ? AND status = 'running'
                     AND lease_owner = ? AND lease_epoch = ?""",
                (
                    claim.owner,
                    claim.lease_expires_at,
                    claim.run_id,
                    claim.claim_owner,
                    claim.epoch,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")
            self._lease_fences[claim.run_id] = (claim.owner, claim.epoch)
            self._commit_if_standalone()

    def renew_target_send(
        self,
        claim: _TargetSendClaim,
        *,
        timeout_seconds: float,
    ) -> _TargetSendClaim:
        if (
            type(claim) is not _TargetSendClaim
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise AgenticOrchestrationError("run.lease_lost")
        with self._turn():
            now = _format_time(self.clock())
            row = self.storage.conn.execute(
                """SELECT status, lease_owner, lease_expires_at, lease_epoch
                   FROM agentic_runs WHERE run_id = ?""",
                (claim.run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["lease_owner"] != claim.claim_owner
                or row["lease_epoch"] != claim.epoch
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now
            ):
                raise AgenticOrchestrationError("run.lease_lost")
            renewed_expiry = max(
                str(row["lease_expires_at"]),
                _format_time(self.clock() + timedelta(seconds=float(timeout_seconds))),
            )
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET lease_expires_at = ?
                   WHERE run_id = ? AND status = 'running' AND active_reads > 0
                     AND lease_owner = ? AND lease_epoch = ?
                     AND lease_expires_at > ?""",
                (
                    renewed_expiry,
                    claim.run_id,
                    claim.claim_owner,
                    claim.epoch,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")
            self._lease_fences[claim.run_id] = (claim.claim_owner, claim.epoch)
            self._commit_if_standalone()
        return _TargetSendClaim(
            run_id=claim.run_id,
            owner=claim.owner,
            claim_owner=claim.claim_owner,
            epoch=claim.epoch,
            lease_expires_at=renewed_expiry,
        )

    def finish_read_budget(
        self,
        run_id: str,
        *,
        bytes_read: int,
        pages: int = 0,
        files: int,
        max_bytes: int,
        max_pages: int = 1_000_000,
        max_files: int,
    ) -> None:
        if (
            type(bytes_read) is not int
            or bytes_read < 0
            or type(pages) is not int
            or pages not in {0, 1}
            or type(files) is not int
            or files not in {0, 1}
            or pages + files > 1
        ):
            raise AgenticOrchestrationError("budget.usage_invalid")
        with self._turn():
            fence = self._assert_run_fence(run_id)
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs
                   SET bytes_used = bytes_used + ?, pages_used = pages_used + ?,
                       files_used = files_used + ?,
                       active_reads = active_reads - 1
                   WHERE run_id = ? AND status = 'running' AND active_reads > 0
                     AND bytes_used + ? <= ? AND pages_used + ? <= ?
                     AND files_used + ? <= ?
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    bytes_read,
                    pages,
                    files,
                    run_id,
                    bytes_read,
                    max_bytes,
                    pages,
                    max_pages,
                    files,
                    max_files,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                row = self.storage.conn.execute(
                    """SELECT bytes_used, pages_used, files_used
                       FROM agentic_runs WHERE run_id = ?""",
                    (run_id,),
                ).fetchone()
                if row is not None and row["bytes_used"] + bytes_read > max_bytes:
                    raise AgenticOrchestrationError("budget.bytes_exhausted")
                if row is not None and row["pages_used"] + pages > max_pages:
                    raise AgenticOrchestrationError("budget.pages_exhausted")
                if row is not None and row["files_used"] + files > max_files:
                    raise AgenticOrchestrationError("budget.files_exhausted")
                raise AgenticOrchestrationError("budget.reservation_missing")
            self._commit_if_standalone()

    def seal_required_tasks(self, run_id: str) -> AgenticParentTask:
        with self._turn():
            fence = self._assert_run_fence(run_id)
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET required_sealed = 1
                   WHERE run_id = ? AND status = 'running'
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.transition_invalid")
            self._commit_if_standalone()
        return self.require_run(run_id)

    def interrupt_run(self, run_id: str, *, cancelled: bool) -> None:
        status = "cancelled" if cancelled else "failed"
        reason = "run.cancelled" if cancelled else "run.interrupted"
        finished_at = _format_time(self.clock())
        with self.transaction():
            fence = self._assert_run_fence(run_id)
            row = self.storage.conn.execute(
                "SELECT status FROM agentic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["status"] in _TERMINAL_TASK_STATUSES:
                return
            self.storage.conn.execute(
                """UPDATE agentic_tasks
                   SET status = ?, failure_code = ?, finished_at = ?,
                       attempt_count = (
                           SELECT COUNT(*) FROM agentic_observations AS observations
                           WHERE observations.task_id = agentic_tasks.task_id
                       )
                   WHERE run_id = ? AND status IN ('queued', 'running')
                     AND EXISTS (
                         SELECT 1 FROM agentic_runs AS runs
                         WHERE runs.run_id = agentic_tasks.run_id
                           AND runs.status = 'running'
                           AND runs.lease_owner IS ? AND runs.lease_epoch = ?
                     )""",
                (
                    status,
                    reason,
                    finished_at,
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            )
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs
                   SET status = ?, warnings_json = ?, finished_at = ?, active_reads = 0,
                       lease_owner = NULL, lease_expires_at = NULL, required_sealed = 1
                   WHERE run_id = ? AND status = 'running'
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    status,
                    canonical_json([reason]),
                    finished_at,
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")

    def create_run(
        self,
        *,
        run_id: str,
        rules: AgenticSiteRules,
        authority: AgenticAuthority,
        replay_of_run_id: str | None = None,
    ) -> AgenticParentTask:
        if not _SOURCE_RUN_RE.fullmatch(run_id):
            raise AgenticOrchestrationError("run.identity_invalid")
        if replay_of_run_id is not None:
            source = self.get_run(replay_of_run_id)
            if source is None:
                raise AgenticOrchestrationError("replay.run_not_found")
            if source.status not in _TERMINAL_TASK_STATUSES:
                raise AgenticOrchestrationError("replay.source_not_terminal")
            if (
                source.rule_id,
                source.rules_version,
                source.rules_sha256,
                source.site_skill_id,
                source.site_skill_version,
                source.site_skill_package_sha256,
                source.execution_plan_id,
                source.execution_plan_version,
                source.execution_plan_sha256,
                source.read_adapter_id,
                source.read_adapter_version,
            ) != (
                rules.rule_id,
                rules.version,
                rules.rules_sha256,
                authority.site_skill_id,
                authority.site_skill_version,
                authority.site_skill_package_sha256,
                authority.execution_plan_id,
                authority.execution_plan_version,
                authority.execution_plan_sha256,
                authority.read_adapter_id,
                authority.read_adapter_version,
            ):
                raise AgenticOrchestrationError("replay.authority_mismatch")
        parent_task_id = _stable_id(
            "parent-task",
            {"run_id": run_id, "schema_version": AGENTIC_ORCHESTRATION_VERSION},
        )
        created_at = _format_time(self.clock())
        values = (
            run_id,
            parent_task_id,
            rules.rule_id,
            rules.version,
            rules.rules_sha256,
            authority.site_skill_id,
            authority.site_skill_version,
            authority.site_skill_package_sha256,
            authority.execution_plan_id,
            authority.execution_plan_version,
            authority.execution_plan_sha256,
            authority.read_adapter_id,
            authority.read_adapter_version,
            replay_of_run_id,
            "running",
            0,
            0,
            0,
            0,
            "[]",
            created_at,
        )
        with self._turn():
            self.storage.conn.execute(
                """INSERT OR IGNORE INTO agentic_runs (
                       run_id, parent_task_id, rule_id, rules_version, rules_sha256,
                       site_skill_id, site_skill_version, site_skill_package_sha256,
                       execution_plan_id, execution_plan_version, execution_plan_sha256,
                       read_adapter_id, read_adapter_version, replay_of_run_id, status,
                       requests_used, bytes_used, pages_used, files_used,
                       warnings_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            self._commit_if_standalone()
        parent = self.require_run(run_id)
        expected = values[1:14]
        actual = (
            parent.parent_task_id,
            parent.rule_id,
            parent.rules_version,
            parent.rules_sha256,
            parent.site_skill_id,
            parent.site_skill_version,
            parent.site_skill_package_sha256,
            parent.execution_plan_id,
            parent.execution_plan_version,
            parent.execution_plan_sha256,
            parent.read_adapter_id,
            parent.read_adapter_version,
            parent.replay_of_run_id,
        )
        if actual != expected:
            raise AgenticOrchestrationError("run.replay_conflict")
        return parent

    def get_run(self, run_id: str) -> AgenticParentTask | None:
        with self._turn():
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._parent_from_row(row) if row is not None else None

    def require_run(self, run_id: str) -> AgenticParentTask:
        parent = self.get_run(run_id)
        if parent is None:
            raise AgenticOrchestrationError("run.not_found")
        return parent

    def create_task(
        self,
        *,
        run_id: str,
        task_key: str,
        kind: Literal["read", "search"],
        required: bool,
        requested_url: str | None = None,
        query: str | None = None,
        depth: int = 0,
        discovery_kind: str,
        discovered_from_url: str | None = None,
        parent_artifact_id: str | None = None,
        adapter_id: str = "web_http",
        adapter_version: str = "1.0.0",
        discovery_adapter_id: str | None = None,
        discovery_adapter_version: str | None = None,
        replay_of_run_id: str | None = None,
    ) -> AgenticChildTask:
        parent = self.get_run(run_id)
        if parent is None:
            raise AgenticOrchestrationError("run.not_found")
        if parent.status != "running":
            raise AgenticOrchestrationError("run.transition_invalid")
        if (
            type(required) is not bool
            or type(depth) is not int
            or depth < 0
            or not isinstance(task_key, str)
            or not task_key
            or len(task_key) > 4096
        ):
            raise AgenticOrchestrationError("task.invalid")
        try:
            _validate_non_sensitive_text(task_key, location="task key")
        except ValueError as exc:
            raise AgenticOrchestrationError("task.invalid") from exc
        if kind == "read" and (requested_url is None or query is not None):
            raise AgenticOrchestrationError("task.invalid")
        if kind == "search" and (query is None or requested_url is not None):
            raise AgenticOrchestrationError("task.invalid")
        if kind not in {"read", "search"} or discovery_kind not in {
            "seed",
            "search",
            "link",
            "crawler",
        }:
            raise AgenticOrchestrationError("task.invalid")
        try:
            if requested_url is not None:
                canonicalize_access_url(requested_url)
            if discovered_from_url is not None:
                canonicalize_access_url(discovered_from_url)
            if query is not None:
                AgenticQuery(text=query)
        except ValueError as exc:
            raise AgenticOrchestrationError("task.invalid") from exc
        if not _IDENTITY_RE.fullmatch(adapter_id) or not _SEMVER_RE.fullmatch(
            adapter_version
        ):
            raise AgenticOrchestrationError("task.invalid")
        if (discovery_adapter_id is None) != (discovery_adapter_version is None):
            raise AgenticOrchestrationError("task.invalid")
        if discovery_adapter_id is not None and (
            not _IDENTITY_RE.fullmatch(discovery_adapter_id)
            or not _SEMVER_RE.fullmatch(discovery_adapter_version or "")
        ):
            raise AgenticOrchestrationError("task.invalid")
        if parent_artifact_id is not None and not re.fullmatch(
            r"artifact-[0-9a-f]{24}", parent_artifact_id
        ):
            raise AgenticOrchestrationError("task.invalid")
        task_id = _stable_id("child-task", {"run_id": run_id, "task_key": task_key})
        replay_of_task_id = None
        if replay_of_run_id is not None:
            if parent.replay_of_run_id != replay_of_run_id:
                raise AgenticOrchestrationError("replay.run_mismatch")
            with self._turn():
                prior = self.storage.conn.execute(
                    """SELECT task_id FROM agentic_tasks
                       WHERE run_id = ? AND task_key = ? AND kind = ?""",
                    (replay_of_run_id, task_key, kind),
                ).fetchone()
            replay_of_task_id = prior["task_id"] if prior is not None else None
        with self._turn():
            fence = self._assert_run_fence(run_id)
            run_row = self.storage.conn.execute(
                "SELECT status, required_sealed FROM agentic_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgenticOrchestrationError("run.not_found")
            if run_row["status"] != "running":
                raise AgenticOrchestrationError("run.transition_invalid")
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if required and run_row["required_sealed"] == 1 and row is None:
                raise AgenticOrchestrationError("task.required_set_sealed")
            if row is None:
                try:
                    _validate_task_discovery(
                        kind=kind,
                        discovery_kind=discovery_kind,
                        discovered_from_url=discovered_from_url,
                        parent_artifact_id=parent_artifact_id,
                        discovery_adapter_id=discovery_adapter_id,
                        discovery_adapter_version=discovery_adapter_version,
                    )
                except ValueError as exc:
                    raise AgenticOrchestrationError("task.invalid") from exc
                ordinal = int(
                    self.storage.conn.execute(
                        "SELECT COUNT(*) AS count FROM agentic_tasks WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()["count"]
                )
                try:
                    changed = self.storage.conn.execute(
                        """INSERT INTO agentic_tasks (
                           task_id, run_id, task_key, task_ordinal, kind, required, status,
                           requested_url, query, depth, discovery_kind, discovered_from_url,
                           parent_artifact_id, adapter_id, adapter_version,
                           discovery_adapter_id, discovery_adapter_version, attempt_count,
                           replay_of_task_id, created_at)
                           SELECT ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?
                           WHERE EXISTS (
                               SELECT 1 FROM agentic_runs AS runs
                               WHERE runs.run_id = ? AND runs.status = 'running'
                                 AND runs.lease_owner IS ? AND runs.lease_epoch = ?
                           )""",
                        (
                            task_id,
                            run_id,
                            task_key,
                            ordinal,
                            kind,
                            int(required),
                            requested_url,
                            query,
                            depth,
                            discovery_kind,
                            discovered_from_url,
                            parent_artifact_id,
                            adapter_id,
                            adapter_version,
                            discovery_adapter_id,
                            discovery_adapter_version,
                            replay_of_task_id,
                            _format_time(self.clock()),
                            run_id,
                            fence[0] if fence is not None else None,
                            fence[1] if fence is not None else 0,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise AgenticOrchestrationError("run.lease_lost")
                except sqlite3.IntegrityError as exc:
                    raise AgenticOrchestrationError("task.required_set_sealed") from exc
                self._commit_if_standalone()
                row = self.storage.conn.execute(
                    "SELECT * FROM agentic_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
        assert row is not None
        task = self._task_from_row(row)
        expected = (
            kind,
            required,
            requested_url,
            query,
            depth,
            discovery_kind,
            discovered_from_url,
            parent_artifact_id,
            adapter_id,
            adapter_version,
            discovery_adapter_id,
            discovery_adapter_version,
            replay_of_task_id,
        )
        actual = (
            task.kind,
            task.required,
            task.requested_url,
            task.query,
            task.depth,
            task.discovery_kind,
            task.discovered_from_url,
            task.parent_artifact_id,
            task.adapter_id,
            task.adapter_version,
            task.discovery_adapter_id,
            task.discovery_adapter_version,
            task.replay_of_task_id,
        )
        if actual != expected:
            raise AgenticOrchestrationError("task.replay_conflict")
        return task

    def transition_task(
        self,
        task_id: str,
        *,
        status: str,
        attempt_count: int | None = None,
        artifact_id: str | None = None,
        access_decision_id: str | None = None,
        failure_code: str | None = None,
    ) -> AgenticChildTask:
        if status not in _TERMINAL_TASK_STATUSES | {"running"}:
            raise AgenticOrchestrationError("task.status_invalid")
        if failure_code is not None and not _REASON_CODE_RE.fullmatch(failure_code):
            raise AgenticOrchestrationError("task.failure_code_invalid")
        if access_decision_id is not None and not _ACCESS_DECISION_RE.fullmatch(
            access_decision_id
        ):
            raise AgenticOrchestrationError("task.access_decision_invalid")
        if artifact_id is not None and not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise AgenticOrchestrationError("task.artifact_invalid")
        with self._turn():
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("task.not_found")
            run_id = str(row["run_id"])
            fence = self._assert_run_fence(run_id)
            current = str(row["status"])
            if current in _TERMINAL_TASK_STATUSES:
                exact = (
                    current == status
                    and (
                        attempt_count is None
                        or int(row["attempt_count"]) == attempt_count
                    )
                    and (artifact_id is None or row["artifact_id"] == artifact_id)
                    and (
                        access_decision_id is None
                        or row["access_decision_id"] == access_decision_id
                    )
                    and (failure_code is None or row["failure_code"] == failure_code)
                )
                if not exact:
                    raise AgenticOrchestrationError("task.transition_invalid")
                return self._task_from_row(row)
            if current == "queued" and status not in {
                "running",
                "rejected",
                "failed",
                "cancelled",
            }:
                raise AgenticOrchestrationError("task.transition_invalid")
            if current == "running" and status == "queued":
                raise AgenticOrchestrationError("task.transition_invalid")
            finished_at = (
                _format_time(self.clock())
                if status in _TERMINAL_TASK_STATUSES
                else None
            )
            changed = self.storage.conn.execute(
                """UPDATE agentic_tasks
                   SET status = ?, attempt_count = COALESCE(?, attempt_count),
                       artifact_id = COALESCE(?, artifact_id),
                       access_decision_id = COALESCE(?, access_decision_id),
                       failure_code = ?, finished_at = ?
                   WHERE task_id = ? AND EXISTS (
                       SELECT 1 FROM agentic_runs AS runs
                       WHERE runs.run_id = agentic_tasks.run_id
                         AND runs.lease_owner IS ? AND runs.lease_epoch = ?
                   )""",
                (
                    status,
                    attempt_count,
                    artifact_id,
                    access_decision_id,
                    failure_code,
                    finished_at,
                    task_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")
            self._commit_if_standalone()
            updated = self.storage.conn.execute(
                "SELECT * FROM agentic_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert updated is not None
        return self._task_from_row(updated)

    def add_observation(
        self,
        *,
        task: AgenticChildTask,
        attempt: int,
        status: str,
        final_url: str | None,
        access_decision_id: str | None,
        artifact_id: str | None,
        reason_code: str,
        redirect_chain: Sequence[Mapping[str, Any]],
        current_url: str | None = None,
        status_code: int | None = None,
    ) -> AgenticReadObservation:
        if (
            task.requested_url is None
            or type(attempt) is not int
            or not 1 <= attempt <= 1_000_000
            or status not in _TERMINAL_TASK_STATUSES
            or not _REASON_CODE_RE.fullmatch(reason_code)
            or len(redirect_chain) > 100
        ):
            raise AgenticOrchestrationError("observation.invalid")
        try:
            if final_url is not None:
                canonicalize_access_url(final_url)
        except ValueError as exc:
            raise AgenticOrchestrationError("observation.invalid") from exc
        if access_decision_id is not None and not _ACCESS_DECISION_RE.fullmatch(
            access_decision_id
        ):
            raise AgenticOrchestrationError("observation.invalid")
        if artifact_id is not None and not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise AgenticOrchestrationError("observation.invalid")
        with self._turn():
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            if row is None:
                raise AgenticOrchestrationError("observation.task_conflict")
            durable_task = self._task_from_row(row)
            if type(task) is not AgenticChildTask or task != durable_task:
                raise AgenticOrchestrationError("observation.task_conflict")
            task = durable_task
            parent = self.require_run(task.run_id)
            if parent.status != "running":
                raise sqlite3.IntegrityError(
                    "agentic observation requires running state"
                )
            current_url = current_url or final_url or task.requested_url
            discovery = {
                "kind": task.discovery_kind,
                "source_url": task.discovered_from_url,
                "parent_artifact_id": task.parent_artifact_id,
                "adapter_id": task.discovery_adapter_id,
                "adapter_version": task.discovery_adapter_version,
            }
            try:
                _validate_observation_provenance(
                    task=task,
                    observation_status=status,
                    reason_code=reason_code,
                    current_url=current_url,
                    final_url=final_url,
                    status_code=status_code,
                    access_decision_id=access_decision_id,
                    redirect_chain=redirect_chain,
                    discovery=discovery,
                )
            except (TypeError, ValueError) as exc:
                raise AgenticOrchestrationError(
                    "observation.provenance_invalid"
                ) from exc
            redirect_json = canonical_json(list(redirect_chain))
            discovery_json = canonical_json(discovery)
            identity = {
                "attempt": attempt,
                "run_id": task.run_id,
                "task_id": task.task_id,
            }
            observation_id = _stable_id("read-observation", identity)
            values = (
                observation_id,
                task.run_id,
                parent.parent_task_id,
                task.task_id,
                attempt,
                status,
                task.requested_url,
                current_url,
                final_url,
                status_code,
                access_decision_id,
                artifact_id,
                reason_code,
                redirect_json,
                discovery_json,
                task.adapter_id,
                task.adapter_version,
                _format_time(self.clock()),
            )
            fence = self._assert_run_fence(task.run_id)
            changed = self.storage.conn.execute(
                """INSERT OR IGNORE INTO agentic_observations (
                       observation_id, run_id, parent_task_id, task_id, attempt,
                       status, requested_url, current_url, final_url, status_code,
                       access_decision_id,
                       artifact_id, reason_code, redirect_chain_json, discovery_json,
                       adapter_id, adapter_version, observed_at)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE EXISTS (
                       SELECT 1 FROM agentic_runs AS runs
                       WHERE runs.run_id = ?
                         AND runs.lease_owner IS ? AND runs.lease_epoch = ?
                   )""",
                values
                + (
                    task.run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed == 0:
                self._assert_run_fence(task.run_id)
            self._commit_if_standalone()
            row = self.storage.conn.execute(
                "SELECT * FROM agentic_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        assert row is not None
        observation = self._observation_from_row(row)
        expected = (
            task.run_id,
            parent.parent_task_id,
            task.task_id,
            attempt,
            status,
            task.requested_url,
            current_url,
            final_url,
            status_code,
            access_decision_id,
            artifact_id,
            reason_code,
            tuple(json.loads(redirect_json)),
            json.loads(discovery_json),
            task.adapter_id,
            task.adapter_version,
        )
        actual = (
            observation.run_id,
            observation.parent_task_id,
            observation.task_id,
            observation.attempt,
            observation.status,
            observation.requested_url,
            observation.current_url,
            observation.final_url,
            observation.status_code,
            observation.access_decision_id,
            observation.artifact_id,
            observation.reason_code,
            tuple(dict(item) for item in observation.redirect_chain),
            dict(observation.discovery),
            observation.adapter_id,
            observation.adapter_version,
        )
        if actual != expected:
            raise AgenticOrchestrationError("observation.replay_conflict")
        return observation

    def _validate_terminal_run_evidence(self, run_id: str) -> None:
        tasks = self.list_tasks(run_id)
        observations = self.list_observations(run_id)
        by_task: dict[str, list[AgenticReadObservation]] = {}
        for observation in observations:
            by_task.setdefault(observation.task_id, []).append(observation)
        for task in tasks:
            task_observations = by_task.get(task.task_id, [])
            if task.kind == "search":
                if (
                    task_observations
                    or task.attempt_count != 0
                    or task.artifact_id is not None
                    or task.access_decision_id is not None
                    or (task.status == "completed" and task.failure_code is not None)
                    or (
                        task.status in {"partial", "failed", "rejected", "cancelled"}
                        and task.failure_code is None
                    )
                ):
                    raise AgenticOrchestrationError("ledger.invalid")
                continue
            if (
                len(task_observations) != task.attempt_count
                or task.status == "partial"
                or (task.status == "completed" and task.failure_code is not None)
                or (
                    task.status in {"failed", "rejected", "cancelled"}
                    and task.failure_code is None
                )
            ):
                raise AgenticOrchestrationError("ledger.invalid")
            for observation in task_observations:
                if observation.status not in {"completed", "failed", "rejected"} or (
                    observation.status == "completed"
                ) != (observation.artifact_id is not None):
                    raise AgenticOrchestrationError("ledger.invalid")
            if task.status == "completed":
                if (
                    not task_observations
                    or task.artifact_id is None
                    or task.access_decision_id is None
                    or task_observations[-1].status != "completed"
                    or task_observations[-1].artifact_id != task.artifact_id
                    or task_observations[-1].access_decision_id
                    != task.access_decision_id
                ):
                    raise AgenticOrchestrationError("ledger.invalid")
                artifact = self.storage.conn.execute(
                    """SELECT source_run_id, normalized_source_identity,
                              requested_url, final_url,
                              access_decision_id, adapter_id, adapter_version
                       FROM artifact_observations WHERE artifact_id = ?""",
                    (task.artifact_id,),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["source_run_id"] != run_id
                    or artifact["normalized_source_identity"] != task.requested_url
                    or artifact["requested_url"] != task.requested_url
                    or artifact["final_url"] != task_observations[-1].final_url
                    or artifact["access_decision_id"] != task.access_decision_id
                    or artifact["adapter_id"] != task.adapter_id
                    or artifact["adapter_version"] != task.adapter_version
                ):
                    raise AgenticOrchestrationError("ledger.invalid")
            elif task.artifact_id is not None or any(
                observation.status == "completed" for observation in task_observations
            ):
                raise AgenticOrchestrationError("ledger.invalid")
            elif task.attempt_count > 0 and task.status in {"failed", "rejected"}:
                if task_observations[-1].status != task.status:
                    raise AgenticOrchestrationError("ledger.invalid")

    def finalize_run(
        self,
        run_id: str,
        *,
        requested_status: str,
        warnings: Sequence[str] = (),
    ) -> AgenticParentTask:
        if requested_status not in _TERMINAL_TASK_STATUSES:
            raise AgenticOrchestrationError("run.status_invalid")
        warning_values = tuple(sorted({str(item) for item in warnings if item}))
        if any(not _REASON_CODE_RE.fullmatch(item) for item in warning_values):
            raise AgenticOrchestrationError("run.warning_invalid")
        with self.transaction():
            fence = self._assert_run_fence(run_id)
            parent = self.require_run(run_id)
            if parent.status in _TERMINAL_TASK_STATUSES:
                if (
                    parent.status != requested_status
                    or parent.warnings != warning_values
                ):
                    raise AgenticOrchestrationError("run.transition_invalid")
                return parent
            sealed = self.storage.conn.execute(
                "SELECT required_sealed, active_reads FROM agentic_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if sealed is None or sealed["required_sealed"] != 1:
                raise AgenticOrchestrationError("task.required_set_unsealed")
            if sealed["active_reads"] != 0:
                raise AgenticOrchestrationError("budget.reservation_active")
            tasks = self.list_tasks(run_id)
            required = [item for item in tasks if item.required]
            if requested_status == "completed":
                if any(item.status not in _TERMINAL_TASK_STATUSES for item in required):
                    raise AgenticOrchestrationError("task.required_children_pending")
                if any(item.status != "completed" for item in required):
                    raise AgenticOrchestrationError(
                        "task.required_children_unsuccessful"
                    )
            if any(item.status not in _TERMINAL_TASK_STATUSES for item in tasks):
                raise AgenticOrchestrationError("task.children_pending")
            expected_status = _derive_parent_outcome(tasks, warnings=warning_values)
            if requested_status != expected_status:
                raise AgenticOrchestrationError("run.outcome_conflict")
            self._validate_terminal_run_evidence(run_id)
            changed = self.storage.conn.execute(
                """UPDATE agentic_runs SET status = ?, warnings_json = ?, finished_at = ?
                       , lease_owner = NULL, lease_expires_at = NULL
                   WHERE run_id = ? AND status = 'running' AND required_sealed = 1
                     AND active_reads = 0 AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    requested_status,
                    canonical_json(list(warning_values)),
                    _format_time(self.clock()),
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.finalize_conflict")
        return self.require_run(run_id)

    def list_tasks(self, run_id: str) -> tuple[AgenticChildTask, ...]:
        with self._turn():
            rows = self.storage.conn.execute(
                "SELECT * FROM agentic_tasks WHERE run_id = ? ORDER BY task_ordinal",
                (run_id,),
            ).fetchall()
        tasks = tuple(self._task_from_row(row) for row in rows)
        if tuple(item.task_ordinal for item in tasks) != tuple(range(len(tasks))):
            raise AgenticOrchestrationError("ledger.invalid")
        return tasks

    def list_observations(self, run_id: str) -> tuple[AgenticReadObservation, ...]:
        with self._turn():
            rows = self.storage.conn.execute(
                """SELECT observations.* FROM agentic_observations AS observations
                   JOIN agentic_tasks AS tasks ON tasks.task_id = observations.task_id
                   WHERE observations.run_id = ?
                   ORDER BY tasks.task_ordinal, observations.attempt""",
                (run_id,),
            ).fetchall()
        observations = tuple(self._observation_from_row(row) for row in rows)
        attempts: dict[str, list[int]] = {}
        for observation in observations:
            attempts.setdefault(observation.task_id, []).append(observation.attempt)
        if any(
            values != list(range(1, len(values) + 1)) for values in attempts.values()
        ):
            raise AgenticOrchestrationError("ledger.invalid")
        return observations

    def _parent_from_row(self, row) -> AgenticParentTask:
        try:
            run_id = _ledger_text(row["run_id"])
            parent_task_id = _ledger_text(row["parent_task_id"])
            status = _ledger_text(row["status"])
            warnings = json.loads(_ledger_text(row["warnings_json"]))
            requests_used = _ledger_nonnegative_int(row["requests_used"])
            bytes_used = _ledger_nonnegative_int(row["bytes_used"])
            pages_used = _ledger_nonnegative_int(row["pages_used"])
            files_used = _ledger_nonnegative_int(row["files_used"])
            active_reads = _ledger_nonnegative_int(row["active_reads"])
            required_sealed = _ledger_boolean(row["required_sealed"])
            created_at = _ledger_time(row["created_at"])
            finished_at = _ledger_optional_time(row["finished_at"])
            lease_owner = row["lease_owner"]
            lease_expires_at = row["lease_expires_at"]
            lease_epoch = _ledger_nonnegative_int(row["lease_epoch"])
            if (
                row["schema_version"] != AGENTIC_LEDGER_VERSION
                or not _SOURCE_RUN_RE.fullmatch(run_id)
                or parent_task_id
                != _stable_id(
                    "parent-task",
                    {
                        "run_id": run_id,
                        "schema_version": AGENTIC_ORCHESTRATION_VERSION,
                    },
                )
                or status not in _RUN_STATUSES
                or not isinstance(warnings, list)
                or warnings != sorted(set(warnings))
                or any(
                    not isinstance(item, str) or not _REASON_CODE_RE.fullmatch(item)
                    for item in warnings
                )
                or canonical_json(warnings) != row["warnings_json"]
                or required_sealed not in {False, True}
                or (lease_owner is None) != (lease_expires_at is None)
                or lease_epoch < 0
                or (
                    lease_owner is not None
                    and (
                        not isinstance(lease_owner, str)
                        or not _IDENTITY_RE.fullmatch(lease_owner)
                        or _ledger_time(lease_expires_at) != lease_expires_at
                    )
                )
                or (status == "running") != (finished_at is None)
                or (status in _TERMINAL_TASK_STATUSES and active_reads != 0)
                or (status in _TERMINAL_TASK_STATUSES and lease_owner is not None)
                or (status in _TERMINAL_TASK_STATUSES and not required_sealed)
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["rule_id"]))
                or not _SEMVER_RE.fullmatch(_ledger_text(row["rules_version"]))
                or not _SHA256_RE.fullmatch(_ledger_text(row["rules_sha256"]))
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["site_skill_id"]))
                or not _SEMVER_RE.fullmatch(_ledger_text(row["site_skill_version"]))
                or not _SHA256_RE.fullmatch(
                    _ledger_text(row["site_skill_package_sha256"])
                )
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["execution_plan_id"]))
                or row["execution_plan_version"] != "acquisition-execution-plan.v1"
                or not _SHA256_RE.fullmatch(_ledger_text(row["execution_plan_sha256"]))
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["read_adapter_id"]))
                or not _SEMVER_RE.fullmatch(_ledger_text(row["read_adapter_version"]))
            ):
                raise ValueError("invalid run ledger row")
            replay_of_run_id = row["replay_of_run_id"]
            if replay_of_run_id is not None:
                source = self.storage.conn.execute(
                    "SELECT * FROM agentic_runs WHERE run_id = ?",
                    (replay_of_run_id,),
                ).fetchone()
                replay_fields = (
                    "rule_id",
                    "rules_version",
                    "rules_sha256",
                    "site_skill_id",
                    "site_skill_version",
                    "site_skill_package_sha256",
                    "execution_plan_id",
                    "execution_plan_version",
                    "execution_plan_sha256",
                    "read_adapter_id",
                    "read_adapter_version",
                )
                if (
                    not isinstance(replay_of_run_id, str)
                    or not _SOURCE_RUN_RE.fullmatch(replay_of_run_id)
                    or replay_of_run_id == run_id
                    or source is None
                    or source["status"] not in _TERMINAL_TASK_STATUSES
                    or any(source[field] != row[field] for field in replay_fields)
                ):
                    raise ValueError("invalid replay run reference")
            if status in _TERMINAL_TASK_STATUSES:
                task_counts = self.storage.conn.execute(
                    """SELECT COUNT(*) AS task_count,
                              SUM(CASE WHEN required = 1 THEN 1 ELSE 0 END) AS required_count,
                              SUM(CASE WHEN status NOT IN ('completed','partial','rejected','failed','cancelled') THEN 1 ELSE 0 END) AS pending_count,
                              SUM(CASE WHEN required = 1 AND status != 'completed' THEN 1 ELSE 0 END) AS unsuccessful_required
                       FROM agentic_tasks WHERE run_id = ?""",
                    (run_id,),
                ).fetchone()
                if (
                    task_counts is None
                    or task_counts["task_count"] < 1
                    or task_counts["required_count"] < 1
                    or task_counts["pending_count"] != 0
                    or (
                        status == "completed"
                        and task_counts["unsuccessful_required"] != 0
                    )
                ):
                    raise ValueError("terminal run violates child barrier")
                self._validate_terminal_run_evidence(run_id)
                if (
                    _derive_parent_outcome(
                        self.list_tasks(run_id), warnings=tuple(warnings)
                    )
                    != status
                ):
                    raise ValueError("terminal run outcome is inconsistent")
            return AgenticParentTask(
                run_id=run_id,
                parent_task_id=parent_task_id,
                status=status,
                rule_id=row["rule_id"],
                rules_version=row["rules_version"],
                rules_sha256=row["rules_sha256"],
                site_skill_id=row["site_skill_id"],
                site_skill_version=row["site_skill_version"],
                site_skill_package_sha256=row["site_skill_package_sha256"],
                execution_plan_id=row["execution_plan_id"],
                execution_plan_version=row["execution_plan_version"],
                execution_plan_sha256=row["execution_plan_sha256"],
                read_adapter_id=row["read_adapter_id"],
                read_adapter_version=row["read_adapter_version"],
                replay_of_run_id=replay_of_run_id,
                requests_used=requests_used,
                bytes_used=bytes_used,
                pages_used=pages_used,
                files_used=files_used,
                warnings=tuple(warnings),
                created_at=created_at,
                finished_at=finished_at,
            )
        except AgenticOrchestrationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AgenticOrchestrationError("ledger.invalid") from exc

    def _task_from_row(self, row) -> AgenticChildTask:
        try:
            task_id = _ledger_text(row["task_id"])
            run_id = _ledger_text(row["run_id"])
            task_key = _ledger_text(row["task_key"])
            ordinal = _ledger_nonnegative_int(row["task_ordinal"])
            required = _ledger_boolean(row["required"])
            status = _ledger_text(row["status"])
            depth = _ledger_nonnegative_int(row["depth"])
            attempt_count = _ledger_nonnegative_int(row["attempt_count"])
            created_at = _ledger_time(row["created_at"])
            finished_at = _ledger_optional_time(row["finished_at"])
            requested_url = row["requested_url"]
            query = row["query"]
            if (
                row["schema_version"] != AGENTIC_LEDGER_VERSION
                or not _SOURCE_RUN_RE.fullmatch(run_id)
                or task_id
                != _stable_id("child-task", {"run_id": run_id, "task_key": task_key})
                or not task_key
                or len(task_key) > 4096
                or status not in _TERMINAL_TASK_STATUSES | {"queued", "running"}
                or row["kind"] not in {"read", "search"}
                or row["discovery_kind"] not in {"seed", "search", "link", "crawler"}
                or (status in _TERMINAL_TASK_STATUSES) != (finished_at is not None)
                or (row["kind"] == "read") != (requested_url is not None)
                or (row["kind"] == "search") != (query is not None)
                or (requested_url is not None and query is not None)
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["adapter_id"]))
                or not _SEMVER_RE.fullmatch(_ledger_text(row["adapter_version"]))
            ):
                raise ValueError("invalid task ledger row")
            if requested_url is not None and (
                not isinstance(requested_url, str)
                or canonicalize_access_url(requested_url) != requested_url
            ):
                raise ValueError("invalid requested URL")
            if query is not None:
                AgenticQuery(text=query)
            discovered_from_url = row["discovered_from_url"]
            if discovered_from_url is not None and (
                not isinstance(discovered_from_url, str)
                or canonicalize_access_url(discovered_from_url) != discovered_from_url
            ):
                raise ValueError("invalid discovery URL")
            for value, pattern in (
                (row["parent_artifact_id"], _ARTIFACT_ID_RE),
                (row["artifact_id"], _ARTIFACT_ID_RE),
                (row["access_decision_id"], _ACCESS_DECISION_RE),
                (row["failure_code"], _REASON_CODE_RE),
            ):
                if value is not None and (
                    not isinstance(value, str) or not pattern.fullmatch(value)
                ):
                    raise ValueError("invalid task evidence")
            discovery_adapter_id = row["discovery_adapter_id"]
            discovery_adapter_version = row["discovery_adapter_version"]
            if (discovery_adapter_id is None) != (discovery_adapter_version is None):
                raise ValueError("partial discovery adapter")
            if discovery_adapter_id is not None and (
                not _IDENTITY_RE.fullmatch(discovery_adapter_id)
                or not _SEMVER_RE.fullmatch(discovery_adapter_version)
            ):
                raise ValueError("invalid discovery adapter")
            _validate_task_discovery(
                kind=row["kind"],
                discovery_kind=row["discovery_kind"],
                discovered_from_url=discovered_from_url,
                parent_artifact_id=row["parent_artifact_id"],
                discovery_adapter_id=discovery_adapter_id,
                discovery_adapter_version=discovery_adapter_version,
            )
            if (
                self.storage.conn.execute(
                    "SELECT 1 FROM agentic_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("dangling run reference")
            replay_of_task_id = row["replay_of_task_id"]
            if (
                replay_of_task_id is not None
                and self.storage.conn.execute(
                    """SELECT 1 FROM agentic_tasks AS source_tasks
                       JOIN agentic_runs AS current_run ON current_run.run_id = ?
                       WHERE source_tasks.task_id = ?
                         AND source_tasks.run_id = current_run.replay_of_run_id
                         AND source_tasks.task_key = ? AND source_tasks.kind = ?""",
                    (run_id, replay_of_task_id, task_key, row["kind"]),
                ).fetchone()
                is None
            ):
                raise ValueError("dangling replay task")
            return AgenticChildTask(
                task_id=task_id,
                run_id=run_id,
                task_key=task_key,
                task_ordinal=ordinal,
                kind=row["kind"],
                required=required,
                status=status,
                requested_url=requested_url,
                query=query,
                depth=depth,
                discovery_kind=row["discovery_kind"],
                discovered_from_url=discovered_from_url,
                parent_artifact_id=row["parent_artifact_id"],
                adapter_id=row["adapter_id"],
                adapter_version=row["adapter_version"],
                discovery_adapter_id=discovery_adapter_id,
                discovery_adapter_version=discovery_adapter_version,
                attempt_count=attempt_count,
                artifact_id=row["artifact_id"],
                access_decision_id=row["access_decision_id"],
                failure_code=row["failure_code"],
                replay_of_task_id=replay_of_task_id,
                created_at=created_at,
                finished_at=finished_at,
            )
        except AgenticOrchestrationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AgenticOrchestrationError("ledger.invalid") from exc

    def _observation_from_row(self, row) -> AgenticReadObservation:
        try:
            observation_id = _ledger_text(row["observation_id"])
            run_id = _ledger_text(row["run_id"])
            task_id = _ledger_text(row["task_id"])
            attempt = _ledger_positive_int(row["attempt"])
            redirects = json.loads(_ledger_text(row["redirect_chain_json"]))
            discovery = json.loads(_ledger_text(row["discovery_json"]))
            requested_url = _ledger_text(row["requested_url"])
            current_url = row["current_url"]
            final_url = row["final_url"]
            status_code = row["status_code"]
            if (
                row["schema_version"] != AGENTIC_LEDGER_VERSION
                or observation_id
                != _stable_id(
                    "read-observation",
                    {"attempt": attempt, "run_id": run_id, "task_id": task_id},
                )
                or row["status"] not in _TERMINAL_TASK_STATUSES
                or not isinstance(redirects, list)
                or len(redirects) > 100
                or any(not isinstance(item, dict) for item in redirects)
                or canonical_json(redirects) != row["redirect_chain_json"]
                or not isinstance(discovery, dict)
                or set(discovery)
                != {
                    "kind",
                    "source_url",
                    "parent_artifact_id",
                    "adapter_id",
                    "adapter_version",
                }
                or canonical_json(discovery) != row["discovery_json"]
                or canonicalize_access_url(requested_url) != requested_url
                or not _REASON_CODE_RE.fullmatch(_ledger_text(row["reason_code"]))
                or not _IDENTITY_RE.fullmatch(_ledger_text(row["adapter_id"]))
                or not _SEMVER_RE.fullmatch(_ledger_text(row["adapter_version"]))
            ):
                raise ValueError("invalid observation ledger row")
            if final_url is not None and (
                not isinstance(final_url, str)
                or canonicalize_access_url(final_url) != final_url
            ):
                raise ValueError("invalid final URL")
            for value, pattern in (
                (row["access_decision_id"], _ACCESS_DECISION_RE),
                (row["artifact_id"], _ARTIFACT_ID_RE),
            ):
                if value is not None and (
                    not isinstance(value, str) or not pattern.fullmatch(value)
                ):
                    raise ValueError("invalid observation evidence")
            task_row = self.storage.conn.execute(
                """SELECT requested_url, run_id, adapter_id, adapter_version
                   FROM agentic_tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            run_row = self.storage.conn.execute(
                "SELECT parent_task_id FROM agentic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                task_row is None
                or run_row is None
                or task_row["run_id"] != run_id
                or task_row["requested_url"] != requested_url
                or task_row["adapter_id"] != row["adapter_id"]
                or task_row["adapter_version"] != row["adapter_version"]
                or run_row["parent_task_id"] != row["parent_task_id"]
            ):
                raise ValueError("dangling observation reference")
            task = self._task_from_row(
                self.storage.conn.execute(
                    "SELECT * FROM agentic_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
            _validate_observation_provenance(
                task=task,
                observation_status=row["status"],
                reason_code=row["reason_code"],
                current_url=current_url,
                final_url=final_url,
                status_code=status_code,
                access_decision_id=row["access_decision_id"],
                redirect_chain=redirects,
                discovery=discovery,
            )
            observed_at = _ledger_time(row["observed_at"])
            return AgenticReadObservation(
                observation_id=observation_id,
                run_id=run_id,
                parent_task_id=row["parent_task_id"],
                task_id=task_id,
                attempt=attempt,
                status=row["status"],
                requested_url=requested_url,
                current_url=current_url,
                final_url=final_url,
                status_code=status_code,
                access_decision_id=row["access_decision_id"],
                artifact_id=row["artifact_id"],
                reason_code=row["reason_code"],
                redirect_chain=tuple(MappingProxyType(item) for item in redirects),
                discovery=MappingProxyType(discovery),
                adapter_id=row["adapter_id"],
                adapter_version=row["adapter_version"],
                observed_at=observed_at,
            )
        except AgenticOrchestrationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AgenticOrchestrationError("ledger.invalid") from exc


_FROZEN_REPOSITORY_RENEW_TARGET_SEND = AgenticTaskRepository.renew_target_send
_FROZEN_REPOSITORY_RELEASE_TARGET_SEND = AgenticTaskRepository.release_target_send
_FROZEN_TARGET_SEND_LEASE_VALIDATE = _TargetSendLease.validate
_FROZEN_TARGET_SEND_LEASE_RENEW = _TargetSendLease.renew
_FROZEN_TARGET_SEND_LEASE_RELEASE = _TargetSendLease.__call__


@dataclass(frozen=True, slots=True)
class _AgenticExecutionSnapshot:
    orchestrator: AgenticOrchestrator
    prepared: PreparedAgenticAuthority
    repository: AgenticTaskRepository
    storage: Any
    authority: AgenticAuthority
    artifact_store: ArtifactStore
    read_gateway: GovernedReadGateway | MockClientReadGateway
    crawler_snapshot: _AdapterSnapshot
    search_snapshot: _AdapterSnapshot | None
    prepared_identity: int
    repository_identity: int
    authority_identity: int
    crawler_snapshot_identity: int
    search_snapshot_identity: int | None

    def validate(self) -> None:
        prepared = object.__getattribute__(
            self.orchestrator, "_AgenticOrchestrator__prepared_authority"
        )
        repository = object.__getattribute__(
            self.orchestrator, "_AgenticOrchestrator__repository"
        )
        crawler_snapshot = object.__getattribute__(
            self.orchestrator, "_crawler_snapshot"
        )
        search_snapshot = object.__getattribute__(self.orchestrator, "_search_snapshot")
        if (
            prepared is not self.prepared
            or repository is not self.repository
            or id(prepared) != self.prepared_identity
            or id(repository) != self.repository_identity
            or id(self.authority) != self.authority_identity
            or self.prepared.authority is not self.authority
            or self.prepared.artifact_store is not self.artifact_store
            or self.prepared.read_gateway is not self.read_gateway
            or self.repository.storage is not self.storage
            or self.artifact_store.storage is not self.storage
            or crawler_snapshot is not self.crawler_snapshot
            or search_snapshot is not self.search_snapshot
            or id(crawler_snapshot) != self.crawler_snapshot_identity
            or (id(search_snapshot) if search_snapshot is not None else None)
            != self.search_snapshot_identity
        ):
            raise AgenticOrchestrationError("authority.execution_seal_invalid")
        _FROZEN_PREPARED_VALIDATE(self.prepared)


class AgenticOrchestrator:
    """Execute one deterministic, bounded parent/child exploration run."""

    def __init__(
        self,
        *,
        storage,
        prepared_authority: PreparedAgenticAuthority,
        crawler_adapter: CrawlerDiscoveryAdapter,
        search_adapter: AuthorizedSearchAdapter | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(prepared_authority) is not PreparedAgenticAuthority:
            raise AgenticOrchestrationError("authority.seal_invalid")
        _FROZEN_PREPARED_VALIDATE(prepared_authority)
        if prepared_authority.artifact_store.storage is not storage:
            raise AgenticOrchestrationError("storage.binding_invalid")
        object.__setattr__(
            self,
            "_AgenticOrchestrator__prepared_authority",
            prepared_authority,
        )
        object.__setattr__(
            self,
            "_AgenticOrchestrator__prepared_identity",
            id(prepared_authority),
        )
        object.__setattr__(
            self,
            "_AgenticOrchestrator__authority_identity",
            id(prepared_authority.authority),
        )
        self.crawler_adapter = crawler_adapter
        self.search_adapter = search_adapter
        self._crawler_snapshot = self._snapshot_adapter(crawler_adapter, search=False)
        self._search_snapshot = (
            self._snapshot_adapter(search_adapter, search=True)
            if search_adapter is not None
            else None
        )
        self.cancel_requested = cancel_requested or (lambda: False)
        self.clock = clock or (lambda: datetime.now(UTC))
        repository = AgenticTaskRepository(storage, clock=self.clock)
        object.__setattr__(self, "_AgenticOrchestrator__repository", repository)
        object.__setattr__(
            self,
            "_AgenticOrchestrator__repository_identity",
            id(repository),
        )

    @property
    def prepared_authority(self) -> PreparedAgenticAuthority:
        return self.__prepared_authority

    @property
    def storage(self):
        return self.__prepared_authority.artifact_store.storage

    @property
    def artifact_store(self) -> ArtifactStore:
        return self.__prepared_authority.artifact_store

    @property
    def read_gateway(self) -> GovernedReadGateway | MockClientReadGateway:
        return self.__prepared_authority.read_gateway

    @property
    def resolved_site_skill(self) -> ResolvedSiteSkill:
        return self.__prepared_authority.resolved_site_skill

    @property
    def execution_plan(self) -> AcquisitionExecutionPlan:
        return self.__prepared_authority.execution_plan

    @property
    def authority(self) -> AgenticAuthority:
        return self.__prepared_authority.authority

    @property
    def repository(self) -> AgenticTaskRepository:
        return self.__repository

    def _prepared_snapshot(self) -> PreparedAgenticAuthority:
        prepared = self.__prepared_authority
        _FROZEN_PREPARED_VALIDATE(prepared)
        if self.__repository.storage is not prepared.artifact_store.storage:
            raise AgenticOrchestrationError("storage.binding_invalid")
        return prepared

    def _execution_snapshot(self) -> _AgenticExecutionSnapshot:
        prepared = object.__getattribute__(
            self, "_AgenticOrchestrator__prepared_authority"
        )
        repository = object.__getattribute__(self, "_AgenticOrchestrator__repository")
        crawler_snapshot = object.__getattribute__(self, "_crawler_snapshot")
        search_snapshot = object.__getattribute__(self, "_search_snapshot")
        if (
            id(prepared)
            != object.__getattribute__(self, "_AgenticOrchestrator__prepared_identity")
            or id(repository)
            != object.__getattribute__(
                self, "_AgenticOrchestrator__repository_identity"
            )
            or id(prepared.authority)
            != object.__getattribute__(self, "_AgenticOrchestrator__authority_identity")
        ):
            raise AgenticOrchestrationError("authority.execution_seal_invalid")
        snapshot = _AgenticExecutionSnapshot(
            orchestrator=self,
            prepared=prepared,
            repository=repository,
            storage=repository.storage,
            authority=prepared.authority,
            artifact_store=prepared.artifact_store,
            read_gateway=prepared.read_gateway,
            crawler_snapshot=crawler_snapshot,
            search_snapshot=search_snapshot,
            prepared_identity=id(prepared),
            repository_identity=id(repository),
            authority_identity=id(prepared.authority),
            crawler_snapshot_identity=id(crawler_snapshot),
            search_snapshot_identity=(
                id(search_snapshot) if search_snapshot is not None else None
            ),
        )
        snapshot.validate()
        return snapshot

    @staticmethod
    def _snapshot_adapter(adapter: object, *, search: bool) -> _AdapterSnapshot:
        adapter_id = getattr(adapter, "adapter_id", None)
        version = getattr(adapter, "adapter_version", None)
        if not isinstance(adapter_id, str) or not _IDENTITY_RE.fullmatch(adapter_id):
            raise AgenticOrchestrationError("adapter.identity_invalid")
        if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
            raise AgenticOrchestrationError("adapter.version_invalid")
        if search and getattr(adapter, "authorized", None) is not True:
            raise AgenticOrchestrationError("search.authorization_required")
        method_name = "search" if search else "discover"
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise AgenticOrchestrationError("adapter.callable_invalid")
        bound_self = getattr(method, "__self__", None)
        return _AdapterSnapshot(
            adapter=adapter,
            object_identity=id(adapter),
            adapter_id=adapter_id,
            adapter_version=version,
            method_name=method_name,
            callable_object=method,
            callable_function=getattr(method, "__func__", method),
            bound_self_identity=id(bound_self) if bound_self is not None else None,
            authorized=True if search else None,
        )

    def run(
        self,
        *,
        rules: AgenticSiteRules,
        run_id: str,
        replay_of_run_id: str | None = None,
    ) -> AgenticRunResult:
        execution = self._execution_snapshot()
        repository = execution.repository
        authority = execution.authority
        rules_snapshot = _FROZEN_RUN_CAPTURE(
            _AgenticRunSnapshot, rules, execution.prepared
        )
        self._validate_bound_rules(rules_snapshot)
        rules = rules_snapshot.rules
        execution.validate()
        existing = repository.get_run(run_id)
        if existing is not None:
            repository.create_run(
                run_id=run_id,
                rules=rules,
                authority=authority,
                replay_of_run_id=replay_of_run_id,
            )
            if existing.status in _TERMINAL_TASK_STATUSES:
                return self._result(run_id, execution=execution)
        else:
            with repository.transaction():
                execution.validate()
                repository.create_run(
                    run_id=run_id,
                    rules=rules,
                    authority=authority,
                    replay_of_run_id=replay_of_run_id,
                )
                for seed_url in rules.scope.seed_urls:
                    repository.create_task(
                        run_id=run_id,
                        task_key=f"read:{seed_url}",
                        kind="read",
                        required=True,
                        requested_url=seed_url,
                        depth=0,
                        discovery_kind="seed",
                        adapter_id=authority.read_adapter_id,
                        adapter_version=authority.read_adapter_version,
                        replay_of_run_id=replay_of_run_id,
                    )
                for index, query in enumerate(rules.scope.queries):
                    adapter_id = (
                        execution.search_snapshot.adapter_id
                        if execution.search_snapshot is not None
                        else "search_unavailable"
                    )
                    adapter_version = (
                        execution.search_snapshot.adapter_version
                        if execution.search_snapshot is not None
                        else "0.0.0"
                    )
                    repository.create_task(
                        run_id=run_id,
                        task_key=f"search:{index}:{query.text}",
                        kind="search",
                        required=query.required,
                        query=query.text,
                        discovery_kind="search",
                        adapter_id=adapter_id,
                        adapter_version=adapter_version,
                        replay_of_run_id=replay_of_run_id,
                    )
                repository.seal_required_tasks(run_id)

        lease_owner = f"run-lease-{secrets.token_hex(16)}"
        execution.validate()
        if not repository.acquire_run_lease(
            run_id, owner=lease_owner, ttl_seconds=3600
        ):
            raise AgenticOrchestrationError("run.lease_unavailable")
        try:
            if existing is not None:
                self._recover_interrupted_tasks(run_id, execution=execution)
            result = self._execute_run(
                run_id=run_id,
                rules_snapshot=rules_snapshot,
                replay_of_run_id=replay_of_run_id,
                lease_owner=lease_owner,
                execution=execution,
            )
        except BaseException as exc:
            if execution.storage.execution_transaction_owned_by_current_thread:
                try:
                    execution.storage.rollback_execution_transaction()
                except sqlite3.Error:
                    exc.add_note("agentic transaction rollback hit a SQLite error")
            try:
                repository.interrupt_run(
                    run_id,
                    cancelled=isinstance(
                        exc, (KeyboardInterrupt, asyncio.CancelledError)
                    ),
                )
            except (AgenticOrchestrationError, sqlite3.Error):
                exc.add_note("agentic interruption persistence hit a SQLite error")
            try:
                repository.release_run_lease(run_id, owner=lease_owner)
            except (AgenticOrchestrationError, sqlite3.Error):
                exc.add_note("agentic lease release hit a SQLite error")
            raise
        else:
            execution.validate()
            repository.release_run_lease(run_id, owner=lease_owner)
            return result

    def _execute_run(
        self,
        *,
        run_id: str,
        rules_snapshot: _AgenticRunSnapshot,
        replay_of_run_id: str | None,
        lease_owner: str,
        execution: _AgenticExecutionSnapshot,
    ) -> AgenticRunResult:
        execution.validate()
        repository = execution.repository
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        warnings: set[str] = set()
        cancelled = False
        queue: deque[str] = deque(
            item.task_id for item in repository.list_tasks(run_id)
        )
        while queue:
            execution.validate()
            if not repository.acquire_run_lease(
                run_id, owner=lease_owner, ttl_seconds=3600
            ):
                raise AgenticOrchestrationError("run.lease_lost")
            task_id = queue.popleft()
            task = next(
                item
                for item in repository.list_tasks(run_id)
                if item.task_id == task_id
            )
            if task.status in _TERMINAL_TASK_STATUSES:
                continue
            if self.cancel_requested():
                cancelled = True
                execution.validate()
                self._cancel_pending(run_id, current=task, execution=execution)
                break
            if task.kind == "search":
                new_tasks = self._run_search_task(
                    task,
                    rules_snapshot=rules_snapshot,
                    replay_of_run_id=replay_of_run_id,
                    execution=execution,
                )
                queue.extend(item.task_id for item in new_tasks)
                continue
            new_tasks, task_warnings, task_cancelled = self._run_read_task(
                task,
                rules_snapshot=rules_snapshot,
                replay_of_run_id=replay_of_run_id,
                execution=execution,
            )
            warnings.update(task_warnings)
            queue.extend(item.task_id for item in new_tasks)
            if task_cancelled:
                cancelled = True
                self._cancel_pending(run_id, execution=execution)
                break

        execution.validate()
        tasks = repository.list_tasks(run_id)
        for task in tasks:
            if task.failure_code == "budget.candidates_exhausted":
                warnings.add("budget.candidates_exhausted")
            if (
                task.status
                in {
                    "partial",
                    "failed",
                    "rejected",
                    "cancelled",
                }
                and not task.required
            ):
                warnings.add("optional_child_failed")
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        status = _derive_parent_outcome(
            tasks, warnings=tuple(warnings), cancelled=cancelled
        )
        execution.validate()
        repository.finalize_run(run_id, requested_status=status, warnings=warnings)
        return self._result(run_id, execution=execution)

    def _recover_interrupted_tasks(
        self, run_id: str, *, execution: _AgenticExecutionSnapshot
    ) -> None:
        execution.validate()
        repository = execution.repository
        storage = execution.storage
        with repository.transaction():
            fence = repository._assert_run_fence(run_id)
            finished_at = _format_time(self.clock())
            storage.conn.execute(
                """UPDATE agentic_tasks
                   SET status = 'failed', failure_code = 'run.interrupted',
                       finished_at = ?, attempt_count = (
                           SELECT COUNT(*) FROM agentic_observations AS observations
                           WHERE observations.task_id = agentic_tasks.task_id
                       )
                   WHERE run_id = ? AND status = 'running'
                     AND EXISTS (
                         SELECT 1 FROM agentic_runs AS runs
                         WHERE runs.run_id = agentic_tasks.run_id
                           AND runs.status = 'running'
                           AND runs.lease_owner IS ? AND runs.lease_epoch = ?
                     )""",
                (
                    finished_at,
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            )
            changed = storage.conn.execute(
                """UPDATE agentic_runs SET active_reads = 0
                   WHERE run_id = ? AND status = 'running'
                     AND lease_owner IS ? AND lease_epoch = ?""",
                (
                    run_id,
                    fence[0] if fence is not None else None,
                    fence[1] if fence is not None else 0,
                ),
            ).rowcount
            if changed != 1:
                raise AgenticOrchestrationError("run.lease_lost")

    def _validate_bound_rules(self, rules_snapshot: _AgenticRunSnapshot) -> None:
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        rules = rules_snapshot.rules
        prepared = rules_snapshot.prepared
        dispatch = prepared.predicate_dispatch
        manifest = prepared.resolved_site_skill.manifest
        plan = prepared.execution_plan
        if rules.site_key != manifest.site_key or plan.site_key != rules.site_key:
            raise AgenticOrchestrationError("rules.site_key_mismatch")
        allowed_domains = {
            item.casefold().rstrip(".") for item in manifest.allowed_domains
        }
        canonical_seed = dispatch.canonicalize_access_url(prepared.scope.seed_url)
        origin_hosts = tuple(
            (dispatch.urlsplit(origin).hostname or "").casefold().rstrip(".")
            for origin in rules.scope.allowed_origins
        )
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        if (
            canonical_seed not in set(rules.scope.seed_urls)
            or not set(rules.scope.allowed_origins) <= set(prepared.allowed_origins)
            or any(
                not _FROZEN_RUN_CONTAINS_URL(rules_snapshot, seed)
                for seed in rules.scope.seed_urls
            )
            or any(
                not _FROZEN_RUN_PATTERN_WITHIN_SCOPE(rules_snapshot, pattern)
                for pattern in rules.scope.allow_patterns
            )
        ):
            raise AgenticOrchestrationError("rules.scope_broader")
        for host in origin_hosts:
            if not any(
                host == domain or host.endswith(f".{domain}")
                for domain in allowed_domains
            ):
                raise AgenticOrchestrationError("rules.origin_broader")
        plan_budgets = plan.scope_budgets
        try:
            plan_depth = plan_budgets["max_depth"]
            plan_pages = plan_budgets["max_pages"]
            plan_files = plan_budgets["max_files"]
            plan_bytes = plan.limits["stdout_bytes"]
        except KeyError as exc:
            raise AgenticOrchestrationError("authority.budget_invalid") from exc
        if any(
            type(value) is not int or value < 1
            for value in (plan_depth, plan_pages, plan_files, plan_bytes)
        ):
            raise AgenticOrchestrationError("authority.budget_invalid")
        plan_requests = plan_pages + plan_files
        if (
            rules.budgets.max_depth > plan_depth
            or rules.budgets.max_requests > plan_requests
            or rules.budgets.max_files > plan_files
            or rules.budgets.max_bytes > plan_bytes
        ):
            raise AgenticOrchestrationError("rules.budget_broader")
        if not set(rules.content_types) <= set(
            prepared.artifact_store.allowed_mime_types
        ):
            raise AgenticOrchestrationError("rules.content_type_unsupported")
        if type(prepared.read_gateway) is GovernedReadGateway:
            config = prepared.read_gateway.gateway.config
            gateway_origins = {item.as_url_origin() for item in config.allowed_origins}
            if (
                not set(rules.scope.allowed_origins) <= gateway_origins
                or config.diagnostic_artifact_sha256 != plan.acquisition_fingerprint
                or config.budget_limit < rules.budgets.max_requests
                or prepared.read_gateway.max_body_bytes > plan_bytes
            ):
                raise AgenticOrchestrationError("gateway.authority_mismatch")
        _FROZEN_RUN_VALIDATE(rules_snapshot)

    def _run_search_task(
        self,
        task: AgenticChildTask,
        *,
        rules_snapshot: _AgenticRunSnapshot,
        replay_of_run_id: str | None,
        execution: _AgenticExecutionSnapshot,
    ) -> tuple[AgenticChildTask, ...]:
        execution.validate()
        repository = execution.repository
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        rules = rules_snapshot.rules
        task = repository.transition_task(task.task_id, status="running")
        snapshot = execution.search_snapshot
        if snapshot is None:
            repository.transition_task(
                task.task_id,
                status="failed",
                failure_code="search.authorization_unavailable",
            )
            return ()
        parent = repository.require_run(task.run_id)
        limit = max(rules.budgets.max_requests - parent.requests_used, 0)
        try:
            candidates, truncated = _bounded_candidates(
                _FROZEN_ADAPTER_INVOKE(snapshot, task.query or ""),
                limit=limit,
                expected_kinds=frozenset({"search"}),
            )
            _FROZEN_ADAPTER_VALIDATE(snapshot)
            execution.validate()
        except _CandidateValidationError:
            repository.transition_task(
                task.task_id,
                status="partial",
                failure_code="search.candidate_invalid",
            )
            return ()
        except Exception:  # noqa: BLE001 - this is the untrusted adapter boundary.
            repository.transition_task(
                task.task_id,
                status="failed",
                failure_code="search.adapter_error",
            )
            return ()
        created: list[AgenticChildTask] = []
        for candidate in candidates:
            created_task = self._schedule_candidate(
                run_id=task.run_id,
                candidate=candidate,
                depth=0,
                parent_artifact_id=None,
                discovery_adapter_id=snapshot.adapter_id,
                discovery_adapter_version=snapshot.adapter_version,
                rules_snapshot=rules_snapshot,
                replay_of_run_id=replay_of_run_id,
                execution=execution,
            )
            if created_task is not None:
                created.append(created_task)
        execution.validate()
        repository.transition_task(
            task.task_id,
            status="partial" if truncated else "completed",
            failure_code="budget.candidates_exhausted" if truncated else None,
        )
        return tuple(created)

    def _run_read_task(
        self,
        task: AgenticChildTask,
        *,
        rules_snapshot: _AgenticRunSnapshot,
        replay_of_run_id: str | None,
        execution: _AgenticExecutionSnapshot,
    ) -> tuple[tuple[AgenticChildTask, ...], tuple[str, ...], bool]:
        execution.validate()
        repository = execution.repository
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        rules = rules_snapshot.rules
        if task.depth > rules.budgets.max_depth:
            self._reject_without_read(
                task, "budget.depth_exhausted", execution=execution
            )
            return (), ("budget.depth_exhausted",), False
        task = repository.transition_task(task.task_id, status="running")
        for attempt in range(1, rules.budgets.max_retries + 2):
            _FROZEN_RUN_VALIDATE(rules_snapshot)
            prepared = rules_snapshot.prepared
            if self.cancel_requested():
                execution.validate()
                repository.transition_task(
                    task.task_id,
                    status="cancelled",
                    attempt_count=attempt - 1,
                    failure_code="run.cancelled",
                )
                return (), (), True
            _FROZEN_RUN_VALIDATE(rules_snapshot)
            execution.validate()
            try:
                remaining_bytes = repository.begin_read_budget(
                    task.run_id,
                    max_requests=rules.budgets.max_requests,
                    max_bytes=rules.budgets.max_bytes,
                    max_files=rules.budgets.max_files,
                    max_concurrency=rules.budgets.max_concurrency,
                )
            except AgenticOrchestrationError as exc:
                if exc.reason_code not in {
                    "budget.requests_exhausted",
                    "budget.bytes_exhausted",
                    "budget.files_exhausted",
                    "budget.concurrency_exhausted",
                }:
                    raise
                self._record_pre_access_failure(
                    task=task,
                    attempt=attempt,
                    reason_code=exc.reason_code,
                    execution=execution,
                )
                return (), (exc.reason_code,), False
            try:

                def consume_target_request(
                    url: str,
                    decision: object,
                    prepared_authority: PreparedAgenticAuthority = prepared,
                ):
                    _FROZEN_RUN_VALIDATE(rules_snapshot)
                    if not _FROZEN_RUN_MATCHES(
                        rules_snapshot, url
                    ) or not _FROZEN_RUN_CONTAINS_URL(rules_snapshot, url):
                        raise AccessGatewayOriginError(
                            "target URL is outside the sealed Agentic scope",
                            decision=decision,
                            current_url=url,
                            final_url=url,
                            redirect_hops=tuple(
                                getattr(decision, "redirect_hops", ()) or ()
                            ),
                        )
                    try:
                        execution.validate()
                        claim = repository.claim_target_send(
                            task.run_id,
                            max_requests=rules.budgets.max_requests,
                            timeout_seconds=float(
                                prepared_authority.execution_plan.limits[
                                    "timeout_seconds"
                                ]
                            ),
                        )
                    except AgenticOrchestrationError as exc:
                        if exc.reason_code != "budget.requests_exhausted":
                            raise
                        raise AccessGatewayBudgetError(
                            "agentic run request budget exhausted",
                            decision=decision,
                            current_url=url,
                            final_url=url,
                            redirect_hops=tuple(
                                getattr(decision, "redirect_hops", ()) or ()
                            ),
                        ) from exc
                    return _TargetSendLease(
                        repository,
                        claim,
                        timeout_seconds=float(
                            prepared_authority.execution_plan.limits["timeout_seconds"]
                        ),
                    )

                read = (
                    _FROZEN_GOVERNED_READ
                    if type(prepared.read_gateway) is GovernedReadGateway
                    else _FROZEN_MOCK_READ
                )
                result = read(
                    prepared.read_gateway,
                    task.requested_url or "",
                    max_body_bytes=remaining_bytes,
                    before_target_request=consume_target_request,
                )
                execution.validate()
            except AccessRejectedError as exc:
                decision = exc.decision
                status = "rejected" if decision.outcome == "reject" else "failed"
                retry = status == "failed" and bool(decision.retryable)
                terminal = not retry or attempt > rules.budgets.max_retries
                self._record_failed_attempt(
                    task=task,
                    attempt=attempt,
                    status=status,
                    current_url=decision.canonical_url,
                    final_url=decision.canonical_url,
                    status_code=None,
                    access_decision_id=decision.decision_id,
                    reason_code=decision.reason_code,
                    redirect_chain=_redirect_chain(decision),
                    bytes_read=0,
                    terminal=terminal,
                    rules=rules,
                    execution=execution,
                )
                if not terminal:
                    continue
                return (), (), False
            except BodyFailure as exc:
                consumed = min(max(int(exc.decoded), 0), remaining_bytes)
                code = f"body.{_safe_reason_component(exc.reason, 'failure')}"
                retry = exc.retryable and attempt <= rules.budgets.max_retries
                decision = getattr(exc, "decision", None)
                self._record_failed_attempt(
                    task=task,
                    attempt=attempt,
                    status="failed",
                    current_url=getattr(exc, "final_url", None),
                    final_url=getattr(exc, "final_url", None),
                    status_code=getattr(exc, "status_code", None),
                    access_decision_id=getattr(decision, "decision_id", None),
                    reason_code=code,
                    redirect_chain=_redirect_chain(exc),
                    bytes_read=consumed,
                    terminal=not retry,
                    rules=rules,
                    execution=execution,
                )
                if retry:
                    continue
                warning = (
                    "budget.bytes_exhausted" if "budget_exhausted" in exc.reason else ""
                )
                return (), ((warning,) if warning else ()), False
            except AccessGatewayError as exc:
                code, retryable, decision = _gateway_failure(exc)
                retry = retryable and attempt <= rules.budgets.max_retries
                redirect_chain = _redirect_chain(exc)
                access_decision_id = getattr(decision, "decision_id", None)
                if access_decision_id is None and redirect_chain:
                    access_decision_id = redirect_chain[-1]["access_decision_id"]
                self._record_failed_attempt(
                    task=task,
                    attempt=attempt,
                    status="failed",
                    current_url=getattr(exc, "current_url", None),
                    final_url=getattr(
                        exc,
                        "final_url",
                        getattr(decision, "canonical_url", None),
                    ),
                    status_code=getattr(exc, "status_code", None),
                    access_decision_id=access_decision_id,
                    reason_code=code,
                    redirect_chain=redirect_chain,
                    bytes_read=0,
                    terminal=not retry,
                    rules=rules,
                    execution=execution,
                )
                if retry:
                    continue
                return (), (), False
            except (OSError, RuntimeError, TypeError, ValueError):
                code = "gateway.unexpected_error"
                self._record_failed_attempt(
                    task=task,
                    attempt=attempt,
                    status="failed",
                    current_url=task.requested_url,
                    final_url=None,
                    status_code=None,
                    access_decision_id=None,
                    reason_code=code,
                    redirect_chain=(),
                    bytes_read=0,
                    terminal=True,
                    rules=rules,
                    execution=execution,
                )
                return (), (), False
            return self._accept_read(
                task,
                attempt=attempt,
                result=result,
                rules_snapshot=rules_snapshot,
                replay_of_run_id=replay_of_run_id,
                execution=execution,
            )
        raise AssertionError("bounded retry loop must return")

    def _record_pre_access_failure(
        self,
        *,
        task: AgenticChildTask,
        attempt: int,
        reason_code: str,
        execution: _AgenticExecutionSnapshot,
    ) -> None:
        execution.validate()
        repository = execution.repository
        with repository.transaction():
            repository.add_observation(
                task=task,
                attempt=attempt,
                status="rejected",
                current_url=task.requested_url,
                final_url=task.requested_url,
                status_code=None,
                access_decision_id=None,
                artifact_id=None,
                reason_code=reason_code,
                redirect_chain=(),
            )
            repository.transition_task(
                task.task_id,
                status="rejected",
                attempt_count=attempt,
                failure_code=reason_code,
            )

    def _record_failed_attempt(
        self,
        *,
        task: AgenticChildTask,
        attempt: int,
        status: str,
        current_url: str | None,
        final_url: str | None,
        status_code: int | None,
        access_decision_id: str | None,
        reason_code: str,
        redirect_chain: Sequence[Mapping[str, Any]],
        bytes_read: int,
        terminal: bool,
        rules: AgenticSiteRules,
        execution: _AgenticExecutionSnapshot,
    ) -> None:
        execution.validate()
        repository = execution.repository
        with repository.transaction():
            repository.finish_read_budget(
                task.run_id,
                bytes_read=bytes_read,
                files=0,
                max_bytes=rules.budgets.max_bytes,
                max_files=rules.budgets.max_files,
            )
            repository.add_observation(
                task=task,
                attempt=attempt,
                status=status,
                current_url=current_url,
                final_url=final_url,
                status_code=status_code,
                access_decision_id=access_decision_id,
                artifact_id=None,
                reason_code=reason_code,
                redirect_chain=redirect_chain,
            )
            if terminal:
                repository.transition_task(
                    task.task_id,
                    status=status,
                    attempt_count=attempt,
                    access_decision_id=access_decision_id,
                    failure_code=reason_code,
                )

    def _accept_read(
        self,
        task: AgenticChildTask,
        *,
        attempt: int,
        result: GovernedReadResult,
        rules_snapshot: _AgenticRunSnapshot,
        replay_of_run_id: str | None,
        execution: _AgenticExecutionSnapshot,
    ) -> tuple[tuple[AgenticChildTask, ...], tuple[str, ...], bool]:
        execution.validate()
        repository = execution.repository
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        rules = rules_snapshot.rules
        prepared = rules_snapshot.prepared
        decision = result.access_decision
        access_decision_id = getattr(decision, "decision_id", None)
        if not isinstance(access_decision_id, str) or not _ACCESS_DECISION_RE.fullmatch(
            access_decision_id
        ):
            raise AgenticOrchestrationError("gateway.decision_invalid")
        body_size = len(result.body)
        if (
            result.decoded_bytes != body_size
            or type(result.wire_bytes) is not int
            or result.wire_bytes < 0
            or result.wire_encoding not in {"identity", "gzip"}
            or not isinstance(result.content_encoding, str)
            or not result.content_encoding
            or len(result.content_encoding) > 64
        ):
            self._record_failed_attempt(
                task=task,
                attempt=attempt,
                status="failed",
                current_url=result.final_url,
                final_url=result.final_url,
                status_code=result.status_code,
                access_decision_id=access_decision_id,
                reason_code="gateway.evidence_invalid",
                redirect_chain=_redirect_chain(decision),
                bytes_read=min(body_size, rules.budgets.max_bytes),
                terminal=True,
                rules=rules,
                execution=execution,
            )
            return (), (), False
        redirects = _redirect_chain(decision)
        media_type = result.content_type.split(";", 1)[0].strip().casefold()
        is_page = media_type in {"text/html", "application/xhtml+xml"}
        in_compiled_scope = (
            _FROZEN_RUN_CONTAINS_PAGE_URL(rules_snapshot, result.final_url)
            if is_page
            else _FROZEN_RUN_CONTAINS_FILE_URL(rules_snapshot, result.final_url)
        )
        reason = None
        if (
            not _FROZEN_RUN_MATCHES(rules_snapshot, result.final_url)
            or not in_compiled_scope
        ):
            reason = "scope.final_url_rejected"
        elif media_type not in set(rules.content_types):
            reason = "content_type.rejected"
        elif not 200 <= result.status_code < 300:
            reason = "http.status_rejected"
        if reason is not None:
            self._record_failed_attempt(
                task=task,
                attempt=attempt,
                status="rejected",
                current_url=result.final_url,
                final_url=result.final_url,
                status_code=result.status_code,
                access_decision_id=access_decision_id,
                reason_code=reason,
                redirect_chain=redirects,
                bytes_read=body_size,
                terminal=True,
                rules=rules,
                execution=execution,
            )
            return (), (), False

        if is_page:
            raw_html = result.body.decode("utf-8", errors="replace")
            from web_listening.blocks.normalizer import normalize_html

            normalized = normalize_html(raw_html, base_url=result.final_url)
            terminal_reason = classify_html_capture(
                requested_url=task.requested_url or result.final_url,
                final_url=result.final_url,
                status_code=result.status_code,
                extracted_text=normalized.content_text,
                raw_text=raw_html,
            )
            if terminal_reason != "accepted":
                reason = f"capture.{terminal_reason}"
                self._record_failed_attempt(
                    task=task,
                    attempt=attempt,
                    status="failed",
                    current_url=result.final_url,
                    final_url=result.final_url,
                    status_code=result.status_code,
                    access_decision_id=access_decision_id,
                    reason_code=reason,
                    redirect_chain=redirects,
                    bytes_read=body_size,
                    terminal=True,
                    rules=rules,
                    execution=execution,
                )
                return (), (), False

        discovered_from = _artifact_discovery(task)
        try:
            _FROZEN_RUN_VALIDATE(rules_snapshot)
            prepared = rules_snapshot.prepared
            with repository.transaction():
                execution.validate()
                repository._assert_run_fence(task.run_id)
                stored = _FROZEN_ARTIFACT_STORE(
                    prepared.artifact_store,
                    source_run_id=task.run_id,
                    normalized_source_identity=task.requested_url or "",
                    entity_bytes=result.body,
                    response_content_type=result.content_type,
                    requested_url=task.requested_url or "",
                    source_url=task.requested_url or "",
                    final_url=result.final_url,
                    retrieved_at=self.clock(),
                    http_status=result.status_code,
                    wire_encoding=result.wire_encoding,
                    content_encoding=result.content_encoding,
                    artifact_status="completed",
                    access_decision_id=access_decision_id,
                    adapter_id=prepared.authority.read_adapter_id,
                    adapter_version=prepared.authority.read_adapter_version,
                    redirect_chain=redirects,
                    discovered_from=discovered_from,
                    parent_artifact_id=task.parent_artifact_id,
                    filename=result.filename,
                )
                repository.finish_read_budget(
                    task.run_id,
                    bytes_read=body_size,
                    pages=int(is_page),
                    files=int(not is_page),
                    max_bytes=rules.budgets.max_bytes,
                    max_pages=prepared.scope.max_pages,
                    max_files=min(
                        rules.budgets.max_files,
                        prepared.scope.max_files,
                    ),
                )
                repository.add_observation(
                    task=task,
                    attempt=attempt,
                    status="completed",
                    current_url=result.final_url,
                    final_url=result.final_url,
                    status_code=result.status_code,
                    access_decision_id=access_decision_id,
                    artifact_id=stored.observation.artifact_id,
                    reason_code="read.completed",
                    redirect_chain=redirects,
                )
                repository.transition_task(
                    task.task_id,
                    status="completed",
                    attempt_count=attempt,
                    artifact_id=stored.observation.artifact_id,
                    access_decision_id=access_decision_id,
                )
        except ArtifactStoreError as exc:
            reason = f"artifact_store.{exc.reason_code}"
            self._record_failed_attempt(
                task=task,
                attempt=attempt,
                status="failed",
                current_url=result.final_url,
                final_url=result.final_url,
                status_code=result.status_code,
                access_decision_id=access_decision_id,
                reason_code=reason,
                redirect_chain=redirects,
                bytes_read=body_size,
                terminal=True,
                rules=rules,
                execution=execution,
            )
            return (), (), False
        except AgenticOrchestrationError as exc:
            if exc.reason_code not in {
                "budget.bytes_exhausted",
                "budget.pages_exhausted",
                "budget.files_exhausted",
            }:
                raise
            self._record_failed_attempt(
                task=task,
                attempt=attempt,
                status="rejected",
                current_url=result.final_url,
                final_url=result.final_url,
                status_code=result.status_code,
                access_decision_id=access_decision_id,
                reason_code=exc.reason_code,
                redirect_chain=redirects,
                bytes_read=(
                    0 if exc.reason_code == "budget.bytes_exhausted" else body_size
                ),
                terminal=True,
                rules=rules,
                execution=execution,
            )
            return (), (exc.reason_code,), False
        created: list[AgenticChildTask] = []
        crawler_warnings: tuple[str, ...] = ()
        if task.depth < rules.budgets.max_depth and is_page:
            execution.validate()
            parent = repository.require_run(task.run_id)
            limit = max(rules.budgets.max_requests - parent.requests_used, 0)
            try:
                crawler_snapshot = execution.crawler_snapshot
                candidates, truncated = _bounded_candidates(
                    _FROZEN_ADAPTER_INVOKE(
                        crawler_snapshot,
                        body=result.body,
                        final_url=result.final_url,
                        parent_artifact_id=stored.observation.artifact_id,
                        depth=task.depth,
                    ),
                    limit=limit,
                    expected_kinds=frozenset({"link", "crawler"}),
                    expected_source_url=result.final_url,
                )
                _FROZEN_ADAPTER_VALIDATE(crawler_snapshot)
                execution.validate()
                if truncated:
                    crawler_warnings = ("budget.candidates_exhausted",)
            except Exception:  # noqa: BLE001 - this is the untrusted adapter boundary.
                candidates = ()
                crawler_warnings = ("crawler.discovery_failed",)
            for candidate in candidates:
                child = self._schedule_candidate(
                    run_id=task.run_id,
                    candidate=candidate,
                    depth=task.depth + 1,
                    parent_artifact_id=stored.observation.artifact_id,
                    discovery_adapter_id=crawler_snapshot.adapter_id,
                    discovery_adapter_version=crawler_snapshot.adapter_version,
                    rules_snapshot=rules_snapshot,
                    replay_of_run_id=replay_of_run_id,
                    execution=execution,
                )
                if child is not None:
                    created.append(child)
        return tuple(created), crawler_warnings, False

    def _schedule_candidate(
        self,
        *,
        run_id: str,
        candidate: AgenticCandidate,
        depth: int,
        parent_artifact_id: str | None,
        discovery_adapter_id: str,
        discovery_adapter_version: str,
        rules_snapshot: _AgenticRunSnapshot,
        replay_of_run_id: str | None,
        execution: _AgenticExecutionSnapshot,
    ) -> AgenticChildTask | None:
        execution.validate()
        repository = execution.repository
        _FROZEN_RUN_VALIDATE(rules_snapshot)
        rules = rules_snapshot.rules
        existing = {item.task_key: item for item in repository.list_tasks(run_id)}
        task_key = f"read:{candidate.url}"
        if task_key in existing:
            return None
        task = repository.create_task(
            run_id=run_id,
            task_key=task_key,
            kind="read",
            # The initial required set is sealed before any adapter executes.
            # Discovery may add work, but cannot elevate it into a new barrier.
            required=False,
            requested_url=candidate.url,
            depth=depth,
            discovery_kind=candidate.discovery_kind,
            discovered_from_url=candidate.discovered_from_url,
            parent_artifact_id=parent_artifact_id,
            adapter_id=execution.authority.read_adapter_id,
            adapter_version=execution.authority.read_adapter_version,
            discovery_adapter_id=discovery_adapter_id,
            discovery_adapter_version=discovery_adapter_version,
            replay_of_run_id=replay_of_run_id,
        )
        if parent_artifact_id is not None and candidate.discovery_kind in {
            "link",
            "crawler",
        }:
            execution.validate()
            parent = _FROZEN_ARTIFACT_GET(execution.artifact_store, parent_artifact_id)
            if candidate.discovered_from_url != parent.observation.final_url:
                self._reject_without_read(
                    task, "discovery.parent_mismatch", execution=execution
                )
                return None
        if depth > rules.budgets.max_depth:
            self._reject_without_read(
                task, "budget.depth_exhausted", execution=execution
            )
            return None
        if not _FROZEN_RUN_MATCHES(
            rules_snapshot, candidate.url
        ) or not _FROZEN_RUN_CONTAINS_URL(rules_snapshot, candidate.url):
            self._reject_without_read(
                task, "scope.candidate_rejected", execution=execution
            )
            return None
        return task

    def _reject_without_read(
        self,
        task: AgenticChildTask,
        reason: str,
        *,
        execution: _AgenticExecutionSnapshot,
    ) -> None:
        execution.validate()
        execution.repository.transition_task(
            task.task_id,
            status="rejected",
            failure_code=reason,
        )

    def _cancel_pending(
        self,
        run_id: str,
        *,
        current: AgenticChildTask | None = None,
        execution: _AgenticExecutionSnapshot,
    ) -> None:
        execution.validate()
        for task in execution.repository.list_tasks(run_id):
            if task.status in {"queued", "running"}:
                execution.repository.transition_task(
                    task.task_id,
                    status="cancelled",
                    failure_code="run.cancelled",
                )

    def _result(
        self, run_id: str, *, execution: _AgenticExecutionSnapshot
    ) -> AgenticRunResult:
        execution.validate()
        repository = execution.repository
        parent = repository.require_run(run_id)
        tasks = repository.list_tasks(run_id)
        observations = repository.list_observations(run_id)
        artifact_ids = tuple(
            dict.fromkeys(
                item.artifact_id
                for item in observations
                if item.artifact_id is not None
            )
        )
        artifacts = tuple(
            _FROZEN_ARTIFACT_GET(execution.artifact_store, artifact_id)
            for artifact_id in artifact_ids
        )
        return AgenticRunResult(
            parent=parent,
            tasks=tasks,
            observations=observations,
            artifacts=artifacts,
        )


class _StorageTurn:
    def __init__(self, storage) -> None:
        self.storage = storage

    def __enter__(self):
        condition = self.storage._execution_transaction_condition
        condition.acquire()
        thread_id = threading.get_ident()
        while (
            self.storage._execution_transaction_depth > 0
            and self.storage._execution_transaction_owner != thread_id
        ):
            condition.wait()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.storage._execution_transaction_condition.release()


class _ExecutionTransaction:
    def __init__(self, storage) -> None:
        self.storage = storage

    def __enter__(self):
        self.storage.begin_execution_transaction()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.storage.execution_transaction_owned_by_current_thread:
            return
        if exc_type is None:
            self.storage.commit_execution_transaction()
        else:
            self.storage.rollback_execution_transaction()


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise AgenticOrchestrationError("time.invalid")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _normalize_persisted_time(value: object) -> str:
    rendered = _ledger_text(value)
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        raise ValueError("ledger timestamp is invalid")
    legacy = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    normalized = _format_time(parsed)
    if rendered not in {legacy, normalized}:
        raise ValueError("ledger timestamp is invalid")
    return normalized


def _ledger_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("ledger text is invalid")
    return value


def _ledger_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("ledger integer is invalid")
    return value


def _ledger_positive_int(value: object) -> int:
    checked = _ledger_nonnegative_int(value)
    if checked < 1:
        raise ValueError("ledger integer is invalid")
    return checked


def _ledger_boolean(value: object) -> bool:
    if type(value) is not int or value not in {0, 1}:
        raise ValueError("ledger boolean is invalid")
    return bool(value)


def _ledger_time(value: object) -> str:
    rendered = _ledger_text(value)
    if _normalize_persisted_time(rendered) != rendered:
        raise ValueError("ledger timestamp is invalid")
    return rendered


def _ledger_optional_time(value: object) -> str | None:
    return None if value is None else _ledger_time(value)


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise AgenticOrchestrationError("ledger.migration_invalid")


def _derive_parent_outcome(
    tasks: Sequence[AgenticChildTask],
    *,
    warnings: Sequence[str] = (),
    cancelled: bool = False,
) -> str:
    if not tasks or any(task.status not in _TERMINAL_TASK_STATUSES for task in tasks):
        raise AgenticOrchestrationError("task.children_pending")
    if "run.cancelled" in warnings:
        return "cancelled"
    if "run.interrupted" in warnings:
        return "failed"
    required = tuple(task for task in tasks if task.required)
    if not required:
        raise AgenticOrchestrationError("task.required_children_missing")
    if cancelled or any(task.status == "cancelled" for task in tasks):
        return "cancelled"
    if warnings and any(task.status == "completed" for task in tasks):
        return "partial"
    if any(task.status == "failed" for task in required):
        return "failed"
    if any(task.status == "rejected" for task in required):
        return "rejected"
    if any(task.status == "partial" for task in required):
        return "partial"
    if warnings or any(task.status != "completed" for task in tasks):
        return "partial"
    return "completed"


def _bounded_candidates(
    candidates: Iterable[AgenticCandidate],
    *,
    limit: int,
    expected_kinds: frozenset[str] | None = None,
    expected_source_url: str | None = None,
) -> tuple[tuple[AgenticCandidate, ...], bool]:
    iterator = iter(candidates)
    selected = tuple(islice(iterator, limit))
    sentinel = object()
    truncated = next(iterator, sentinel) is not sentinel
    if any(type(item) is not AgenticCandidate for item in selected):
        raise _CandidateValidationError(
            "discovery adapter returned an invalid candidate"
        )
    if expected_kinds is not None and any(
        item.discovery_kind not in expected_kinds for item in selected
    ):
        raise _CandidateValidationError("candidate discovery kind is invalid")
    if expected_source_url is not None and any(
        item.discovered_from_url != expected_source_url for item in selected
    ):
        raise _CandidateValidationError("candidate discovery source is invalid")
    return tuple(sorted(selected, key=lambda item: item.url)), truncated


def _validate_observation_provenance(
    *,
    task: AgenticChildTask,
    observation_status: object,
    reason_code: object,
    current_url: object,
    final_url: object,
    status_code: object,
    access_decision_id: object,
    redirect_chain: Sequence[Mapping[str, Any]],
    discovery: Mapping[str, Any],
) -> None:
    redirect_keys = {
        "ordinal",
        "from_url",
        "to_url",
        "http_status",
        "access_decision_id",
        "decision",
    }
    discovery_keys = {
        "kind",
        "source_url",
        "parent_artifact_id",
        "adapter_id",
        "adapter_version",
    }
    if (
        not isinstance(current_url, str)
        or canonicalize_access_url(current_url) != current_url
    ):
        raise ValueError("invalid current URL")
    if status_code is not None and (
        type(status_code) is not int or not 100 <= status_code <= 599
    ):
        raise ValueError("invalid HTTP status")
    endpoint = task.requested_url
    for ordinal, hop in enumerate(redirect_chain):
        if set(hop) != redirect_keys or hop["ordinal"] != ordinal:
            raise ValueError("invalid redirect hop shape")
        source = hop["from_url"]
        target = hop["to_url"]
        if (
            source != endpoint
            or not isinstance(target, str)
            or canonicalize_access_url(source) != source
            or canonicalize_access_url(target) != target
            or hop["http_status"] not in {301, 302, 303, 307, 308}
            or not isinstance(hop["access_decision_id"], str)
            or not _ACCESS_DECISION_RE.fullmatch(hop["access_decision_id"])
            or hop["decision"] != "allow"
        ):
            raise ValueError("invalid redirect hop evidence")
        endpoint = target
    if redirect_chain and current_url != endpoint:
        raise ValueError("redirect endpoint mismatch")
    if not redirect_chain and current_url != task.requested_url:
        raise ValueError("empty redirect endpoint mismatch")
    rejected_redirect_target = (
        final_url is not None
        and final_url != current_url
        and observation_status in {"failed", "rejected"}
        and status_code in {301, 302, 303, 307, 308}
        and reason_code in {"gateway.origin", "gateway.redirect"}
    )
    if (
        final_url is not None
        and current_url != final_url
        and not rejected_redirect_target
    ):
        raise ValueError("final endpoint mismatch")
    if (
        access_decision_id is not None
        and redirect_chain
        and redirect_chain[-1]["access_decision_id"] != access_decision_id
    ):
        raise ValueError("redirect decision mismatch")
    if access_decision_id is None:
        decisionless_reason = reason_code in _DECISIONLESS_REASON_CODES
        if (
            observation_status not in {"failed", "rejected"}
            or not decisionless_reason
            or redirect_chain
            or final_url != current_url
            or status_code is not None
        ):
            raise ValueError("decisionless failure evidence is invalid")
    if observation_status == "completed" and (
        access_decision_id is None
        or final_url is None
        or status_code is None
        or not 200 <= status_code < 300
    ):
        raise ValueError("completed response evidence is invalid")
    if set(discovery) != discovery_keys or discovery["kind"] != task.discovery_kind:
        raise ValueError("invalid discovery shape")
    expected = (
        task.discovered_from_url,
        task.parent_artifact_id,
        task.discovery_adapter_id,
        task.discovery_adapter_version,
    )
    actual = (
        discovery["source_url"],
        discovery["parent_artifact_id"],
        discovery["adapter_id"],
        discovery["adapter_version"],
    )
    if actual != expected:
        raise ValueError("discovery binding mismatch")
    if task.discovery_kind == "seed" and any(value is not None for value in actual):
        raise ValueError("seed discovery must be root evidence")
    if task.discovery_kind == "search" and (
        task.kind != "read"
        or task.parent_artifact_id is not None
        or task.discovered_from_url is None
    ):
        raise ValueError("search discovery is invalid")
    if task.discovery_kind in {"link", "crawler"} and (
        task.kind != "read"
        or task.parent_artifact_id is None
        or task.discovered_from_url is None
    ):
        raise ValueError("crawler discovery is invalid")


def _validate_task_discovery(
    *,
    kind: str,
    discovery_kind: str,
    discovered_from_url: object,
    parent_artifact_id: object,
    discovery_adapter_id: object,
    discovery_adapter_version: object,
) -> None:
    values = (
        discovered_from_url,
        parent_artifact_id,
        discovery_adapter_id,
        discovery_adapter_version,
    )
    if kind == "search":
        if discovery_kind != "search" or any(value is not None for value in values):
            raise ValueError("search task discovery is invalid")
        return
    if kind != "read":
        raise ValueError("task kind is invalid")
    if discovery_kind == "seed":
        if any(value is not None for value in values):
            raise ValueError("seed task discovery is invalid")
        return
    if discovery_kind == "search":
        if (
            discovered_from_url is None
            or parent_artifact_id is not None
            or discovery_adapter_id is None
            or discovery_adapter_version is None
        ):
            raise ValueError("search candidate discovery is invalid")
        return
    if discovery_kind in {"link", "crawler"}:
        if any(value is None for value in values):
            raise ValueError("crawler candidate discovery is invalid")
        return
    raise ValueError("discovery kind is invalid")


def _stable_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _url_origin(
    url: str,
    _normalizer: Callable[[str], tuple[str, Any]] = normalize_http_url,
) -> str:
    _, origin = _normalizer(url)
    return origin.as_url_origin()


def _path_within_prefix(path: str, prefix: str) -> bool:
    normalized = "/" + prefix.strip("/") if prefix != "/" else "/"
    return normalized == "/" or path == normalized or path.startswith(normalized + "/")


@dataclass(frozen=True, slots=True)
class _AgenticPredicateDispatch:
    fnmatchcase: Callable[[str, str], bool]
    canonicalize_access_url: Callable[[str], str]
    url_origin: Callable[[str], str]
    urlsplit: Callable[[str], Any]
    path_within_prefix: Callable[[str, str], bool]
    normalize_http_url: Callable[[str], tuple[str, Any]]
    access_decision_graph: tuple[object, ...]


def _access_decision_canonicalization_graph() -> tuple[object, ...]:
    return (
        access_decision_module.canonicalize_access_url,
        access_decision_module._canonical_url,
        access_decision_module._canonical_query,
        access_decision_module._query_decoding_passes,
        access_decision_module._validate_non_sensitive_text,
        access_decision_module._is_access_secret_like_key,
        access_decision_module.contains_uri_userinfo,
        access_decision_module.is_secret_like_key,
        access_decision_module.canonicalize_requested_http_url,
        access_decision_module.urlsplit,
        access_decision_module.urlunsplit,
        access_decision_module.unquote,
        access_decision_module.unquote_plus,
        access_decision_module.unicodedata.normalize,
        access_decision_module.re.sub,
        access_decision_module.re.search,
    )


def _validate_agentic_predicate_dispatch(
    dispatch: _AgenticPredicateDispatch,
) -> tuple[int, ...]:
    if (
        type(dispatch) is not _AgenticPredicateDispatch
        or dispatch is not _FROZEN_AGENTIC_PREDICATE_DISPATCH
        or fnmatchcase is not dispatch.fnmatchcase
        or canonicalize_access_url is not dispatch.canonicalize_access_url
        or _url_origin is not dispatch.url_origin
        or urlsplit is not dispatch.urlsplit
        or _path_within_prefix is not dispatch.path_within_prefix
        or normalize_http_url is not dispatch.normalize_http_url
        or _access_decision_canonicalization_graph() != dispatch.access_decision_graph
    ):
        raise ValueError("Agentic predicate dispatch changed")
    return tuple(
        id(item)
        for item in (
            dispatch.fnmatchcase,
            dispatch.canonicalize_access_url,
            dispatch.url_origin,
            dispatch.urlsplit,
            dispatch.path_within_prefix,
            dispatch.normalize_http_url,
            *dispatch.access_decision_graph,
        )
    )


_AGENTIC_PREDICATE_DISPATCH = _AgenticPredicateDispatch(
    fnmatchcase=fnmatchcase,
    canonicalize_access_url=canonicalize_access_url,
    url_origin=_url_origin,
    urlsplit=urlsplit,
    path_within_prefix=_path_within_prefix,
    normalize_http_url=normalize_http_url,
    access_decision_graph=_access_decision_canonicalization_graph(),
)
_AGENTIC_PREDICATE_DISPATCH_VALIDATOR = _validate_agentic_predicate_dispatch
_FROZEN_AGENTIC_PREDICATE_DISPATCH = _AGENTIC_PREDICATE_DISPATCH
_FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR = _AGENTIC_PREDICATE_DISPATCH_VALIDATOR
_BIND_AGENTIC_PREDICATE_ROOTS(
    _AGENTIC_PREDICATE_DISPATCH,
    _AGENTIC_PREDICATE_DISPATCH_VALIDATOR,
)
del _BIND_AGENTIC_PREDICATE_ROOTS


def _redirect_chain(context: object | None) -> tuple[dict[str, Any], ...]:
    if context is None:
        return ()
    decision = getattr(context, "decision", None) or context
    final_decision_id = getattr(decision, "decision_id", None)
    hops = tuple(
        getattr(context, "redirect_hops", None)
        or getattr(decision, "redirect_hops", ())
        or ()
    )
    chain: list[dict[str, Any]] = []
    for index, hop in enumerate(hops):
        proof = getattr(hop, "access_proof", None)
        proof_id = getattr(proof, "decision_id", final_decision_id)
        decision_id = (
            final_decision_id
            if index == len(hops) - 1 and final_decision_id is not None
            else proof_id
        )
        chain.append(
            {
                "ordinal": index,
                "from_url": hop.source_url,
                "to_url": hop.canonical_target_url,
                "http_status": hop.http_status,
                "access_decision_id": decision_id,
                "decision": "allow",
            }
        )
    return tuple(chain)


def _gateway_failure(
    error: AccessGatewayError,
) -> tuple[str, bool, object | None]:
    if isinstance(error, AccessGatewayBudgetError) and error.__cause__ is not None:
        cause = error.__cause__
        if isinstance(cause, AgenticOrchestrationError):
            return cause.reason_code, False, error.decision
    if isinstance(error, AccessGatewayTransportError):
        kind = (
            error.kind
            if isinstance(error.kind, str) and error.kind in _KNOWN_TRANSPORT_KINDS
            else "unclassified_transport"
        )
        return (
            f"gateway.transport.{kind}",
            bool(error.retryable) and kind != "unclassified_transport",
            error.decision,
        )
    return (
        f"gateway.{type(error).__name__.removeprefix('AccessGateway').removesuffix('Error').casefold()}",
        False,
        getattr(error, "decision", None),
    )


def _artifact_discovery(task: AgenticChildTask) -> dict[str, Any]:
    if task.discovery_kind == "seed":
        return {"kind": "seed", "artifact_id": None, "source_url": None}
    if task.discovery_kind == "search":
        return {
            "kind": "search",
            "artifact_id": None,
            "source_url": task.discovered_from_url,
        }
    return {
        "kind": task.discovery_kind,
        "artifact_id": task.parent_artifact_id,
        "source_url": task.discovered_from_url,
    }


def _safe_reason_component(value: object, fallback: str) -> str:
    rendered = str(value)
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rendered):
        return rendered
    if rendered.startswith("unclassified_body_failure:"):
        return "unclassified_body_failure"
    return fallback


__all__ = [
    "ACQUISITION_BATCH_RESULT_VERSION",
    "AGENTIC_ORCHESTRATION_VERSION",
    "AGENTIC_SITE_RULES_VERSION",
    "AgenticAuthority",
    "AgenticBudgets",
    "AgenticCandidate",
    "AgenticChildTask",
    "AgenticOrchestrationError",
    "AgenticOrchestrator",
    "AgenticParentTask",
    "AgenticQuery",
    "AgenticReadObservation",
    "AgenticRunResult",
    "AgenticScopeRules",
    "AgenticSiteRules",
    "AgenticTaskRepository",
    "AuthorizedSearchAdapter",
    "CrawlerDiscoveryAdapter",
    "HtmlLinkCrawlerAdapter",
    "PreparedAgenticAuthority",
    "load_agentic_site_rules",
    "prepare_agentic_authority",
]
