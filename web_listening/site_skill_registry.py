"""Read-only discovery and static validation for packaged Site Skills."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from web_listening.contracts import SiteSkill

PACKAGE_DIGEST_FRAME = b"web-listening.site-skill-package.v1\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_RESERVED_RE = re.compile(
    r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]|CONIN\$|CONOUT\$)(?:\..*)?$"
)
_GOVERNED_SUFFIXES = (".json", ".yaml", ".yml", ".md", ".py", ".txt")
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)["']?(?P<key>[\w-]+)["']?\s*[:=]\s*
    (?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,]+)"""
)
_AUTHORIZATION_VALUE_RE = re.compile(r"(?i)^(?:bearer|basic)\s+(?P<value>.+)$")
_PROXY_URL_RE = re.compile(r"(?i)\b(?:https?|socks[45]?)://[^\s\"'<>]+")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
_POSIX_ABSOLUTE_RE = re.compile(
    r"""(?<![\w:/\\])/(?:[^\s"'<>/]+(?:/[^\s"'<>/]*)*|/?(?=["']|[ \t]*(?:[,}\]\r\n]|$)))"""
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"""(?<!\w)[A-Za-z]:[\\/][^\s"'<>]+""")
_UNC_RE = re.compile(r"""(?<![\\\w])\\\\[^\\\s"'<>]+\\[^\s"'<>]+""")
_FORWARD_UNC_RE = re.compile(r"""(?<![:/\w])//[^/\s"'<>]+/[^\s"'<>]+""")
_FILE_URL_RE = re.compile(r"(?i)\bfile:(?://)?[^\s\"'<>]+")
_REFERENCE_RE = re.compile(
    r"(?ix)^(?:\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|%[A-Z_][A-Z0-9_]*%|env:[A-Z_][A-Z0-9_]*|<[^>]+>|\{\{[^}]+\}\}|(?:secret|vault|keyring|aws-sm|gcp-sm|azure-kv)://.+)$"
)
_URL_KEY_RE = re.compile(r"(?i)(?:^|[_-])(?:url|uri|endpoint)(?:$|[_-])")
_MAX_STRUCTURED_DEPTH = 100
_MAX_PACKAGE_DEPTH = 100
_MAX_PACKAGE_FILES = 1024
_MAX_REGISTRY_ENTRIES = 1024
_PACKAGE_ENTRY_MULTIPLIER = 2
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_PACKAGE_BYTES = 32 * 1024 * 1024
_MAX_PACKAGE_DIAGNOSTICS = 1024


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class _DiagnosticCollector(list[Diagnostic]):
    """Collect a bounded diagnostic prefix plus one stable truncation marker."""

    def __init__(self) -> None:
        super().__init__()
        self._truncated = False
        self.codes: set[str] = set()

    def append(self, item: Diagnostic) -> None:
        self.codes.add(item.code)
        if len(self) < _MAX_PACKAGE_DIAGNOSTICS:
            super().append(item)
        elif not self._truncated:
            self[-1] = Diagnostic(
                "package.diagnostic_limit",
                ".",
                f"package diagnostics truncated at {_MAX_PACKAGE_DIAGNOSTICS} entries",
            )
            self._truncated = True

    def extend(self, items: Iterable[Diagnostic]) -> None:
        for item in items:
            self.append(item)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except (TypeError, ValueError) as exc:
            raise yaml.constructor.ConstructorError(
                None, None, "mapping key must be hashable", key_node.start_mark
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate mapping key", key_node.start_mark
            )
        try:
            result[key] = loader.construct_object(value_node, deep=deep)
        except (TypeError, ValueError) as exc:
            raise yaml.constructor.ConstructorError(
                None, None, "mapping key must be hashable", key_node.start_mark
            ) from exc
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def default_registry_root() -> Path:
    return Path(__file__).resolve().parent / "skills" / "sites"


def _diagnostic(code: str, path: str, message: str) -> Diagnostic:
    return Diagnostic(code, path, message)


