from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
)


ExecutorId = Literal[
    "web_http",
    "browser_rendered",
    "browseract",
    "sitemap",
    "rss",
    "cloakbrowser",
    "batch_python",
]
def _serialize_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _serialize_json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_json_value(child) for child in value]
    return value


JsonObject = Annotated[
    Mapping[str, JsonValue],
    PlainSerializer(_serialize_json_value, return_type=dict[str, JsonValue]),
]
NonEmptyString = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SkillVersion = Annotated[
    str,
    Field(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"),
]

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SECRET_KEY_PARTS = {
    "authorization", "cookie", "credential", "key", "password", "secret", "token"
}
_SECRET_KEY_NAMES = {
    "accesskey", "apikey", "awsaccesskeyid", "clientapikey", "privatekey",
    "proxyauth", "proxycredential", "proxycredentials", "proxypassword",
    "proxyuser", "proxyusername", "xapikey",
}
_SECRET_COMPACT_SUFFIXES = (
    "accesskeyid", "accesskey", "apikey", "authorization", "cookie", "credential",
    "credentials", "password", "secret", "token",
)
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9\u00b9\u00b2\u00b3]|"
    r"lpt[1-9\u00b9\u00b2\u00b3])(?:\..*)?$", re.IGNORECASE
)


class ImmutableJsonMapping(Mapping[str, JsonValue]):
    """An immutable JSON mapping with constant-time key lookup."""

    __slots__ = ("__data",)

    def __init__(
        self,
        values: Mapping[str, JsonValue] | tuple[tuple[str, JsonValue], ...],
    ) -> None:
        items = tuple(values.items() if isinstance(values, Mapping) else values)
        data = dict(items)
        if len(items) != len(data):
            raise ValueError("ImmutableJsonMapping keys must be unique")
        object.__setattr__(self, "_ImmutableJsonMapping__data", MappingProxyType(data))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getitem__(self, key: str) -> JsonValue:
        return self.__data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__data)

    def __len__(self) -> int:
        return len(self.__data)

    def __contains__(self, key: object) -> bool:
        return key in self.__data

    def keys(self):
        return self.__data.keys()

    def items(self):
        return self.__data.items()

    def values(self):
        return self.__data.values()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self.__data == other
        return NotImplemented

    __hash__ = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.__data)!r})"

    def __copy__(self) -> ImmutableJsonMapping:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ImmutableJsonMapping:
        return self


def _validate_unique_json_object_keys(json_data: str | bytes | bytearray) -> None:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    try:
        json.loads(json_data, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Pydantic below retains its normal JSON parse-error shape and location.
        pass


class StrictContractModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        if kwargs.get("strict") is False:
            raise TypeError("strict=False is not supported for governed contracts")
        extra = kwargs.pop("extra", None)
        if extra not in (None, "forbid"):
            raise TypeError("extra must be None or 'forbid' for governed contracts")
        return super().model_validate(obj, **kwargs)

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        if "experimental_allow_partial" in kwargs:
            raise TypeError(
                "experimental_allow_partial is not supported for governed contract JSON"
            )
        if kwargs.get("strict") is False:
            raise TypeError("strict=False is not supported for governed contract JSON")
        extra = kwargs.pop("extra", None)
        if extra not in (None, "forbid"):
            raise TypeError(
                "extra must be None or 'forbid' for governed contract JSON"
            )
        _validate_unique_json_object_keys(json_data)
        return super().model_validate_json(json_data, **kwargs)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        values = self.model_dump(mode="python", round_trip=True)
        if update:
            values.update(update)
        return type(self).model_validate(values)


def require_aware_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


def _urlsplit_for_userinfo_detection(value: str):
    detection_value = unicodedata.normalize("NFKC", value).replace("\\", "/")
    special_scheme = re.match(r"^(https?):/*(.*)$", detection_value, re.I)
    if special_scheme:
        detection_value = f"{special_scheme.group(1)}://{special_scheme.group(2)}"
    return urlsplit(detection_value)


def validate_portable_json(value: JsonObject) -> JsonObject:
    def visit(item: JsonValue, location: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = unicodedata.normalize("NFKC", key)
                normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
                normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).lower()
                parts = {part for part in normalized.split("_") if part}
                compact = normalized.replace("_", "")
                if (
                    parts & _SECRET_KEY_PARTS
                    or compact in _SECRET_KEY_NAMES
                    or compact.endswith(_SECRET_COMPACT_SUFFIXES)
                ):
                    raise ValueError(
                        f"{location} contains forbidden secret-like key: {key}"
                    )
                visit(child, f"{location}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{location} contains a non-finite number")
        elif isinstance(item, str):
            parsed = _urlsplit_for_userinfo_detection(item)
            if parsed.netloc and parsed.username is not None:
                raise ValueError(f"{location} contains URI userinfo")

    def freeze(item: JsonValue) -> JsonValue:
        if isinstance(item, Mapping):
            return ImmutableJsonMapping(
                {key: freeze(child) for key, child in item.items()}
            )
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    visit(value, "JSON metadata")
    return freeze(value)  # type: ignore[return-value]


def validate_http_url_without_credentials(value: object) -> object:
    parsed = _urlsplit_for_userinfo_detection(str(value))
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain credentials or userinfo")
    return value


def validate_domain(value: str) -> str:
    if (
        value != value.strip()
        or value != value.lower()
        or not _DOMAIN_RE.fullmatch(value)
    ):
        raise ValueError("allowed_domains must contain canonical lowercase hostnames")
    return value


def validate_portable_relative_path(
    value: str | None,
    *,
    field_name: str,
    suffixes: tuple[str, ...] = (),
) -> str | None:
    if value is None:
        return None
    if (
        not value
        or value != value.strip()
        or _CONTROL_RE.search(value)
        or "\\" in value
        or value.startswith("/")
        or "//" in value
    ):
        raise ValueError(
            f"{field_name} must be a canonical portable POSIX relative path"
        )
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(
            f"{field_name} must not contain empty, dot, or dotdot components"
        )
    if any(
        any(char in _WINDOWS_INVALID_FILENAME_CHARS for char in part)
        or part.endswith((" ", "."))
        or _WINDOWS_RESERVED_NAME_RE.fullmatch(part)
        for part in raw_parts
    ):
        raise ValueError(
            f"{field_name} must not contain Windows-invalid characters or reserved names"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError(
            f"{field_name} must be a canonical portable POSIX relative path"
        )
    if suffixes and path.suffix.lower() not in suffixes:
        expected = ", ".join(suffixes)
        raise ValueError(f"{field_name} must use one of these suffixes: {expected}")
    return value


def validate_script_path(value: str | None) -> str | None:
    return validate_portable_relative_path(
        value, field_name="script_path", suffixes=(".py",)
    )


def validate_profile_ref(value: str) -> str:
    validated = validate_portable_relative_path(
        value, field_name="profile_ref", suffixes=(".json", ".yaml", ".yml")
    )
    assert validated is not None
    return validated


def validate_entrypoint(value: str) -> str:
    validated = validate_portable_relative_path(
        value, field_name="entrypoint", suffixes=(".py",)
    )
    assert validated is not None
    return validated


def validate_artifact_path(value: str | None) -> str | None:
    return validate_portable_relative_path(value, field_name="artifact_path")