def _canonical_component(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return (
        bool(value)
        and value == value.strip()
        and value == unicodedata.normalize("NFC", value)
        and not value.endswith((" ", "."))
        and not any(
            unicodedata.category(character).startswith("C") or character in '<>:"/\\|?*'
            for character in value
        )
        and not _WINDOWS_RESERVED_RE.fullmatch(value)
    )


def _safe_relative(value: str) -> bool:
    if not value or "\\" in value or value != value.strip() or _DRIVE_RE.match(value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(
        part not in {"", ".", ".."} and _canonical_component(part)
        for part in path.parts
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256(PACKAGE_DIGEST_FRAME)
    for relative, content in sorted(files):
        try:
            name = relative.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            # Invalid names are diagnosed during the descriptor walk. Keep digesting
            # total and deterministic without accepting a lossy encoding.
            name = relative.encode("utf-8", errors="backslashreplace")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


_INCOMPLETE_PACKAGE_DIGEST_CODES = {
    "package.aggregate_size_limit",
    "package.depth_limit",
    "package.file_count_limit",
    "package.file_size_limit",
    "package.not_directory",
    "package.resource_exhausted",
    "path.tree_changed",
    "path.unreadable",
}


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_tree(
    package: Path, diagnostics: list[Diagnostic], *, package_fd: int | None = None
) -> list[tuple[str, bytes]]:
    """Read a stable tree through directory descriptors, refusing links and races."""
    files: list[tuple[str, bytes]] = []
    file_count = 0
    entry_count = 0
    traversal_stopped = False
    aggregate_bytes = 0
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = (
            os.dup(package_fd) if package_fd is not None else os.open(package, flags)
        )
    except OSError:
        diagnostics.append(
            _diagnostic(
                "package.not_directory", ".", "package must be a real directory"
            )
        )
        return files

    seen: dict[str, str] = {}

    def walk(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal aggregate_bytes, entry_count, file_count, traversal_stopped
        if traversal_stopped:
            return
        try:
            before = os.fstat(directory_fd)
            names = []
            entry_limit = _MAX_PACKAGE_FILES * _PACKAGE_ENTRY_MULTIPLIER
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > entry_limit:
                        traversal_stopped = True
                        diagnostics.append(
                            _diagnostic(
                                "package.file_count_limit",
                                ".",
                                f"package contains more than {_MAX_PACKAGE_FILES} files",
                            )
                        )
                        return
                    names.append(entry.name)
            names.sort()
        except OSError:
            diagnostics.append(
                _diagnostic(
                    "path.unreadable", prefix or ".", "directory cannot be inspected"
                )
            )
            return
        for name in names:
            if traversal_stopped:
                return
            relative = f"{prefix}/{name}" if prefix else name
            if not _safe_relative(relative):
                diagnostics.append(
                    _diagnostic(
                        "path.invalid",
                        relative,
                        "path is not portable, NFC, and canonical",
                    )
                )
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in seen and seen[folded] != relative:
                diagnostics.append(
                    _diagnostic(
                        "path.case_collision",
                        relative,
                        "portable path collides after NFC and case folding",
                    )
                )
            seen[folded] = relative
            try:
                entry_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                diagnostics.append(
                    _diagnostic(
                        "path.tree_changed",
                        relative,
                        "package tree changed during validation",
                    )
                )
                continue
            if stat.S_ISLNK(entry_before.st_mode):
                diagnostics.append(
                    _diagnostic("path.symlink", relative, "symlinks are forbidden")
                )
                continue
            if stat.S_ISDIR(entry_before.st_mode):
                if depth >= _MAX_PACKAGE_DEPTH:
                    diagnostics.append(
                        _diagnostic(
                            "package.depth_limit",
                            relative,
                            f"package directory depth exceeds {_MAX_PACKAGE_DEPTH}",
                        )
                    )
                    continue
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    diagnostics.append(
                        _diagnostic(
                            "path.tree_changed",
                            relative,
                            "directory changed during validation",
                        )
                    )
                    continue
                try:
                    if _identity(os.fstat(child_fd)) != _identity(entry_before):
                        diagnostics.append(
                            _diagnostic(
                                "path.tree_changed",
                                relative,
                                "directory changed during validation",
                            )
                        )
                    else:
                        walk(child_fd, relative, depth + 1)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_before.st_mode):
                file_count += 1
                if file_count > _MAX_PACKAGE_FILES:
                    traversal_stopped = True
                    diagnostics.append(
                        _diagnostic(
                            "package.file_count_limit",
                            ".",
                            f"package contains more than {_MAX_PACKAGE_FILES} files",
                        )
                    )
                    return
                if entry_before.st_size > _MAX_FILE_BYTES:
                    diagnostics.append(
                        _diagnostic(
                            "package.file_size_limit",
                            relative,
                            f"file exceeds {_MAX_FILE_BYTES} bytes",
                        )
                    )
                    continue
                if aggregate_bytes + entry_before.st_size > _MAX_PACKAGE_BYTES:
                    diagnostics.append(
                        _diagnostic(
                            "package.aggregate_size_limit",
                            relative,
                            f"package exceeds {_MAX_PACKAGE_BYTES} bytes",
                        )
                    )
                    continue
                file_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                    with os.fdopen(file_fd, "rb", closefd=True) as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            raise OSError("opened entry is not a regular file")
                        if _identity(opened) != _identity(entry_before):
                            raise OSError("opened entry identity changed")
                        if opened.st_size > _MAX_FILE_BYTES:
                            diagnostics.append(
                                _diagnostic(
                                    "package.file_size_limit",
                                    relative,
                                    f"file exceeds {_MAX_FILE_BYTES} bytes",
                                )
                            )
                            continue
                        if aggregate_bytes + opened.st_size > _MAX_PACKAGE_BYTES:
                            diagnostics.append(
                                _diagnostic(
                                    "package.aggregate_size_limit",
                                    relative,
                                    f"package exceeds {_MAX_PACKAGE_BYTES} bytes",
                                )
                            )
                            continue
                        data = stream.read(_MAX_FILE_BYTES + 1)
                        if len(data) > _MAX_FILE_BYTES:
                            diagnostics.append(
                                _diagnostic(
                                    "package.file_size_limit",
                                    relative,
                                    f"file exceeds {_MAX_FILE_BYTES} bytes",
                                )
                            )
                            continue
                        if aggregate_bytes + len(data) > _MAX_PACKAGE_BYTES:
                            diagnostics.append(
                                _diagnostic(
                                    "package.aggregate_size_limit",
                                    relative,
                                    f"package exceeds {_MAX_PACKAGE_BYTES} bytes",
                                )
                            )
                            continue
                        after_read = os.fstat(stream.fileno())
                    entry_after = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError:
                    diagnostics.append(
                        _diagnostic(
                            "path.tree_changed",
                            relative,
                            "file changed during validation",
                        )
                    )
                    continue
                if _identity(opened) != _identity(after_read) or _identity(
                    opened
                ) != _identity(entry_after):
                    diagnostics.append(
                        _diagnostic(
                            "path.tree_changed",
                            relative,
                            "file changed during validation",
                        )
                    )
                    continue
                aggregate_bytes += len(data)
                files.append((relative, data))
            else:
                diagnostics.append(
                    _diagnostic(
                        "path.non_regular",
                        relative,
                        "only directories and regular files are allowed",
                    )
                )
        try:
            after = os.fstat(directory_fd)
            after_names = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    after_names.append(entry.name)
                    if len(after_names) > entry_limit:
                        break
            after_names.sort()
        except OSError:
            diagnostics.append(
                _diagnostic(
                    "path.tree_changed",
                    prefix or ".",
                    "package tree changed during validation",
                )
            )
            return
        if _identity(before) != _identity(after) or names != after_names:
            diagnostics.append(
                _diagnostic(
                    "path.tree_changed",
                    prefix or ".",
                    "package tree changed during validation",
                )
            )

    try:
        try:
            walk(root_fd, "", 0)
        except MemoryError:
            diagnostics.append(
                _diagnostic(
                    "package.resource_exhausted",
                    ".",
                    "package validation exceeded available memory",
                )
            )
        if package_fd is None:
            try:
                path_after = os.stat(package, follow_symlinks=False)
                if _identity(os.fstat(root_fd))[:3] != _identity(path_after)[:3]:
                    diagnostics.append(
                        _diagnostic(
                            "path.tree_changed",
                            ".",
                            "package root changed during validation",
                        )
                    )
            except OSError:
                diagnostics.append(
                    _diagnostic(
                        "path.tree_changed",
                        ".",
                        "package root changed during validation",
                    )
                )
    finally:
        os.close(root_fd)
    return files


def _json_unique(data: bytes) -> Any:
    def reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=reject)


def _secret_key(key: str) -> bool:
    normalized = unicodedata.normalize("NFKC", key)
    words = [
        word
        for chunk in re.findall(r"[a-zA-Z0-9]+", normalized)
        for word in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", chunk)
    ]
    singular = {
        "auths": "auth",
        "cookies": "cookie",
        "credentials": "credential",
        "keys": "key",
        "passwords": "password",
        "schemes": "scheme",
        "secrets": "secret",
        "tokens": "token",
        "authorizations": "authorization",
        "counts": "count",
        "domains": "domain",
        "policies": "policy",
        "urls": "url",
    }
    parts = [singular.get(word.lower(), word.lower()) for word in words]
    public_metadata_names = {
        ("authorization", "url"),
        ("token", "count"),
        ("password", "policy"),
        ("cookie", "domain"),
        ("token", "endpoint"),
        ("token", "url"),
        ("auth", "method"),
        ("password", "reset", "url"),
        ("cookie", "name"),
        ("credential", "type"),
    }
    if tuple(parts) in public_metadata_names:
        return False
    public_metadata_families = {
        ("password", "strength", "hint"),
        ("token", "bucket", "size"),
    }
    if tuple(parts) in public_metadata_families:
        return False
    if parts in (
        ["allowed", "reference", "scheme"],
        ["allowed", "references", "scheme"],
        ["secret", "policy"],
        ["allow", "secret", "references"],
        ["forbid", "secret", "values"],
    ):
        return True
    if len(parts) >= 2 and parts[-2:] in (["proxy", "user"], ["proxy", "username"]):
        return True
    credential_terms = {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
    if any(part in credential_terms for part in parts):
        return True
    key_index = next((i for i, part in enumerate(parts) if part == "key"), None)
    return (
        key_index is not None
        and key_index > 0
        and parts[key_index - 1]
        in {
            "access",
            "api",
            "jwt",
            "private",
            "secret",
        }
    )


def _python_literal_findings(
    text: str,
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """Return literal governed assignments and strings without executing Python."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], [], False
    assignments: list[tuple[str, str]] = []
    strings: list[str] = []

    def literal_text(node: ast.expr) -> str | None:
        if not isinstance(node, ast.Constant):
            return None
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return os.fsdecode(node.value)
        return None

    def assignment_key(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
        ):
            return target.slice.value
        return None

    def pair_literals(target: ast.expr, value: ast.expr) -> None:
        key = assignment_key(target)
        if key is not None:
            decoded_value = literal_text(value)
            if decoded_value is not None:
                assignments.append((key, decoded_value))
            return
        if not isinstance(target, (ast.Tuple, ast.List)) or not isinstance(
            value, (ast.Tuple, ast.List)
        ):
            return
        if any(isinstance(element, ast.Starred) for element in target.elts):
            return
        if len(target.elts) != len(value.elts):
            return
        for child_target, child_value in zip(target.elts, value.elts, strict=True):
            pair_literals(child_target, child_value)

    def mapping_literals(node: ast.Dict) -> None:
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if not isinstance(key_node, ast.Constant):
                continue
            if isinstance(key_node.value, str):
                key = key_node.value
            elif isinstance(key_node.value, bytes):
                key = os.fsdecode(key_node.value)
            else:
                continue
            decoded_value = literal_text(value_node)
            if decoded_value is not None:
                assignments.append((key, decoded_value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                strings.append(node.value)
            elif isinstance(node.value, bytes):
                strings.append(os.fsdecode(node.value))
        if isinstance(node, ast.Dict):
            mapping_literals(node)
        if isinstance(node, ast.AugAssign):
            pair_literals(node.target, node.value)
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            pair_literals(target, node.value)
    return assignments, strings, True


def _mapping_keys_are_strings(value: object) -> bool:
    """Return false for any non-string mapping key, including through aliases."""
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, (Mapping, list)):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    return False
                pending.append(child)
        else:
            pending.extend(current)
    return True


def _unquote_assignment_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, str) else value
            except (ValueError, TypeError):
                pass
        return value[1:-1]
    return value


def _reference_scheme(value: str) -> str | None:
    if not _REFERENCE_RE.fullmatch(value):
        return None
    if value.startswith(("${", "$", "%", "env:")):
        return "env"
    if value.startswith("<"):
        return "placeholder"
    if value.startswith("{{"):
        return "template"
    return value.split(":", 1)[0].lower()


def _classify_secret_assignment(key: str, value: str) -> tuple[str | None, str | None]:
    """Return the literal diagnostic category or allowed-reference scheme."""
    normalized_key = unicodedata.normalize("NFKC", key).lower()
    if normalized_key == "authorization":
        match = _AUTHORIZATION_VALUE_RE.fullmatch(value)
        scheme = _reference_scheme(match.group("value")) if match else None
        return (
            (None, scheme)
            if scheme is not None
            else (
                "security.secret.authorization",
                None,
            )
        )
    scheme = _reference_scheme(value)
    if normalized_key in {"cookie", "set-cookie"}:
        return (
            (None, scheme)
            if scheme is not None
            else (
                "security.secret.cookie",
                None,
            )
        )
    return (
        (None, scheme)
        if scheme is not None
        else (
            "security.secret.assignment",
            None,
        )
    )


def _walk_structured(
    value: object,
    *,
    key: str | None = None,
    depth: int = 0,
    active: set[int] | None = None,
    visited: set[int] | None = None,
):
    """Yield scalar values without following recursive or excessively deep aliases."""
    if depth > _MAX_STRUCTURED_DEPTH:
        raise ValueError("structured data exceeds maximum nesting depth")
    active = active if active is not None else set()
    visited = visited if visited is not None else set()
    if isinstance(value, (Mapping, list)):
        identity = id(value)
        if identity in active:
            raise ValueError("recursive structured-data alias")
        if identity in visited:
            return
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                for child_key, child in value.items():
                    yield from _walk_structured(
                        child,
                        key=str(child_key),
                        depth=depth + 1,
                        active=active,
                        visited=visited,
                    )
            else:
                for child in value:
                    yield from _walk_structured(
                        child,
                        key=key,
                        depth=depth + 1,
                        active=active,
                        visited=visited,
                    )
        finally:
            active.remove(identity)
        visited.add(identity)
    else:
        yield key, value


def _secret_metadata_value(
    key: str,
    value: object,
    *,
    parent_key: str | None,
    relative: str,
) -> bool:
    """Recognize only the typed secret-policy metadata in manifest context."""
    if relative.casefold() != "manifest.json":
        return False
    normalized = unicodedata.normalize("NFKC", key).lower()
    normalized_parent = (
        unicodedata.normalize("NFKC", parent_key).lower() if parent_key else None
    )
    if parent_key is None:
        return (normalized == "secret_policy" and isinstance(value, Mapping)) or (
            normalized == "site_key" and isinstance(value, str)
        )
    if normalized_parent != "secret_policy":
        return False
    if normalized in {"allow_secret_references", "forbid_secret_values"}:
        return isinstance(value, bool)
    return (
        normalized == "allowed_reference_schemes"
        and isinstance(value, list)
        and all(isinstance(item, str) for item in value)
    )


def _secret_diagnostics(
    text: str,
    relative: str,
    manifest: SiteSkill | None,
    structured: object | None = None,
) -> list[Diagnostic]:
    found: set[str] = set()
    references: set[str] = set()
    for match in _ASSIGNMENT_RE.finditer(text):
        key = match.group("key")
        value = _unquote_assignment_value(match.group("value"))
        if (
            not _secret_key(key)
            or value.startswith(("{", "["))
            or (relative.casefold() == "manifest.json" and structured is not None)
        ):
            continue
        category, scheme = _classify_secret_assignment(key, value)
        if category is not None and value:
            found.add(category)
        if scheme is not None:
            references.add(scheme)
    if relative.casefold().endswith(".py"):
        assignments, _, _ = _python_literal_findings(text)
        for key, value in assignments:
            if not _secret_key(key):
                continue
            category, scheme = _classify_secret_assignment(key, value)
            if category is not None and value:
                found.add(category)
            if scheme is not None:
                references.add(scheme)

    def inspect_mapping(value: object, parent_key: str | None) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if (
                    isinstance(key, str)
                    and _secret_key(key)
                    and not _secret_metadata_value(
                        key, child, parent_key=parent_key, relative=relative
                    )
                    and child is not None
                ):
                    if isinstance(child, str):
                        category, scheme = _classify_secret_assignment(key, child)
                        if category is not None and child:
                            found.add(category)
                        if scheme is not None:
                            references.add(scheme)
                    else:
                        found.add("security.secret.assignment")

    # Traversal validates aliases/depth; mapping inspection is performed separately
    # so a credential key can classify a container value as a literal.
    list(_walk_structured(structured))
    pending = [(structured, None)]
    seen: set[int] = set()
    while pending:
        current, parent_key = pending.pop()
        if isinstance(current, (Mapping, list)):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            inspect_mapping(current, parent_key)
            if isinstance(current, Mapping):
                pending.extend((child, str(key)) for key, child in current.items())
            else:
                pending.extend((child, parent_key) for child in current)

    for match in _PROXY_URL_RE.finditer(text):
        try:
            if urlsplit(match.group(0)).username is not None:
                found.add("security.secret.proxy_userinfo")
        except ValueError:
            pass
    if _PRIVATE_KEY_RE.search(text):
        found.add("security.secret.private_key")
    contains_literal = bool(found)
    if references:
        policy = manifest.secret_policy if manifest else None
        if policy is None or not policy.allow_secret_references:
            found.add("security.secret.reference_forbidden")
        elif not references.issubset(set(policy.allowed_reference_schemes)):
            found.add("security.secret.reference_scheme")
    if contains_literal:
        found.add("security.secret_value")
    return [
        _diagnostic(code, relative, "governed text contains a credential value")
        for code in sorted(found)
    ]


def _looks_url(key: str | None, value: str) -> bool:
    return bool(
        (key and _URL_KEY_RE.search(key))
        or value.startswith("//")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
    )


def _valid_profile_url(value: str, domains: tuple[str, ...]) -> bool:
    if value != value.strip() or any(
        unicodedata.category(c).startswith("C") or c.isspace() for c in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    del port
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and host is not None
        and parsed.username is None
        and parsed.password is None
        and not any(character.isspace() for character in parsed.netloc)
        and any(
            host.lower() == domain or host.lower().endswith("." + domain)
            for domain in domains
        )
    )


def _profile_urls(
    value: Any,
    domains: tuple[str, ...],
    relative: str,
    diagnostics: list[Diagnostic],
    key: str | None = None,
) -> None:
    for child_key, child in _walk_structured(value, key=key):
        if (
            isinstance(child, str)
            and _looks_url(child_key, child)
            and not _valid_profile_url(child, domains)
        ):
            diagnostics.append(
                _diagnostic(
                    "profile.unauthorized_url",
                    relative,
                    "profile contains an invalid or unauthorized URL",
                )
            )


def _structured_absolute_path(value: object) -> bool:
    for _, child in _walk_structured(value):
        if not isinstance(child, str) or re.match(r"(?i)^https?://", child):
            continue
        if (
            child.startswith("/")
            or bool(_DRIVE_RE.match(child))
            or child.startswith("\\\\")
            or bool(_FILE_URL_RE.match(child))
        ):
            return True
    return False


def _validate_site_skill_package(
    package_path: str | Path, *, _package_fd: int | None = None
) -> dict[str, object]:
    package = Path(package_path)
    diagnostics = _DiagnosticCollector()
    files = _read_tree(package, diagnostics, package_fd=_package_fd)
    package_digest_complete = not (diagnostics.codes & _INCOMPLETE_PACKAGE_DIGEST_CODES)
    manifest: SiteSkill | None = None
    script_digests: dict[str, str] = {}
    file_map = dict(files)

    for required in ("manifest.json", "SKILL.md"):
        if not file_map.get(required):
            diagnostics.append(
                _diagnostic(
                    "package.missing_required",
                    required,
                    "required non-empty file is missing",
                )
            )
    for required_dir in ("profiles", "scripts", "tests"):
        if not any(
            name.startswith(required_dir + "/") and data for name, data in files
        ):
            diagnostics.append(
                _diagnostic(
                    "package.missing_directory_content",
                    required_dir,
                    "required directory has no non-empty files",
                )
            )

    manifest_bytes = file_map.get("manifest.json", b"")
    if manifest_bytes:
        try:
            manifest = SiteSkill.model_validate_json(manifest_bytes)
        except (ValidationError, ValueError, UnicodeError) as exc:
            diagnostics.append(
                _diagnostic(
                    "manifest.invalid",
                    "manifest.json",
                    f"manifest does not satisfy site-skill.v1: {type(exc).__name__}",
                )
            )

    if not _canonical_component(package.name) or not _canonical_component(
        package.parent.name
    ):
        diagnostics.append(
            _diagnostic(
                "package.invalid_layout_component",
                ".",
                "site_key and version directory components must be portable NFC names",
            )
        )

    referenced: set[str] = set()
    if manifest:
        if package.name != manifest.version or package.parent.name != manifest.site_key:
            diagnostics.append(
                _diagnostic(
                    "manifest.layout_mismatch",
                    "manifest.json",
                    "package directories must match manifest site_key and version",
                )
            )
        for executor in manifest.executors:
            if executor.script_path:
                referenced.add(executor.script_path)
        for recipe in manifest.recipes:
            referenced.update((recipe.profile_ref, recipe.entrypoint))
        for relative in sorted(referenced):
            if not _safe_relative(relative):
                diagnostics.append(
                    _diagnostic(
                        "reference.invalid_path",
                        relative,
                        "reference is not a canonical relative path",
                    )
                )
            elif not file_map.get(relative):
                diagnostics.append(
                    _diagnostic(
                        "reference.missing",
                        relative,
                        "declared non-empty file is missing",
                    )
                )
        for relative in sorted(
            path for path in referenced if path.endswith(".py") and path in file_map
        ):
            script_digests[relative] = _sha256(file_map[relative])

        verification_path = "tests/verification.json"
        try:
            declaration = _json_unique(file_map.get(verification_path, b""))
            implemented = (
                declaration.get("implemented_rule_ids")
                if isinstance(declaration, dict)
                else None
            )
            if (
                not isinstance(implemented, list)
                or not implemented
                or not all(isinstance(item, str) for item in implemented)
            ):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            implemented = []
            diagnostics.append(
                _diagnostic(
                    "verification.invalid_declaration",
                    verification_path,
                    "implemented rule IDs must be a non-empty string list in duplicate-key-free JSON",
                )
            )
        declared = {
            rule.rule_id
            for recipe in manifest.recipes
            for rule in recipe.verification_rules
        }
        for rule_id in sorted(declared - set(implemented)):
            diagnostics.append(
                _diagnostic(
                    "verification.not_implemented",
                    verification_path,
                    f"declared verification rule is not implemented: {rule_id}",
                )
            )

        for relative, data in files:
            suffix = PurePosixPath(relative).suffix.casefold()
            if relative.startswith("profiles/") and data:
                try:
                    profile = (
                        _json_unique(data)
                        if suffix == ".json"
                        else yaml.load(data, Loader=_UniqueKeyLoader)
                    )
                except (
                    ValueError,
                    TypeError,
                    UnicodeError,
                    RecursionError,
                    yaml.YAMLError,
                ):
                    diagnostics.append(
                        _diagnostic(
                            "profile.invalid",
                            relative,
                            "profile must be duplicate-key-free UTF-8 JSON/YAML",
                        )
                    )
                    continue
                if not isinstance(profile, Mapping):
                    diagnostics.append(
                        _diagnostic(
                            "profile.invalid", relative, "profile must be a mapping"
                        )
                    )
                    continue
                try:
                    list(_walk_structured(profile))
                except ValueError:
                    diagnostics.append(
                        _diagnostic(
                            "profile.invalid",
                            relative,
                            "profile contains recursive aliases or excessive nesting",
                        )
                    )
                    continue
                domains = profile.get("allowed_domains")
                valid_domains = (
                    isinstance(domains, list)
                    and bool(domains)
                    and all(isinstance(item, str) for item in domains)
                    and len(domains) == len(set(domains))
                    and set(domains).issubset(manifest.allowed_domains)
                )
                if not valid_domains:
                    diagnostics.append(
                        _diagnostic(
                            "profile.domain_mismatch",
                            relative,
                            "profile allowed_domains must be a non-empty subset of manifest domains",
                        )
                    )
                _profile_urls(profile, manifest.allowed_domains, relative, diagnostics)

    for relative, data in files:
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix in _GOVERNED_SUFFIXES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                diagnostics.append(
                    _diagnostic(
                        "text.invalid_utf8",
                        relative,
                        "governed text must be valid UTF-8",
                    )
                )
                continue
            structured: object | None = None
            structured_parse_failed = False
            try:
                if suffix == ".json":
                    structured = _json_unique(data)
                elif suffix in {".yaml", ".yml"}:
                    structured = yaml.load(data, Loader=_UniqueKeyLoader)
            except (
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
                yaml.YAMLError,
            ):
                structured_parse_failed = True
                diagnostics.append(
                    _diagnostic(
                        "structured.invalid",
                        relative,
                        "structured data must be duplicate-key-free valid JSON/YAML",
                    )
                )
            structured_safe = not structured_parse_failed
            try:
                if structured_safe:
                    list(_walk_structured(structured))
            except ValueError:
                structured_safe = False
                diagnostics.append(
                    _diagnostic(
                        "structured.invalid",
                        relative,
                        "structured data contains recursive aliases or excessive nesting",
                    )
                )
            if structured_safe and not _mapping_keys_are_strings(structured):
                structured_safe = False
                diagnostics.append(
                    _diagnostic(
                        "structured.invalid_key",
                        relative,
                        "structured mapping keys must be strings",
                    )
                )
            if structured_safe:
                diagnostics.extend(
                    _secret_diagnostics(text, relative, manifest, structured)
                )
            path_text = re.sub(
                r"(?i)\b(?!file:)[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+",
                "",
                text,
            )
            raw_absolute_path = any(
                pattern.search(path_text)
                for pattern in (
                    _POSIX_ABSOLUTE_RE,
                    _WINDOWS_ABSOLUTE_RE,
                    _UNC_RE,
                    _FORWARD_UNC_RE,
                    _FILE_URL_RE,
                )
            )
            decoded_absolute_path = False
            if structured_safe:
                decoded_absolute_path = _structured_absolute_path(structured)
            if suffix == ".py":
                _, python_strings, python_valid = _python_literal_findings(text)
                if not python_valid:
                    diagnostics.append(
                        _diagnostic(
                            "python.invalid",
                            relative,
                            "governed Python must be syntactically valid",
                        )
                    )
                decoded_absolute_path = (
                    decoded_absolute_path or _structured_absolute_path(python_strings)
                )
            if raw_absolute_path or decoded_absolute_path:
                diagnostics.append(
                    _diagnostic(
                        "security.absolute_path",
                        relative,
                        "governed text contains an absolute filesystem path",
                    )
                )

    diagnostics.sort(key=lambda item: (item.code, item.path, item.message))
    return {
        "path": str(package),
        "valid": not diagnostics,
        "site_key": manifest.site_key if manifest else None,
        "version": manifest.version if manifest else None,
        "skill_id": manifest.skill_id if manifest else None,
        "manifest_sha256": _sha256(manifest_bytes) if manifest_bytes else None,
        "package_digest_algorithm": "sha256:web-listening.site-skill-package.v1",
        "package_sha256": _package_digest(files) if package_digest_complete else None,
        "script_sha256": dict(sorted(script_digests.items())),
        "diagnostics": [item.to_dict() for item in diagnostics],
    }


def validate_site_skill_package(
    package_path: str | Path, *, _package_fd: int | None = None
) -> dict[str, object]:
    package = Path(package_path)
    try:
        return _validate_site_skill_package(package, _package_fd=_package_fd)
    except MemoryError:
        return _registry_failure(
            package,
            "package.resource_exhausted",
            "package validation exceeded available memory",
        )


def _registry_failure(path: Path, code: str, message: str) -> dict[str, object]:
    return {
        "path": str(path),
        "valid": False,
        "site_key": None,
        "version": None,
        "skill_id": None,
        "manifest_sha256": None,
        "package_digest_algorithm": "sha256:web-listening.site-skill-package.v1",
        "package_sha256": None,
        "script_sha256": {},
        "diagnostics": [_diagnostic(code, ".", message).to_dict()],
    }


def list_site_skills(root: str | Path | None = None) -> list[dict[str, object]]:
    registry = Path(root) if root is not None else default_registry_root()
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        registry_fd = os.open(registry, flags)
    except OSError:
        if root is None:
            try:
                registry.lstat()
            except FileNotFoundError:
                return []
            except OSError:
                pass
        return [
            _registry_failure(
                registry,
                "registry.invalid_root",
                "explicit registry root must be an existing, readable, real directory",
            )
        ]
    results: list[dict[str, object]] = []

    def bounded_names(directory_fd: int) -> list[str]:
        names: list[str] = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if len(names) >= _MAX_REGISTRY_ENTRIES:
                    raise OverflowError
                names.append(entry.name)
        names.sort()
        return names

    try:
        try:
            site_names = bounded_names(registry_fd)
        except OverflowError:
            return [
                _registry_failure(
                    registry,
                    "registry.entry_count_limit",
                    f"registry contains more than {_MAX_REGISTRY_ENTRIES} entries",
                )
            ]
        except MemoryError:
            return [
                _registry_failure(
                    registry,
                    "registry.resource_exhausted",
                    "registry enumeration exceeded available memory",
                )
            ]
        for site_name in site_names:
            site_path = registry / site_name
            try:
                site_fd = os.open(site_name, flags, dir_fd=registry_fd)
            except OSError:
                results.append(
                    _registry_failure(
                        site_path,
                        "site.not_directory",
                        "site candidate must be a readable, real directory",
                    )
                )
                continue
            try:
                try:
                    package_names = bounded_names(site_fd)
                except OverflowError:
                    results.append(
                        _registry_failure(
                            site_path,
                            "site.entry_count_limit",
                            f"site contains more than {_MAX_REGISTRY_ENTRIES} entries",
                        )
                    )
                    continue
                except MemoryError:
                    results.append(
                        _registry_failure(
                            site_path,
                            "site.resource_exhausted",
                            "site enumeration exceeded available memory",
                        )
                    )
                    continue
                except OSError:
                    results.append(
                        _registry_failure(
                            site_path,
                            "site.not_directory",
                            "site candidate must be a readable, real directory",
                        )
                    )
                    continue
                for package_name in package_names:
                    package_path = site_path / package_name
                    try:
                        package_fd = os.open(package_name, flags, dir_fd=site_fd)
                    except OSError:
                        results.append(
                            _registry_failure(
                                package_path,
                                "package.not_directory",
                                "package must be a readable, real directory",
                            )
                        )
                        continue
                    try:
                        results.append(
                            validate_site_skill_package(
                                package_path, _package_fd=package_fd
                            )
                        )
                    finally:
                        os.close(package_fd)
            finally:
                os.close(site_fd)
    except MemoryError:
        return [
            _registry_failure(
                registry,
                "registry.resource_exhausted",
                "registry enumeration exceeded available memory",
            )
        ]
    except OSError:
        return [
            _registry_failure(
                registry,
                "registry.invalid_root",
                "registry root cannot be inspected",
            )
        ]
    finally:
        os.close(registry_fd)
    return results


def resolve_site_skill(
    *,
    site_key: str,
    version: str,
    package_sha256: str,
    root: str | Path | None = None,
    _registry_snapshot: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if not _canonical_component(site_key):
        raise ValueError("site_key must be a portable NFC directory component")
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("version must be canonical MAJOR.MINOR.PATCH")
    if not _SHA256_RE.fullmatch(package_sha256):
        raise ValueError("package digest must be exact lowercase SHA-256")
    matches = [
        item
        for item in (
            _registry_snapshot
            if _registry_snapshot is not None
            else list_site_skills(root)
        )
        if item["site_key"] == site_key
        and item["version"] == version
        and item["package_sha256"] == package_sha256
    ]
    if len(matches) != 1 or matches[0]["valid"] is not True:
        raise LookupError(f"exact Site Skill resolution found {len(matches)} matches")
    return matches[0]


__all__ = [
    "default_registry_root",
    "list_site_skills",
    "resolve_site_skill",
    "validate_site_skill_package",
]
