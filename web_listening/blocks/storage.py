import json
import base64
import binascii
import hashlib
import os
import sqlite3
import stat
import re
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import FunctionType
from typing import Final, List, Optional

from web_listening.models import (
    AnalysisReport,
    AcquisitionArtifact,
    AcquisitionAttempt,
    Change,
    CrawlRun,
    CrawlScope,
    Document,
    FileObservation,
    Job,
    PageEdge,
    PageSnapshot,
    Site,
    SiteSnapshot,
    TrackedFile,
    TrackedPage,
)
from web_listening.blocks.acquisition_gateway import redact_persisted_value
from web_listening.blocks.diff import compute_hash
from web_listening.contracts import AcquisitionAttempt as ContractAcquisitionAttempt
from web_listening.contracts._protocol import validate_portable_relative_path


_EXECUTION_ARTIFACT_CLEANUP_LOCK = threading.RLock()
_CROSS_THREAD_TRANSACTION_HANDOFF_METHODS = frozenset(
    {"rollback_execution_transaction"}
)


def _serialize_public_storage_operations(cls):
    """Give every public operation one owner-aware turn on a Storage instance."""

    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or not isinstance(method, FunctionType):
            continue

        @wraps(method)
        def serialized(self, *args, __method=method, **kwargs):
            condition = getattr(self, "_execution_transaction_condition", None)
            if condition is None:
                return __method(self, *args, **kwargs)
            with condition:
                thread_id = threading.get_ident()
                while (
                    self._execution_transaction_depth > 0
                    and self._execution_transaction_owner != thread_id
                    and __method.__name__
                    not in _CROSS_THREAD_TRANSACTION_HANDOFF_METHODS
                ):
                    condition.wait()
                return __method(self, *args, **kwargs)

        setattr(cls, name, serialized)
    return cls


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    from dateutil import parser as dtparser

    return dtparser.parse(value)


class ExecutionArtifactOwnershipError(ValueError):
    """A published path no longer names the inode offered for journaling."""


class ExecutionArtifactRollbackError(RuntimeError):
    """An owned name could not be safely removed or restored from quarantine."""


@dataclass(slots=True)
class _ExecutionCreatedPathOwnership:
    path: Path
    cleanup_root: Path
    descriptor: int


@dataclass(slots=True)
class _ExecutionCreatedDirectoryOwnership:
    path: Path
    cleanup_root: Path
    descriptor: int


@_serialize_public_storage_operations
class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._execution_transaction_depth = 0
        self._execution_transaction_owner: int | None = None
        self._execution_transaction_condition = threading.Condition(threading.RLock())
        self._execution_created_paths: list[_ExecutionCreatedPathOwnership] = []
        self._execution_created_directories: list[
            _ExecutionCreatedDirectoryOwnership
        ] = []
        self._execution_cleanup_lock = _EXECUTION_ARTIFACT_CLEANUP_LOCK
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    @property
    def execution_transaction_active(self) -> bool:
        with self._execution_transaction_condition:
            return self._execution_transaction_depth > 0

    @property
    def execution_transaction_owned_by_current_thread(self) -> bool:
        with self._execution_transaction_condition:
            return (
                self._execution_transaction_depth > 0
                and self._execution_transaction_owner == threading.get_ident()
            )

    def begin_execution_transaction(self) -> None:
        """Defer all execution writes until the governed traversal succeeds."""
        thread_id = threading.get_ident()
        with self._execution_transaction_condition:
            while (
                self._execution_transaction_depth > 0
                and self._execution_transaction_owner != thread_id
            ):
                self._execution_transaction_condition.wait()
            if self._execution_transaction_depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
                self._execution_transaction_owner = thread_id
            self._execution_transaction_depth += 1

    def commit_execution_transaction(self) -> None:
        thread_id = threading.get_ident()
        with self._execution_transaction_condition:
            if self._execution_transaction_depth == 0:
                return
            if self._execution_transaction_owner != thread_id:
                raise RuntimeError("execution transaction is owned by another thread")
            if self._execution_transaction_depth > 1:
                self._execution_transaction_depth -= 1
                return
            try:
                self.conn.commit()
            except BaseException:
                try:
                    transaction_pending = self.conn.in_transaction
                except BaseException:
                    transaction_pending = True
                if transaction_pending:
                    try:
                        self.rollback_execution_transaction()
                    except BaseException:
                        pass
                else:
                    self._execution_transaction_depth = 0
                    try:
                        self._release_execution_created_paths()
                    except BaseException:
                        pass
                    finally:
                        self._execution_transaction_owner = None
                        self._execution_transaction_condition.notify_all()
                raise
            else:
                self._execution_transaction_depth = 0
                try:
                    self._release_execution_created_paths()
                finally:
                    self._execution_transaction_owner = None
                    self._execution_transaction_condition.notify_all()

    def register_execution_created_path(
        self,
        path: str | Path,
        *,
        cleanup_root: str | Path,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        """Journal one exact no-follow leaf for access-reject rollback."""
        if not self.execution_transaction_active:
            return
        try:
            anchored_path, anchored_root = self._anchored_execution_path(
                path, cleanup_root
            )
        except ValueError as exc:
            raise ExecutionArtifactOwnershipError(str(exc)) from exc
        descriptor: int | None = None
        try:
            descriptor, opened = self._open_anchored_execution_path(
                anchored_path, anchored_root
            )
        except (OSError, ValueError) as exc:
            raise ExecutionArtifactOwnershipError(
                "execution artifact identity cannot be verified"
            ) from exc
        ownership_failure = False
        try:
            if not stat.S_ISREG(opened.st_mode):
                raise ExecutionArtifactOwnershipError(
                    "execution artifact must be a regular no-follow leaf"
                )
            actual_identity = (opened.st_dev, opened.st_ino)
            if expected_identity is not None and actual_identity != expected_identity:
                raise ExecutionArtifactOwnershipError(
                    "execution artifact identity mismatch before journal handoff"
                )
            self._execution_created_paths.append(
                _ExecutionCreatedPathOwnership(
                    path=anchored_path,
                    cleanup_root=anchored_root,
                    descriptor=descriptor,
                )
            )
            descriptor = None
        except BaseException:
            ownership_failure = True
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    if not ownership_failure:
                        raise

    def ensure_execution_artifact_directory(
        self,
        path: str | Path,
        *,
        cleanup_root: str | Path,
    ) -> None:
        """Create and journal only directories first created by this execution."""
        if not self.execution_transaction_active:
            Path(path).mkdir(parents=True, exist_ok=True)
            return
        try:
            anchored_path, anchored_root = self._anchored_execution_path(
                path, cleanup_root
            )
        except ValueError as exc:
            raise ExecutionArtifactOwnershipError(str(exc)) from exc
        with self._execution_cleanup_lock:
            anchored_root.mkdir(parents=True, exist_ok=True)
            supports_secure_dir_fd = all(
                operation in os.supports_dir_fd
                for operation in (os.open, os.stat, os.mkdir, os.rmdir)
            )
            if supports_secure_dir_fd:
                self._ensure_execution_artifact_directory_secure(
                    anchored_path, anchored_root
                )
            else:
                self._ensure_execution_artifact_directory_fallback(
                    anchored_path, anchored_root
                )

    def _ensure_execution_artifact_directory_fallback(
        self, path: Path, cleanup_root: Path
    ) -> None:
        root_info = cleanup_root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or cleanup_root.is_symlink():
            raise ExecutionArtifactOwnershipError(
                "execution cleanup root must be a real directory"
            )
        current = cleanup_root
        for component in path.relative_to(cleanup_root).parts:
            current /= component
            created = False
            created_identity: tuple[int, int] | None = None
            descriptor: int | None = None
            ownership_transferred = False
            operation_failure = False
            try:
                try:
                    named = current.lstat()
                except FileNotFoundError:
                    current.mkdir(mode=0o700)
                    named = current.lstat()
                    created = True
                    created_identity = (named.st_dev, named.st_ino)
                if not stat.S_ISDIR(named.st_mode) or current.is_symlink():
                    raise ExecutionArtifactOwnershipError(
                        "execution artifact parent must be a no-follow directory"
                    )
                descriptor = self._open_execution_directory_descriptor(current)
                opened = os.fstat(descriptor)
                named_after = current.lstat()
                opened_identity = (opened.st_dev, opened.st_ino)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(named_after.st_mode)
                    or (named_after.st_dev, named_after.st_ino) != opened_identity
                ):
                    raise ExecutionArtifactOwnershipError(
                        "execution artifact directory changed during creation"
                    )
                if created:
                    self._execution_created_directories.append(
                        _ExecutionCreatedDirectoryOwnership(
                            path=current,
                            cleanup_root=cleanup_root,
                            descriptor=descriptor,
                        )
                    )
                    descriptor = None
                    ownership_transferred = True
            except BaseException:
                operation_failure = True
                if (
                    created
                    and not ownership_transferred
                    and created_identity is not None
                ):
                    self._rmdir_path_if_identity(current, created_identity)
                raise
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException:
                        if not operation_failure:
                            raise

    def _ensure_execution_artifact_directory_secure(
        self, path: Path, cleanup_root: Path
    ) -> None:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            parent_fd = os.open(cleanup_root, directory_flags)
            descriptors.append(parent_fd)
            current = cleanup_root
            for component in path.relative_to(cleanup_root).parts:
                current /= component
                created = False
                created_identity: tuple[int, int] | None = None
                ownership_descriptor: int | None = None
                try:
                    try:
                        child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                    except FileNotFoundError:
                        os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                        created = True
                        named_created = os.stat(
                            component, dir_fd=parent_fd, follow_symlinks=False
                        )
                        created_identity = (
                            named_created.st_dev,
                            named_created.st_ino,
                        )
                        child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                    descriptors.append(child_fd)
                    opened = os.fstat(child_fd)
                    named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                    opened_identity = (opened.st_dev, opened.st_ino)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or not stat.S_ISDIR(named.st_mode)
                        or (named.st_dev, named.st_ino) != opened_identity
                    ):
                        raise ExecutionArtifactOwnershipError(
                            "execution artifact directory changed during creation"
                        )
                    if created:
                        ownership_descriptor = os.dup(child_fd)
                        self._execution_created_directories.append(
                            _ExecutionCreatedDirectoryOwnership(
                                path=current,
                                cleanup_root=cleanup_root,
                                descriptor=ownership_descriptor,
                            )
                        )
                        ownership_descriptor = None
                    parent_fd = child_fd
                except BaseException:
                    if ownership_descriptor is not None:
                        try:
                            os.close(ownership_descriptor)
                        except BaseException:
                            pass
                    if created and created_identity is not None:
                        self._rmdir_at_if_identity(
                            parent_fd,
                            component,
                            created_identity,
                        )
                    raise
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except BaseException:
                    pass

    @staticmethod
    def _open_execution_directory_descriptor(path: Path) -> int:
        if os.name == "nt":
            return Storage._open_windows_shared_execution_directory(path)
        return os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    @staticmethod
    def _open_windows_shared_execution_directory(path: Path) -> int:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            os.fspath(path),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise

    def _register_open_execution_created_directory(
        self,
        path: str | Path,
        *,
        cleanup_root: str | Path,
        source_descriptor: int,
        expected_identity: tuple[int, int],
    ) -> None:
        if not self.execution_transaction_active:
            return
        try:
            anchored_path, anchored_root = self._anchored_execution_path(
                path, cleanup_root
            )
        except ValueError as exc:
            raise ExecutionArtifactOwnershipError(str(exc)) from exc
        descriptor: int | None = None
        failure_active = False
        try:
            descriptor = os.dup(source_descriptor)
            opened = os.fstat(descriptor)
            named = self._lstat_anchored_execution_path(anchored_path, anchored_root)
            actual_identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or actual_identity != expected_identity
                or (named.st_dev, named.st_ino) != expected_identity
            ):
                raise ExecutionArtifactOwnershipError(
                    "execution artifact directory identity mismatch before journal handoff"
                )
            self._execution_created_directories.append(
                _ExecutionCreatedDirectoryOwnership(
                    path=anchored_path,
                    cleanup_root=anchored_root,
                    descriptor=descriptor,
                )
            )
            descriptor = None
        except BaseException:
            failure_active = True
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    if not failure_active:
                        raise

    @staticmethod
    def _rmdir_path_if_identity(path: Path, expected_identity: tuple[int, int]) -> bool:
        try:
            named = path.lstat()
            if (
                not stat.S_ISDIR(named.st_mode)
                or path.is_symlink()
                or (named.st_dev, named.st_ino) != expected_identity
            ):
                return False
            path.rmdir()
            return True
        except OSError:
            return False

    @staticmethod
    def _rmdir_at_if_identity(
        parent_fd: int, target: str, expected_identity: tuple[int, int]
    ) -> bool:
        try:
            named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != expected_identity
            ):
                return False
            os.rmdir(target, dir_fd=parent_fd)
            return True
        except OSError:
            return False

    @staticmethod
    def _anchored_execution_path(
        path: str | Path, cleanup_root: str | Path
    ) -> tuple[Path, Path]:
        supplied_path = Path(path)
        if ".." in supplied_path.parts:
            raise ValueError("execution artifact path traversal is not allowed")
        anchored_path = Path(os.path.abspath(os.fspath(supplied_path)))
        anchored_root = Path(os.path.abspath(os.fspath(Path(cleanup_root))))
        try:
            relative = anchored_path.relative_to(anchored_root)
        except ValueError as exc:
            raise ValueError(
                "execution artifact must stay under its cleanup root"
            ) from exc
        if not relative.parts:
            raise ValueError("execution artifact must name a leaf below cleanup root")
        return anchored_path, anchored_root

    @staticmethod
    def _lstat_anchored_execution_path(path: Path, cleanup_root: Path):
        relative = path.relative_to(cleanup_root)
        supports_secure_dir_fd = all(
            operation in os.supports_dir_fd for operation in (os.open, os.stat)
        )
        if not supports_secure_dir_fd:
            current = cleanup_root
            root_info = current.lstat()
            if not stat.S_ISDIR(root_info.st_mode) or current.is_symlink():
                raise ValueError("execution cleanup root must be a real directory")
            for component in relative.parts[:-1]:
                current /= component
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode) or current.is_symlink():
                    raise ValueError(
                        "execution artifact parent must be a no-follow directory"
                    )
            return path.lstat()

        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            parent_fd = os.open(cleanup_root, directory_flags)
            descriptors.append(parent_fd)
            for component in relative.parts[:-1]:
                parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                descriptors.append(parent_fd)
            return os.stat(relative.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _open_anchored_execution_path(path: Path, cleanup_root: Path):
        """Open and pin one anchored no-follow leaf until transaction completion."""
        relative = path.relative_to(cleanup_root)
        supports_secure_dir_fd = all(
            operation in os.supports_dir_fd for operation in (os.open, os.stat)
        )
        leaf_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if not supports_secure_dir_fd:
            named_before = Storage._lstat_anchored_execution_path(path, cleanup_root)
            if not stat.S_ISREG(named_before.st_mode):
                raise ValueError("execution artifact must be a regular no-follow leaf")
            if os.name == "nt":
                descriptor = Storage._open_windows_shared_execution_leaf(path)
            else:
                descriptor = os.open(path, leaf_flags)
            try:
                opened = os.fstat(descriptor)
                named_after = Storage._lstat_anchored_execution_path(path, cleanup_root)
                opened_identity = (opened.st_dev, opened.st_ino)
                if (
                    not stat.S_ISREG(named_before.st_mode)
                    or not stat.S_ISREG(named_after.st_mode)
                    or (named_before.st_dev, named_before.st_ino) != opened_identity
                    or (named_after.st_dev, named_after.st_ino) != opened_identity
                ):
                    raise ValueError(
                        "execution artifact changed while opening journal ownership"
                    )
                return descriptor, opened
            except BaseException:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
                raise

        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_descriptors: list[int] = []
        leaf_descriptor: int | None = None
        try:
            parent_fd = os.open(cleanup_root, directory_flags)
            directory_descriptors.append(parent_fd)
            for component in relative.parts[:-1]:
                parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                directory_descriptors.append(parent_fd)
            leaf_name = relative.parts[-1]
            leaf_descriptor = os.open(leaf_name, leaf_flags, dir_fd=parent_fd)
            opened = os.fstat(leaf_descriptor)
            named = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
            opened_identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or (named.st_dev, named.st_ino) != opened_identity
            ):
                raise ValueError(
                    "execution artifact changed while opening journal ownership"
                )
        except BaseException:
            if leaf_descriptor is not None:
                try:
                    os.close(leaf_descriptor)
                except BaseException:
                    pass
            raise
        finally:
            for directory_descriptor in reversed(directory_descriptors):
                try:
                    os.close(directory_descriptor)
                except BaseException:
                    pass
        return leaf_descriptor, opened

    @staticmethod
    def _open_windows_shared_execution_leaf(path: Path) -> int:
        """Open a no-follow Windows handle that still permits identity-safe delete."""
        import ctypes
        from ctypes import wintypes
        import msvcrt

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            os.fspath(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise

    def _release_execution_created_paths(self) -> None:
        ownerships = self._execution_created_paths
        directory_ownerships = self._execution_created_directories
        self._execution_created_paths = []
        self._execution_created_directories = []
        close_failure: BaseException | None = None
        for ownership in reversed((*ownerships, *directory_ownerships)):
            try:
                os.close(ownership.descriptor)
            except BaseException as exc:
                close_failure = close_failure or exc
        if close_failure is not None:
            raise close_failure

    def remove_execution_created_path_if_identity(
        self,
        path: str | Path,
        *,
        cleanup_root: str | Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        anchored_path, anchored_root = self._anchored_execution_path(path, cleanup_root)
        relative = anchored_path.relative_to(anchored_root)
        supports_secure_dir_fd = all(
            operation in os.supports_dir_fd
            for operation in (
                os.open,
                os.stat,
                os.unlink,
                os.rename,
                os.mkdir,
                os.rmdir,
                os.link,
            )
        )
        with self._execution_cleanup_lock:
            if not supports_secure_dir_fd:
                return self._quarantine_execution_path_fallback(
                    anchored_path,
                    anchored_root,
                    expected_identity,
                )
            return self._quarantine_execution_path_secure(
                anchored_root,
                relative,
                expected_identity,
            )

    @staticmethod
    def _quarantine_name() -> str:
        return f".web-listening-rollback-{secrets.token_hex(16)}"

    def _quarantine_execution_path_fallback(
        self,
        path: Path,
        cleanup_root: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        try:
            self._lstat_anchored_execution_path(path, cleanup_root)
        except (OSError, ValueError):
            return False
        quarantine = path.parent / self._quarantine_name()
        quarantine.mkdir(mode=0o700)
        quarantine_info = quarantine.lstat()
        if not stat.S_ISDIR(quarantine_info.st_mode) or quarantine.is_symlink():
            raise ExecutionArtifactRollbackError(
                "execution rollback quarantine must be a no-follow directory"
            )
        candidate = quarantine / "candidate"
        moved = False
        candidate_descriptor: int | None = None
        operation_failure = False
        try:
            try:
                os.rename(path, candidate)
            except FileNotFoundError:
                quarantine.rmdir()
                return False
            moved = True
            try:
                candidate_info = candidate.lstat()
            except OSError as exc:
                raise ExecutionArtifactRollbackError(
                    "execution rollback quarantine cannot be verified"
                ) from exc
            matches = False
            if stat.S_ISREG(candidate_info.st_mode):
                try:
                    candidate_descriptor, opened = self._open_anchored_execution_path(
                        candidate, cleanup_root
                    )
                except (OSError, ValueError):
                    matches = False
                else:
                    matches = (opened.st_dev, opened.st_ino) == expected_identity
            if matches:
                candidate.unlink()
                moved = False
                quarantine.rmdir()
                return True
            try:
                if os.name == "nt":
                    os.rename(candidate, path)
                else:
                    os.link(candidate, path, follow_symlinks=False)
                    candidate.unlink()
            except FileExistsError as exc:
                raise ExecutionArtifactRollbackError(
                    f"execution rollback restore collision; quarantine retained at {quarantine}"
                ) from exc
            except OSError as exc:
                raise ExecutionArtifactRollbackError(
                    f"execution rollback restore failed; quarantine retained at {quarantine}"
                ) from exc
            moved = False
            quarantine.rmdir()
            return False
        except BaseException:
            operation_failure = True
            if moved and not path.exists():
                try:
                    if os.name == "nt":
                        os.rename(candidate, path)
                    else:
                        os.link(candidate, path, follow_symlinks=False)
                        candidate.unlink()
                    moved = False
                    quarantine.rmdir()
                except BaseException:
                    pass
            raise
        finally:
            if candidate_descriptor is not None:
                try:
                    os.close(candidate_descriptor)
                except BaseException:
                    if not operation_failure:
                        raise

    def _quarantine_execution_path_secure(
        self,
        cleanup_root: Path,
        relative: Path,
        expected_identity: tuple[int, int],
    ) -> bool:

        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        candidate_descriptor: int | None = None
        quarantine_name = self._quarantine_name()
        moved = False
        operation_failure = False
        try:
            parent_fd = os.open(cleanup_root, directory_flags)
            descriptors.append(parent_fd)
            for component in relative.parts[:-1]:
                parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                descriptors.append(parent_fd)
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
            quarantine_fd = os.open(
                quarantine_name,
                directory_flags,
                dir_fd=parent_fd,
            )
            descriptors.append(quarantine_fd)
            quarantine_named = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            quarantine_opened = os.fstat(quarantine_fd)
            if not stat.S_ISDIR(quarantine_named.st_mode) or (
                quarantine_named.st_dev,
                quarantine_named.st_ino,
            ) != (quarantine_opened.st_dev, quarantine_opened.st_ino):
                raise ExecutionArtifactRollbackError(
                    "execution rollback quarantine directory identity changed"
                )
            leaf_name = relative.parts[-1]
            try:
                os.rename(
                    leaf_name,
                    "candidate",
                    src_dir_fd=parent_fd,
                    dst_dir_fd=quarantine_fd,
                )
            except FileNotFoundError:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
                return False
            moved = True
            candidate_info = os.stat(
                "candidate", dir_fd=quarantine_fd, follow_symlinks=False
            )
            matches = False
            if stat.S_ISREG(candidate_info.st_mode):
                leaf_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                candidate_descriptor = os.open(
                    "candidate", leaf_flags, dir_fd=quarantine_fd
                )
                opened = os.fstat(candidate_descriptor)
                named = os.stat(
                    "candidate", dir_fd=quarantine_fd, follow_symlinks=False
                )
                matches = (
                    stat.S_ISREG(opened.st_mode)
                    and (opened.st_dev, opened.st_ino)
                    == (named.st_dev, named.st_ino)
                    == expected_identity
                )
            if matches:
                os.unlink("candidate", dir_fd=quarantine_fd)
                moved = False
                os.rmdir(quarantine_name, dir_fd=parent_fd)
                return True
            try:
                os.link(
                    "candidate",
                    leaf_name,
                    src_dir_fd=quarantine_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.unlink("candidate", dir_fd=quarantine_fd)
            except FileExistsError as exc:
                raise ExecutionArtifactRollbackError(
                    f"execution rollback restore collision; quarantine retained at {relative.parent / quarantine_name}"
                ) from exc
            except OSError as exc:
                raise ExecutionArtifactRollbackError(
                    f"execution rollback restore failed; quarantine retained at {relative.parent / quarantine_name}"
                ) from exc
            moved = False
            os.rmdir(quarantine_name, dir_fd=parent_fd)
            return False
        except BaseException:
            operation_failure = True
            if moved:
                try:
                    os.link(
                        "candidate",
                        relative.parts[-1],
                        src_dir_fd=descriptors[-1],
                        dst_dir_fd=descriptors[-2],
                        follow_symlinks=False,
                    )
                    os.unlink("candidate", dir_fd=descriptors[-1])
                    moved = False
                    os.rmdir(quarantine_name, dir_fd=descriptors[-2])
                except BaseException:
                    pass
            raise
        finally:
            close_failure: BaseException | None = None
            if candidate_descriptor is not None:
                try:
                    os.close(candidate_descriptor)
                except BaseException as exc:
                    close_failure = exc
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    close_failure = close_failure or exc
            if close_failure is not None and not operation_failure:
                raise close_failure

    def _remove_execution_created_directory_if_identity(
        self,
        path: Path,
        *,
        cleanup_root: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        anchored_path, anchored_root = self._anchored_execution_path(path, cleanup_root)
        relative = anchored_path.relative_to(anchored_root)
        supports_secure_dir_fd = all(
            operation in os.supports_dir_fd
            for operation in (os.open, os.stat, os.rename, os.rmdir)
        )
        with self._execution_cleanup_lock:
            if not supports_secure_dir_fd:
                return self._quarantine_execution_directory_fallback(
                    anchored_path,
                    anchored_root,
                    expected_identity,
                )
            return self._quarantine_execution_directory_secure(
                anchored_root,
                relative,
                expected_identity,
            )

    @staticmethod
    def _rename_directory_no_replace(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        """Atomically move a directory without replacing an existing name."""
        if os.name == "nt":
            if src_dir_fd is not None or dst_dir_fd is not None:
                raise OSError("directory-fd rename is unavailable on Windows")
            os.rename(source, destination)
            return

        import ctypes
        import errno

        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory rename is unavailable",
            ) from exc
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        at_fdcwd = -100
        result = renameat2(
            at_fdcwd if src_dir_fd is None else src_dir_fd,
            os.fsencode(source),
            at_fdcwd if dst_dir_fd is None else dst_dir_fd,
            os.fsencode(destination),
            1,  # RENAME_NOREPLACE
        )
        if result:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    @staticmethod
    def _directory_identity_matches(
        named,
        opened,
        expected_identity: tuple[int, int],
    ) -> bool:
        return (
            stat.S_ISDIR(named.st_mode)
            and stat.S_ISDIR(opened.st_mode)
            and (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
            == expected_identity
        )

    def _quarantine_execution_directory_fallback(
        self,
        path: Path,
        cleanup_root: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        try:
            named = self._lstat_anchored_execution_path(path, cleanup_root)
        except (OSError, ValueError):
            return False
        if not stat.S_ISDIR(named.st_mode) or path.is_symlink():
            return False
        try:
            if any(path.iterdir()):
                return False
        except OSError:
            return False
        quarantine = path.parent / self._quarantine_name()
        quarantine_descriptor: int | None = None
        moved = False
        operation_failure = False
        try:
            try:
                self._rename_directory_no_replace(path, quarantine)
            except BaseException as exc:
                effected = self._restore_uncertain_directory_quarantine_fallback(
                    quarantine,
                    path,
                    expected_identity,
                )
                if isinstance(exc, FileNotFoundError) and not effected:
                    return False
                if isinstance(exc, FileExistsError) and not effected:
                    raise ExecutionArtifactRollbackError(
                        f"execution directory rollback quarantine collision at {quarantine}"
                    ) from exc
                raise
            moved = True
            quarantine_named = quarantine.lstat()
            quarantine_descriptor = self._open_execution_directory_descriptor(
                quarantine
            )
            quarantine_opened = os.fstat(quarantine_descriptor)
            matches = (
                self._directory_identity_matches(
                    quarantine_named,
                    quarantine_opened,
                    expected_identity,
                )
                and not quarantine.is_symlink()
            )
            if matches:
                try:
                    quarantine.rmdir()
                except OSError:
                    self._restore_quarantined_directory_fallback(
                        quarantine,
                        path,
                    )
                    moved = False
                    return False
                moved = False
                return True
            self._restore_quarantined_directory_fallback(
                quarantine,
                path,
            )
            moved = False
            return False
        except BaseException:
            operation_failure = True
            if moved:
                try:
                    self._restore_quarantined_directory_fallback(
                        quarantine,
                        path,
                    )
                    moved = False
                except BaseException:
                    pass
            raise
        finally:
            if quarantine_descriptor is not None:
                try:
                    os.close(quarantine_descriptor)
                except BaseException:
                    if not operation_failure:
                        raise

    def _restore_uncertain_directory_quarantine_fallback(
        self,
        quarantine: Path,
        path: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        descriptor: int | None = None
        try:
            named = quarantine.lstat()
            descriptor = self._open_execution_directory_descriptor(quarantine)
            opened = os.fstat(descriptor)
            if (
                not self._directory_identity_matches(
                    named,
                    opened,
                    expected_identity,
                )
                or quarantine.is_symlink()
            ):
                return False
            try:
                self._rename_directory_no_replace(quarantine, path)
            except BaseException:
                pass
            return True
        except BaseException:
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass

    def _restore_quarantined_directory_fallback(
        self,
        quarantine: Path,
        path: Path,
    ) -> None:
        try:
            self._rename_directory_no_replace(quarantine, path)
        except FileExistsError as exc:
            raise ExecutionArtifactRollbackError(
                f"execution directory rollback restore collision; quarantine retained at {quarantine}"
            ) from exc
        except OSError as exc:
            raise ExecutionArtifactRollbackError(
                f"execution directory rollback restore failed; quarantine retained at {quarantine}"
            ) from exc

    def _quarantine_execution_directory_secure(
        self,
        cleanup_root: Path,
        relative: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        quarantine_descriptor: int | None = None
        quarantine_name = self._quarantine_name()
        moved = False
        operation_failure = False
        try:
            parent_fd = os.open(cleanup_root, directory_flags)
            descriptors.append(parent_fd)
            for component in relative.parts[:-1]:
                parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                descriptors.append(parent_fd)
            try:
                leaf_probe_fd = os.open(
                    relative.parts[-1],
                    directory_flags,
                    dir_fd=parent_fd,
                )
            except OSError:
                return False
            try:
                if os.listdir(leaf_probe_fd):
                    return False
            finally:
                os.close(leaf_probe_fd)
            leaf_name = relative.parts[-1]
            try:
                self._rename_directory_no_replace(
                    leaf_name,
                    quarantine_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except BaseException as exc:
                effected = self._restore_uncertain_directory_quarantine_secure(
                    parent_fd,
                    quarantine_name,
                    leaf_name,
                    expected_identity,
                )
                if isinstance(exc, FileNotFoundError) and not effected:
                    return False
                if isinstance(exc, FileExistsError) and not effected:
                    raise ExecutionArtifactRollbackError(
                        f"execution directory rollback quarantine collision at {relative.parent / quarantine_name}"
                    ) from exc
                raise
            moved = True
            quarantine_descriptor = os.open(
                quarantine_name,
                directory_flags,
                dir_fd=parent_fd,
            )
            quarantine_opened = os.fstat(quarantine_descriptor)
            quarantine_named = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            matches = self._directory_identity_matches(
                quarantine_named,
                quarantine_opened,
                expected_identity,
            )
            if matches:
                try:
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
                except OSError:
                    self._restore_quarantined_directory_secure(
                        parent_fd,
                        leaf_name,
                        relative,
                        quarantine_name,
                    )
                    moved = False
                    return False
                moved = False
                return True
            self._restore_quarantined_directory_secure(
                parent_fd,
                leaf_name,
                relative,
                quarantine_name,
            )
            moved = False
            return False
        except BaseException:
            operation_failure = True
            if moved:
                try:
                    self._restore_quarantined_directory_secure(
                        descriptors[-1],
                        relative.parts[-1],
                        relative,
                        quarantine_name,
                    )
                    moved = False
                except BaseException:
                    pass
            raise
        finally:
            close_failure: BaseException | None = None
            if quarantine_descriptor is not None:
                try:
                    os.close(quarantine_descriptor)
                except BaseException as exc:
                    close_failure = exc
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    close_failure = close_failure or exc
            if close_failure is not None and not operation_failure:
                raise close_failure

    def _restore_uncertain_directory_quarantine_secure(
        self,
        parent_fd: int,
        quarantine_name: str,
        leaf_name: str,
        expected_identity: tuple[int, int],
    ) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                quarantine_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(descriptor)
            named = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not self._directory_identity_matches(
                named,
                opened,
                expected_identity,
            ):
                return False
            try:
                self._rename_directory_no_replace(
                    quarantine_name,
                    leaf_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except BaseException:
                pass
            return True
        except BaseException:
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass

    def _restore_quarantined_directory_secure(
        self,
        parent_fd: int,
        leaf_name: str,
        relative: Path,
        quarantine_name: str,
    ) -> None:
        try:
            self._rename_directory_no_replace(
                quarantine_name,
                leaf_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise ExecutionArtifactRollbackError(
                f"execution directory rollback restore collision; quarantine retained at {relative.parent / quarantine_name}"
            ) from exc
        except OSError as exc:
            raise ExecutionArtifactRollbackError(
                f"execution directory rollback restore failed; quarantine retained at {relative.parent / quarantine_name}"
            ) from exc

    def rollback_execution_transaction(self) -> None:
        with self._execution_transaction_condition:
            rollback_failure: BaseException | None = None
            try:
                if self._execution_transaction_depth:
                    self.conn.rollback()
            except BaseException as exc:
                rollback_failure = exc
            finally:
                self._execution_transaction_depth = 0
                ownerships = self._execution_created_paths
                directory_ownerships = self._execution_created_directories
                self._execution_created_paths = []
                self._execution_created_directories = []
                for ownership in reversed(ownerships):
                    try:
                        opened = os.fstat(ownership.descriptor)
                        self.remove_execution_created_path_if_identity(
                            ownership.path,
                            cleanup_root=ownership.cleanup_root,
                            expected_identity=(opened.st_dev, opened.st_ino),
                        )
                    except BaseException as exc:
                        rollback_failure = rollback_failure or exc
                    finally:
                        try:
                            os.close(ownership.descriptor)
                        except BaseException as exc:
                            rollback_failure = rollback_failure or exc
                directory_ownerships.sort(
                    key=lambda ownership: len(
                        ownership.path.relative_to(ownership.cleanup_root).parts
                    ),
                    reverse=True,
                )
                for ownership in directory_ownerships:
                    try:
                        opened = os.fstat(ownership.descriptor)
                        self._remove_execution_created_directory_if_identity(
                            ownership.path,
                            cleanup_root=ownership.cleanup_root,
                            expected_identity=(opened.st_dev, opened.st_ino),
                        )
                    except BaseException as exc:
                        rollback_failure = rollback_failure or exc
                for ownership in reversed(directory_ownerships):
                    try:
                        os.close(ownership.descriptor)
                    except BaseException as exc:
                        rollback_failure = rollback_failure or exc
                self._execution_transaction_owner = None
                self._execution_transaction_condition.notify_all()
            if rollback_failure is not None:
                raise rollback_failure

    def _commit(self) -> None:
        if not self.execution_transaction_active:
            self.conn.commit()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                name TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                fetch_mode TEXT DEFAULT 'http',
                fetch_config_json TEXT DEFAULT '{}',
                created_at TEXT,
                last_checked_at TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS site_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                captured_at TEXT,
                content_hash TEXT NOT NULL,
                raw_html TEXT DEFAULT '',
                cleaned_html TEXT DEFAULT '',
                content_text TEXT DEFAULT '',
                markdown TEXT DEFAULT '',
                fit_markdown TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                fetch_mode TEXT DEFAULT 'http',
                final_url TEXT DEFAULT '',
                status_code INTEGER,
                links TEXT DEFAULT '[]',
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                detected_at TEXT,
                change_type TEXT NOT NULL,
                summary TEXT DEFAULT '',
                diff_snippet TEXT DEFAULT '',
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                title TEXT DEFAULT '',
                url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                institution TEXT DEFAULT '',
                page_url TEXT DEFAULT '',
                published_at TEXT,
                downloaded_at TEXT,
                local_path TEXT DEFAULT '',
                doc_type TEXT DEFAULT '',
                sha256 TEXT DEFAULT '',
                file_size INTEGER,
                content_type TEXT DEFAULT '',
                etag TEXT DEFAULT '',
                last_modified TEXT DEFAULT '',
                content_md TEXT DEFAULT '',
                content_md_status TEXT DEFAULT 'pending',
                content_md_updated_at TEXT,
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS document_blobs (
                sha256 TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                file_size INTEGER,
                content_type TEXT DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_blobs (
                sha256 TEXT PRIMARY KEY,
                artifact_uri TEXT NOT NULL UNIQUE,
                storage_path TEXT NOT NULL UNIQUE,
                entity_size_bytes INTEGER NOT NULL,
                stored_size_bytes INTEGER NOT NULL,
                storage_encoding TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_blob_retirements (
                sha256 TEXT PRIMARY KEY,
                retired_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_versions (
                version_id TEXT PRIMARY KEY,
                manifest_version TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                normalized_source_identity TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                artifact_uri TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(manifest_version, source_run_id, normalized_source_identity, sha256),
                FOREIGN KEY (sha256) REFERENCES artifact_blobs(sha256)
            );

            CREATE TABLE IF NOT EXISTS artifact_observations (
                artifact_id TEXT PRIMARY KEY,
                version_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                normalized_source_identity TEXT NOT NULL,
                requested_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                final_url TEXT NOT NULL,
                filename TEXT NOT NULL DEFAULT '',
                retrieved_at TEXT NOT NULL,
                http_status INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                artifact_uri TEXT NOT NULL,
                wire_encoding TEXT NOT NULL,
                content_encoding TEXT NOT NULL,
                artifact_role TEXT NOT NULL,
                artifact_status TEXT NOT NULL,
                access_decision_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                redirect_chain_json TEXT NOT NULL DEFAULT '[]',
                discovered_from_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(manifest_version, source_run_id, normalized_source_identity, sha256),
                FOREIGN KEY (version_id) REFERENCES artifact_versions(version_id),
                FOREIGN KEY (sha256) REFERENCES artifact_blobs(sha256)
            );

            CREATE TABLE IF NOT EXISTS artifact_lineage (
                lineage_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                related_artifact_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(artifact_id, relation, related_artifact_id),
                FOREIGN KEY (artifact_id) REFERENCES artifact_observations(artifact_id),
                FOREIGN KEY (related_artifact_id) REFERENCES artifact_observations(artifact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_versions_sha256
                ON artifact_versions(sha256);
            CREATE INDEX IF NOT EXISTS idx_artifact_observations_source
                ON artifact_observations(normalized_source_identity, retrieved_at, artifact_id);
            CREATE INDEX IF NOT EXISTS idx_artifact_observations_version
                ON artifact_observations(version_id);
            CREATE INDEX IF NOT EXISTS idx_artifact_lineage_related
                ON artifact_lineage(related_artifact_id, artifact_id);

            CREATE TRIGGER IF NOT EXISTS artifact_versions_blob_insert_guard
            BEFORE INSERT ON artifact_versions
            WHEN NOT EXISTS (
                SELECT 1 FROM artifact_blobs WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'artifact version requires an existing blob');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_versions_blob_update_guard
            BEFORE UPDATE OF sha256 ON artifact_versions
            WHEN NOT EXISTS (
                SELECT 1 FROM artifact_blobs WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'artifact version requires an existing blob');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_observations_reference_insert_guard
            BEFORE INSERT ON artifact_observations
            WHEN NOT EXISTS (
                    SELECT 1 FROM artifact_versions WHERE version_id = NEW.version_id
                 )
                 OR NOT EXISTS (
                    SELECT 1 FROM artifact_blobs WHERE sha256 = NEW.sha256
                 )
            BEGIN
                SELECT RAISE(ABORT, 'artifact observation requires existing references');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_observations_reference_update_guard
            BEFORE UPDATE OF version_id, sha256 ON artifact_observations
            WHEN NOT EXISTS (
                    SELECT 1 FROM artifact_versions WHERE version_id = NEW.version_id
                 )
                 OR NOT EXISTS (
                    SELECT 1 FROM artifact_blobs WHERE sha256 = NEW.sha256
                 )
            BEGIN
                SELECT RAISE(ABORT, 'artifact observation requires existing references');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_lineage_reference_insert_guard
            BEFORE INSERT ON artifact_lineage
            WHEN NOT EXISTS (
                    SELECT 1 FROM artifact_observations
                    WHERE artifact_id = NEW.artifact_id
                 )
                 OR NOT EXISTS (
                    SELECT 1 FROM artifact_observations
                    WHERE artifact_id = NEW.related_artifact_id
                 )
            BEGIN
                SELECT RAISE(ABORT, 'artifact lineage requires existing observations');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_lineage_reference_update_guard
            BEFORE UPDATE OF artifact_id, related_artifact_id ON artifact_lineage
            WHEN NOT EXISTS (
                    SELECT 1 FROM artifact_observations
                    WHERE artifact_id = NEW.artifact_id
                 )
                 OR NOT EXISTS (
                    SELECT 1 FROM artifact_observations
                    WHERE artifact_id = NEW.related_artifact_id
                 )
            BEGIN
                SELECT RAISE(ABORT, 'artifact lineage requires existing observations');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_blobs_reference_delete_guard
            BEFORE DELETE ON artifact_blobs
            WHEN EXISTS (
                    SELECT 1 FROM artifact_versions WHERE sha256 = OLD.sha256
                 )
                 OR EXISTS (
                    SELECT 1 FROM artifact_observations WHERE sha256 = OLD.sha256
                 )
            BEGIN
                SELECT RAISE(ABORT, 'referenced artifact blob cannot be deleted');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_versions_reference_delete_guard
            BEFORE DELETE ON artifact_versions
            WHEN EXISTS (
                SELECT 1 FROM artifact_observations WHERE version_id = OLD.version_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'referenced artifact version cannot be deleted');
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_observations_reference_delete_guard
            BEFORE DELETE ON artifact_observations
            WHEN EXISTS (
                    SELECT 1 FROM artifact_lineage WHERE artifact_id = OLD.artifact_id
                 )
                 OR EXISTS (
                    SELECT 1 FROM artifact_lineage
                    WHERE related_artifact_id = OLD.artifact_id
                 )
            BEGIN
                SELECT RAISE(ABORT, 'referenced artifact observation cannot be deleted');
            END;

            CREATE TABLE IF NOT EXISTS analysis_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT,
                site_ids TEXT DEFAULT '[]',
                summary_md TEXT DEFAULT '',
                change_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                status TEXT DEFAULT 'queued',
                stage TEXT DEFAULT 'accepted',
                stage_message TEXT DEFAULT '',
                progress INTEGER DEFAULT 0,
                scope_id INTEGER,
                run_id INTEGER,
                produced_artifacts_json TEXT DEFAULT '{}',
                artifact_summary_json TEXT DEFAULT '{}',
                error TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                error_detail_json TEXT DEFAULT '{}',
                is_retryable INTEGER DEFAULT 0,
                accepted_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
            );

            CREATE TABLE IF NOT EXISTS crawl_scopes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                seed_url TEXT NOT NULL,
                allowed_origin TEXT NOT NULL,
                allowed_page_prefixes_json TEXT DEFAULT '[]',
                allowed_file_prefixes_json TEXT DEFAULT '[]',
                max_depth INTEGER DEFAULT 3,
                max_pages INTEGER DEFAULT 100,
                max_files INTEGER DEFAULT 20,
                follow_files INTEGER DEFAULT 1,
                fetch_mode TEXT DEFAULT 'http',
                fetch_config_json TEXT DEFAULT '{}',
                is_initialized INTEGER DEFAULT 0,
                baseline_run_id INTEGER,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_type TEXT DEFAULT 'bootstrap',
                status TEXT DEFAULT 'queued',
                started_at TEXT,
                finished_at TEXT,
                pages_seen INTEGER DEFAULT 0,
                files_seen INTEGER DEFAULT 0,
                pages_changed INTEGER DEFAULT 0,
                files_changed INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id)
            );

            CREATE TABLE IF NOT EXISTS tracked_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                canonical_url TEXT NOT NULL,
                depth INTEGER DEFAULT 0,
                first_seen_run_id INTEGER,
                last_seen_run_id INTEGER,
                miss_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                latest_snapshot_id INTEGER,
                latest_hash TEXT DEFAULT '',
                UNIQUE(scope_id, canonical_url),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id)
            );

            CREATE TABLE IF NOT EXISTS page_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                page_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                captured_at TEXT,
                content_hash TEXT NOT NULL,
                raw_html TEXT DEFAULT '',
                cleaned_html TEXT DEFAULT '',
                content_text TEXT DEFAULT '',
                markdown TEXT DEFAULT '',
                fit_markdown TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                fetch_mode TEXT DEFAULT 'http',
                final_url TEXT DEFAULT '',
                status_code INTEGER,
                links TEXT DEFAULT '[]',
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
            );

            CREATE TABLE IF NOT EXISTS page_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                from_page_id INTEGER NOT NULL,
                to_page_id INTEGER NOT NULL,
                UNIQUE(scope_id, run_id, from_page_id, to_page_id),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                FOREIGN KEY (from_page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (to_page_id) REFERENCES tracked_pages(id)
            );

            CREATE TABLE IF NOT EXISTS tracked_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                canonical_url TEXT NOT NULL,
                first_seen_run_id INTEGER,
                last_seen_run_id INTEGER,
                miss_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                latest_document_id INTEGER,
                latest_sha256 TEXT DEFAULT '',
                UNIQUE(scope_id, canonical_url),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (latest_document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS file_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                page_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                document_id INTEGER,
                discovered_url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                tracked_local_path TEXT DEFAULT '',
                attempt_id TEXT,
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                FOREIGN KEY (page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (file_id) REFERENCES tracked_files(id),
                FOREIGN KEY (document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS acquisition_attempts (
                attempt_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content_kind TEXT NOT NULL,
                profile_id TEXT,
                site_skill_id TEXT,
                site_skill_version TEXT,
                site_skill_package_sha256 TEXT,
                recipe_id TEXT,
                script_sha256 TEXT,
                executor_id TEXT NOT NULL,
                executor_version TEXT NOT NULL,
                requested_url TEXT NOT NULL,
                final_url TEXT,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                acquisition_fingerprint TEXT,
                classification TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                validation_json TEXT DEFAULT '{}',
                canonical_json TEXT NOT NULL,
                redaction_status TEXT NOT NULL,
                authority_mode TEXT NOT NULL,
                UNIQUE(run_id, request_id, position)
            );

            CREATE TABLE IF NOT EXISTS acquisition_artifacts (
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                portable_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                redaction_status TEXT NOT NULL,
                PRIMARY KEY(attempt_id, kind, portable_path),
                FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id)
            );
            CREATE INDEX IF NOT EXISTS idx_acquisition_attempts_run_scope
                ON acquisition_attempts(scope_id, run_id, position, attempt_id);
            CREATE INDEX IF NOT EXISTS idx_acquisition_artifacts_attempt
                ON acquisition_artifacts(attempt_id);
        """)
        self._ensure_column("sites", "fetch_mode", "TEXT DEFAULT 'http'")
        self._ensure_column("sites", "fetch_config_json", "TEXT DEFAULT '{}'")
        self._ensure_column("site_snapshots", "raw_html", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "cleaned_html", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "markdown", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "fit_markdown", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "metadata_json", "TEXT DEFAULT '{}'")
        self._ensure_column("site_snapshots", "fetch_mode", "TEXT DEFAULT 'http'")
        self._ensure_column("site_snapshots", "final_url", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "status_code", "INTEGER")
        self._ensure_column("documents", "sha256", "TEXT DEFAULT ''")
        self._ensure_column("documents", "file_size", "INTEGER")
        self._ensure_column("documents", "content_type", "TEXT DEFAULT ''")
        self._ensure_column("documents", "etag", "TEXT DEFAULT ''")
        self._ensure_column("documents", "last_modified", "TEXT DEFAULT ''")
        self._ensure_column("documents", "content_md_status", "TEXT DEFAULT 'pending'")
        self._ensure_column("documents", "content_md_updated_at", "TEXT")
        self._ensure_column("jobs", "produced_artifacts_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "stage", "TEXT DEFAULT 'accepted'")
        self._ensure_column("jobs", "stage_message", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "artifact_summary_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "error_code", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "error_detail_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "is_retryable", "INTEGER DEFAULT 0")
        self._ensure_column("jobs", "accepted_at", "TEXT")
        self._ensure_column("file_observations", "document_id", "INTEGER")
        self._ensure_column(
            "file_observations", "tracked_local_path", "TEXT DEFAULT ''"
        )
        self._ensure_column("page_snapshots", "attempt_id", "TEXT")
        self._ensure_column("file_observations", "attempt_id", "TEXT")
        self._ensure_column(
            "artifact_versions", "mime_type", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(
            "artifact_observations", "filename", "TEXT NOT NULL DEFAULT ''"
        )
        self.conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS documents_retired_blob_insert_guard
            BEFORE INSERT ON documents
            WHEN COALESCE(NEW.sha256, '') != '' AND EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS documents_retired_blob_update_guard
            BEFORE UPDATE OF sha256 ON documents
            WHEN COALESCE(NEW.sha256, '') != '' AND EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS document_blobs_retired_insert_guard
            BEFORE INSERT ON document_blobs
            WHEN EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS document_blobs_retired_update_guard
            BEFORE UPDATE OF sha256 ON document_blobs
            WHEN EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS tracked_files_retired_blob_insert_guard
            BEFORE INSERT ON tracked_files
            WHEN COALESCE(NEW.latest_sha256, '') != '' AND EXISTS (
                SELECT 1 FROM artifact_blob_retirements
                WHERE sha256 = NEW.latest_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS tracked_files_retired_blob_update_guard
            BEFORE UPDATE OF latest_sha256 ON tracked_files
            WHEN COALESCE(NEW.latest_sha256, '') != '' AND EXISTS (
                SELECT 1 FROM artifact_blob_retirements
                WHERE sha256 = NEW.latest_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS acquisition_artifacts_retired_insert_guard
            BEFORE INSERT ON acquisition_artifacts
            WHEN EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            CREATE TRIGGER IF NOT EXISTS acquisition_artifacts_retired_update_guard
            BEFORE UPDATE OF sha256 ON acquisition_artifacts
            WHEN EXISTS (
                SELECT 1 FROM artifact_blob_retirements WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired artifact blob cannot be referenced');
            END;
            """
        )
        self.conn.execute(
            """UPDATE artifact_versions
               SET mime_type = COALESCE(
                   (SELECT artifact_observations.mime_type
                    FROM artifact_observations
                    WHERE artifact_observations.version_id = artifact_versions.version_id
                    ORDER BY artifact_observations.artifact_id
                    LIMIT 1),
                   mime_type
               )
               WHERE mime_type = ''"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_page_snapshots_attempt_id ON page_snapshots(attempt_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_observations_attempt_id ON file_observations(attempt_id)"
        )
        self._commit()

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str):
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name not in existing:
            self.conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            )

    def close(self):
        close_failure: BaseException | None = None
        try:
            self.rollback_execution_transaction()
        except BaseException as exc:
            close_failure = exc
        try:
            self.conn.close()
        except BaseException as exc:
            close_failure = close_failure or exc
        if close_failure is not None:
            raise close_failure

    # ── Sites ──────────────────────────────────────────────────────────────

    def add_site(self, site: Site) -> Site:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO sites (url, name, tags, fetch_mode, fetch_config_json, created_at, last_checked_at, is_active) VALUES (?,?,?,?,?,?,?,?)",
            (
                site.url,
                site.name,
                json.dumps(site.tags),
                site.fetch_mode,
                json.dumps(site.fetch_config_json),
                site.created_at.isoformat() if site.created_at else now,
                site.last_checked_at.isoformat() if site.last_checked_at else None,
                int(site.is_active),
            ),
        )
        self._commit()
        return self.get_site(cur.lastrowid)

    def get_site(self, site_id: int) -> Optional[Site]:
        row = self.conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        if row is None:
            return None
        return Site(
            id=row["id"],
            url=row["url"],
            name=row["name"] or "",
            tags=json.loads(row["tags"] or "[]"),
            fetch_mode=row["fetch_mode"] or "http",
            fetch_config_json=json.loads(row["fetch_config_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]),
            last_checked_at=_parse_dt(row["last_checked_at"]),
            is_active=bool(row["is_active"]),
        )

    def list_sites(self, active_only: bool = True) -> List[Site]:
        if active_only:
            rows = self.conn.execute("SELECT * FROM sites WHERE is_active=1").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM sites").fetchall()
        return [
            Site(
                id=r["id"],
                url=r["url"],
                name=r["name"] or "",
                tags=json.loads(r["tags"] or "[]"),
                fetch_mode=r["fetch_mode"] or "http",
                fetch_config_json=json.loads(r["fetch_config_json"] or "{}"),
                created_at=_parse_dt(r["created_at"]),
                last_checked_at=_parse_dt(r["last_checked_at"]),
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def deactivate_site(self, site_id: int):
        self.conn.execute("UPDATE sites SET is_active=0 WHERE id=?", (site_id,))
        self._commit()

    def update_site_checked(self, site_id: int):
        self.conn.execute(
            "UPDATE sites SET last_checked_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), site_id),
        )
        self._commit()

    # ── Snapshots ──────────────────────────────────────────────────────────

    def add_snapshot(self, snapshot: SiteSnapshot) -> SiteSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO site_snapshots (
                   site_id, captured_at, content_hash, raw_html, cleaned_html, content_text,
                   markdown, fit_markdown, metadata_json, fetch_mode, final_url, status_code, links
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.site_id,
                snapshot.captured_at.isoformat() if snapshot.captured_at else now,
                snapshot.content_hash,
                snapshot.raw_html,
                snapshot.cleaned_html,
                snapshot.content_text,
                snapshot.markdown,
                snapshot.fit_markdown,
                json.dumps(snapshot.metadata_json),
                snapshot.fetch_mode,
                snapshot.final_url,
                snapshot.status_code,
                json.dumps(snapshot.links),
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return self._row_to_snapshot(row)

    def get_latest_snapshot(self, site_id: int) -> Optional[SiteSnapshot]:
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE site_id=? ORDER BY captured_at DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_snapshot(row)

    def _row_to_snapshot(self, row) -> SiteSnapshot:
        return SiteSnapshot(
            id=row["id"],
            site_id=row["site_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            raw_html=row["raw_html"] or "",
            cleaned_html=row["cleaned_html"] or "",
            content_text=row["content_text"] or "",
            markdown=row["markdown"] or "",
            fit_markdown=row["fit_markdown"] or "",
            metadata_json=json.loads(row["metadata_json"] or "{}"),
            fetch_mode=row["fetch_mode"] or "http",
            final_url=row["final_url"] or "",
            status_code=row["status_code"],
            links=json.loads(row["links"] or "[]"),
        )

    # ── Changes ────────────────────────────────────────────────────────────

    def add_change(self, change: Change) -> Change:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO changes (site_id, detected_at, change_type, summary, diff_snippet) VALUES (?,?,?,?,?)",
            (
                change.site_id,
                change.detected_at.isoformat() if change.detected_at else now,
                change.change_type,
                change.summary,
                change.diff_snippet,
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM changes WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return Change(
            id=row["id"],
            site_id=row["site_id"],
            detected_at=_parse_dt(row["detected_at"]),
            change_type=row["change_type"],
            summary=row["summary"] or "",
            diff_snippet=row["diff_snippet"] or "",
        )

    def list_changes(
        self, site_id: Optional[int] = None, since: Optional[datetime] = None
    ) -> List[Change]:
        query = "SELECT * FROM changes WHERE 1=1"
        params = []
        if site_id is not None:
            query += " AND site_id=?"
            params.append(site_id)
        if since is not None:
            query += " AND detected_at>=?"
            params.append(since.isoformat())
        query += " ORDER BY detected_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [
            Change(
                id=r["id"],
                site_id=r["site_id"],
                detected_at=_parse_dt(r["detected_at"]),
                change_type=r["change_type"],
                summary=r["summary"] or "",
                diff_snippet=r["diff_snippet"] or "",
            )
            for r in rows
        ]

    # ── Documents ──────────────────────────────────────────────────────────

    def add_document(self, doc: Document) -> Document:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.conn.execute(
            "SELECT id FROM documents WHERE download_url = ? ORDER BY id ASC LIMIT 1",
            (doc.download_url,),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """INSERT INTO documents
                   (site_id, title, url, download_url, institution, page_url,
                    published_at, downloaded_at, local_path, doc_type, sha256,
                    file_size, content_type, etag, last_modified, content_md,
                    content_md_status, content_md_updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc.site_id,
                    doc.title,
                    doc.url,
                    doc.download_url,
                    doc.institution,
                    doc.page_url,
                    doc.published_at.isoformat() if doc.published_at else None,
                    doc.downloaded_at.isoformat() if doc.downloaded_at else now,
                    doc.local_path,
                    doc.doc_type,
                    doc.sha256,
                    doc.file_size,
                    doc.content_type,
                    doc.etag,
                    doc.last_modified,
                    doc.content_md,
                    doc.content_md_status,
                    doc.content_md_updated_at.isoformat()
                    if doc.content_md_updated_at
                    else None,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """UPDATE documents
                   SET site_id = ?,
                       title = ?,
                       url = ?,
                       institution = ?,
                       page_url = ?,
                       published_at = ?,
                       downloaded_at = ?,
                       local_path = ?,
                       doc_type = ?,
                       sha256 = ?,
                       file_size = ?,
                       content_type = ?,
                       etag = ?,
                       last_modified = ?,
                       content_md = ?,
                       content_md_status = ?,
                       content_md_updated_at = ?
                   WHERE id = ?""",
                (
                    doc.site_id,
                    doc.title,
                    doc.url,
                    doc.institution,
                    doc.page_url,
                    doc.published_at.isoformat() if doc.published_at else None,
                    doc.downloaded_at.isoformat() if doc.downloaded_at else now,
                    doc.local_path,
                    doc.doc_type,
                    doc.sha256,
                    doc.file_size,
                    doc.content_type,
                    doc.etag,
                    doc.last_modified,
                    doc.content_md,
                    doc.content_md_status,
                    doc.content_md_updated_at.isoformat()
                    if doc.content_md_updated_at
                    else None,
                    row_id,
                ),
            )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id=?", (row_id,)
        ).fetchone()
        return self._row_to_document(row)

    def _row_to_document(self, row) -> Document:
        row_keys = set(row.keys())
        return Document(
            id=row["id"],
            site_id=row["site_id"],
            title=row["title"] or "",
            url=row["url"],
            download_url=row["download_url"],
            institution=row["institution"] or "",
            page_url=row["page_url"] or "",
            published_at=_parse_dt(row["published_at"]),
            downloaded_at=_parse_dt(row["downloaded_at"]),
            local_path=row["local_path"] or "",
            doc_type=row["doc_type"] or "",
            sha256=row["sha256"] or "",
            file_size=row["file_size"],
            content_type=row["content_type"] or "",
            etag=row["etag"] or "",
            last_modified=row["last_modified"] or "",
            content_md=row["content_md"] or "",
            content_md_status=row["content_md_status"] or "pending",
            content_md_updated_at=_parse_dt(row["content_md_updated_at"]),
            tracked_local_path=row["tracked_local_path"]
            if "tracked_local_path" in row_keys
            else "",
        )

    def get_document_by_download_url(self, download_url: str) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE download_url = ? ORDER BY id ASC LIMIT 1",
            (download_url,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_document(self, document_id: int) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_document_by_sha256(self, sha256: str) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE sha256 = ? ORDER BY id ASC LIMIT 1",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_blob(self, sha256: str) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT sha256, canonical_path, file_size, content_type
               FROM document_blobs WHERE sha256 = ?""",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return {
            "sha256": row["sha256"],
            "canonical_path": row["canonical_path"],
            "file_size": row["file_size"],
            "content_type": row["content_type"] or "",
        }

    def upsert_blob(
        self,
        *,
        sha256: str,
        canonical_path: str,
        file_size: Optional[int],
        content_type: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO document_blobs (
                sha256, canonical_path, file_size, content_type, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                canonical_path = excluded.canonical_path,
                file_size = excluded.file_size,
                content_type = excluded.content_type,
                last_seen_at = excluded.last_seen_at
            """,
            (sha256, canonical_path, file_size, content_type, now, now),
        )
        self._commit()

    def list_documents(
        self, site_id: Optional[int] = None, institution: Optional[str] = None
    ) -> List[Document]:
        query = "SELECT * FROM documents WHERE 1=1"
        params = []
        if site_id is not None:
            query += " AND site_id=?"
            params.append(site_id)
        if institution is not None:
            query += " AND institution=?"
            params.append(institution)
        query += " ORDER BY downloaded_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_document(r) for r in rows]

    def list_scope_documents(
        self, scope_id: int, run_id: Optional[int] = None
    ) -> List[Document]:
        if run_id is None:
            rows = self.conn.execute(
                """
                SELECT
                    d.*,
                    COALESCE(tp.canonical_url, d.page_url, '') AS page_url,
                    fo.tracked_local_path AS tracked_local_path
                FROM file_observations fo
                LEFT JOIN tracked_files tf ON tf.id = fo.file_id
                JOIN documents d ON d.id = COALESCE(fo.document_id, tf.latest_document_id)
                LEFT JOIN tracked_pages tp ON tp.id = fo.page_id
                WHERE fo.scope_id = ? AND COALESCE(fo.document_id, tf.latest_document_id) IS NOT NULL
                ORDER BY tp.canonical_url ASC, d.download_url ASC, fo.id ASC
                """,
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT
                    d.*,
                    COALESCE(tp.canonical_url, d.page_url, '') AS page_url,
                    fo.tracked_local_path AS tracked_local_path
                FROM file_observations fo
                LEFT JOIN tracked_files tf ON tf.id = fo.file_id
                JOIN documents d ON d.id = COALESCE(fo.document_id, tf.latest_document_id)
                LEFT JOIN tracked_pages tp ON tp.id = fo.page_id
                WHERE fo.scope_id = ? AND fo.run_id = ? AND COALESCE(fo.document_id, tf.latest_document_id) IS NOT NULL
                ORDER BY tp.canonical_url ASC, d.download_url ASC, fo.id ASC
                """,
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_document(row) for row in rows]

    def update_document_content_md(
        self,
        document_id: int,
        *,
        content_md: str,
        content_md_status: str = "converted",
    ) -> Optional[Document]:
        existing = self.get_document(document_id)
        if existing is None:
            return None

        updated_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE documents
            SET content_md = ?,
                content_md_status = ?,
                content_md_updated_at = ?
            WHERE id = ?
            """,
            (content_md, content_md_status, updated_at, document_id),
        )
        self._commit()
        return self.get_document(document_id)

    # ── Analyses ───────────────────────────────────────────────────────────

    def add_analysis(self, report: AnalysisReport) -> AnalysisReport:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO analysis_reports
               (period_start, period_end, generated_at, site_ids, summary_md, change_count)
               VALUES (?,?,?,?,?,?)""",
            (
                report.period_start.isoformat(),
                report.period_end.isoformat(),
                report.generated_at.isoformat() if report.generated_at else now,
                json.dumps(report.site_ids),
                report.summary_md,
                report.change_count,
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM analysis_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return self._row_to_analysis(row)

    def _row_to_analysis(self, row) -> AnalysisReport:
        return AnalysisReport(
            id=row["id"],
            period_start=_parse_dt(row["period_start"]),
            period_end=_parse_dt(row["period_end"]),
            generated_at=_parse_dt(row["generated_at"]),
            site_ids=json.loads(row["site_ids"] or "[]"),
            summary_md=row["summary_md"] or "",
            change_count=row["change_count"] or 0,
        )

    def list_analyses(self) -> List[AnalysisReport]:
        rows = self.conn.execute(
            "SELECT * FROM analysis_reports ORDER BY generated_at DESC"
        ).fetchall()
        return [self._row_to_analysis(r) for r in rows]

    # ── Jobs ───────────────────────────────────────────────────────────────

    def add_job(self, job: Job) -> Job:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO jobs (
                job_type, status, stage, stage_message, progress, scope_id, run_id,
                produced_artifacts_json, artifact_summary_json, error, error_code,
                error_detail_json, is_retryable, accepted_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_type,
                job.status,
                job.stage,
                job.stage_message,
                job.progress,
                job.scope_id,
                job.run_id,
                json.dumps(job.produced_artifacts),
                json.dumps(job.artifact_summary),
                job.error,
                job.error_code,
                json.dumps(job.error_detail),
                int(job.is_retryable),
                job.accepted_at.isoformat() if job.accepted_at else now,
                job.started_at.isoformat() if job.started_at else None,
                job.finished_at.isoformat() if job.finished_at else None,
            ),
        )
        self._commit()
        return self.get_job(cur.lastrowid)

    _UPDATABLE_JOB_FIELDS: Final[frozenset[str]] = frozenset(
        [
            "status",
            "stage",
            "stage_message",
            "progress",
            "scope_id",
            "run_id",
            "produced_artifacts",
            "artifact_summary",
            "error",
            "error_code",
            "error_detail",
            "is_retryable",
            "accepted_at",
            "started_at",
            "finished_at",
        ]
    )

    _JOB_FIELD_COLUMN_MAP: Final[dict[str, str]] = {
        "produced_artifacts": "produced_artifacts_json",
        "artifact_summary": "artifact_summary_json",
        "error_detail": "error_detail_json",
    }

    def update_job(self, job_id: int, **fields) -> Optional[Job]:
        if not fields:
            return self.get_job(job_id)
        unknown = set(fields) - self._UPDATABLE_JOB_FIELDS
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        assignments = []
        params = []
        for key, value in fields.items():
            column_name = self._JOB_FIELD_COLUMN_MAP.get(key, key)
            assignments.append(f"{column_name} = ?")
            if isinstance(value, datetime):
                params.append(value.isoformat())
            elif key in {"produced_artifacts", "artifact_summary", "error_detail"}:
                params.append(json.dumps(value or {}))
            elif key == "is_retryable":
                params.append(int(bool(value)))
            else:
                params.append(value)
        params.append(job_id)
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
            params,
        )
        self._commit()
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> Optional[Job]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        scope_id: Optional[int] = None,
        job_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Job]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[object] = []
        if scope_id is not None:
            query += " AND scope_id = ?"
            params.append(scope_id)
        if job_type:
            query += " AND job_type = ?"
            params.append(job_type)
        query += " ORDER BY COALESCE(finished_at, started_at, accepted_at) DESC, job_id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_latest_job(
        self, *, scope_id: int, job_type: str, status: Optional[str] = None
    ) -> Optional[Job]:
        query = """
            SELECT * FROM jobs
            WHERE scope_id = ? AND job_type = ?
        """
        params: list[object] = [scope_id, job_type]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY COALESCE(finished_at, started_at, accepted_at) DESC, job_id DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def _row_to_job(self, row) -> Job:
        try:
            produced_artifacts = json.loads(row["produced_artifacts_json"] or "{}")
        except json.JSONDecodeError:
            produced_artifacts = {}
        if not isinstance(produced_artifacts, dict):
            produced_artifacts = {}
        try:
            artifact_summary = json.loads(row["artifact_summary_json"] or "{}")
        except json.JSONDecodeError:
            artifact_summary = {}
        if not isinstance(artifact_summary, dict):
            artifact_summary = {}
        try:
            error_detail = json.loads(row["error_detail_json"] or "{}")
        except json.JSONDecodeError:
            error_detail = {}
        if not isinstance(error_detail, dict):
            error_detail = {}
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            status=row["status"] or "queued",
            stage=row["stage"] or "accepted",
            stage_message=row["stage_message"] or "",
            progress=row["progress"] or 0,
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            produced_artifacts=produced_artifacts,
            artifact_summary=artifact_summary,
            error=row["error"] or "",
            error_code=row["error_code"] or "",
            error_detail=error_detail,
            is_retryable=bool(row["is_retryable"] or 0),
            accepted_at=_parse_dt(row["accepted_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
        )

    # ── Recursive Scopes ──────────────────────────────────────────────────

    def add_crawl_scope(self, scope: CrawlScope) -> CrawlScope:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO crawl_scopes (
                site_id, seed_url, allowed_origin, allowed_page_prefixes_json,
                allowed_file_prefixes_json, max_depth, max_pages, max_files,
                follow_files, fetch_mode, fetch_config_json, is_initialized,
                baseline_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope.site_id,
                scope.seed_url,
                scope.allowed_origin,
                json.dumps(scope.allowed_page_prefixes),
                json.dumps(scope.allowed_file_prefixes),
                scope.max_depth,
                scope.max_pages,
                scope.max_files,
                int(scope.follow_files),
                scope.fetch_mode,
                json.dumps(scope.fetch_config_json),
                int(scope.is_initialized),
                scope.baseline_run_id,
                scope.created_at.isoformat() if scope.created_at else now,
                scope.updated_at.isoformat() if scope.updated_at else now,
            ),
        )
        self._commit()
        return self.get_crawl_scope(cur.lastrowid)

    def update_crawl_scope(self, scope: CrawlScope) -> CrawlScope:
        if scope.id is None:
            raise ValueError("scope.id must not be None when updating a crawl scope")
        updated_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE crawl_scopes
            SET site_id = ?,
                seed_url = ?,
                allowed_origin = ?,
                allowed_page_prefixes_json = ?,
                allowed_file_prefixes_json = ?,
                max_depth = ?,
                max_pages = ?,
                max_files = ?,
                follow_files = ?,
                fetch_mode = ?,
                fetch_config_json = ?,
                is_initialized = ?,
                baseline_run_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                scope.site_id,
                scope.seed_url,
                scope.allowed_origin,
                json.dumps(scope.allowed_page_prefixes),
                json.dumps(scope.allowed_file_prefixes),
                scope.max_depth,
                scope.max_pages,
                scope.max_files,
                int(scope.follow_files),
                scope.fetch_mode,
                json.dumps(scope.fetch_config_json),
                int(scope.is_initialized),
                scope.baseline_run_id,
                updated_at,
                scope.id,
            ),
        )
        self._commit()
        return self.get_crawl_scope(scope.id)

    def get_crawl_scope(self, scope_id: int) -> Optional[CrawlScope]:
        row = self.conn.execute(
            "SELECT * FROM crawl_scopes WHERE id = ?",
            (scope_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_crawl_scope(row)

    def list_crawl_scopes(self, site_id: Optional[int] = None) -> List[CrawlScope]:
        if site_id is None:
            rows = self.conn.execute(
                "SELECT * FROM crawl_scopes ORDER BY id ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM crawl_scopes WHERE site_id = ? ORDER BY id ASC",
                (site_id,),
            ).fetchall()
        return [self._row_to_crawl_scope(row) for row in rows]

    def _row_to_crawl_scope(self, row) -> CrawlScope:
        return CrawlScope(
            id=row["id"],
            site_id=row["site_id"],
            seed_url=row["seed_url"],
            allowed_origin=row["allowed_origin"] or "",
            allowed_page_prefixes=json.loads(row["allowed_page_prefixes_json"] or "[]"),
            allowed_file_prefixes=json.loads(row["allowed_file_prefixes_json"] or "[]"),
            max_depth=row["max_depth"] or 3,
            max_pages=row["max_pages"] or 100,
            max_files=row["max_files"] or 20,
            follow_files=bool(row["follow_files"]),
            fetch_mode=row["fetch_mode"] or "http",
            fetch_config_json=json.loads(row["fetch_config_json"] or "{}"),
            is_initialized=bool(row["is_initialized"]),
            baseline_run_id=row["baseline_run_id"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def add_crawl_run(self, run: CrawlRun) -> CrawlRun:
        cur = self.conn.execute(
            """
            INSERT INTO crawl_runs (
                scope_id, run_type, status, started_at, finished_at,
                pages_seen, files_seen, pages_changed, files_changed, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.scope_id,
                run.run_type,
                run.status,
                run.started_at.isoformat() if run.started_at else None,
                run.finished_at.isoformat() if run.finished_at else None,
                run.pages_seen,
                run.files_seen,
                run.pages_changed,
                run.files_changed,
                run.error_message,
            ),
        )
        self._commit()
        return self.get_crawl_run(cur.lastrowid)

    def update_crawl_run(self, run_id: int, **fields) -> Optional[CrawlRun]:
        if not fields:
            return self.get_crawl_run(run_id)
        assignments = []
        params = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if isinstance(value, datetime):
                params.append(value.isoformat())
            else:
                params.append(value)
        params.append(run_id)
        self.conn.execute(
            f"UPDATE crawl_runs SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        self._commit()
        return self.get_crawl_run(run_id)

    def get_crawl_run(self, run_id: int) -> Optional[CrawlRun]:
        row = self.conn.execute(
            "SELECT * FROM crawl_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_crawl_run(row)

    def _row_to_crawl_run(self, row) -> CrawlRun:
        return CrawlRun(
            id=row["id"],
            scope_id=row["scope_id"],
            run_type=row["run_type"] or "bootstrap",
            status=row["status"] or "queued",
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            pages_seen=row["pages_seen"] or 0,
            files_seen=row["files_seen"] or 0,
            pages_changed=row["pages_changed"] or 0,
            files_changed=row["files_changed"] or 0,
            error_message=row["error_message"] or "",
        )

    def upsert_tracked_page(
        self,
        *,
        scope_id: int,
        canonical_url: str,
        depth: int,
        run_id: int,
        latest_hash: str = "",
        latest_snapshot_id: Optional[int] = None,
    ) -> TrackedPage:
        existing = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE scope_id = ? AND canonical_url = ?",
            (scope_id, canonical_url),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO tracked_pages (
                    scope_id, canonical_url, depth, first_seen_run_id, last_seen_run_id,
                    miss_count, is_active, latest_snapshot_id, latest_hash
                ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    scope_id,
                    canonical_url,
                    depth,
                    run_id,
                    run_id,
                    latest_snapshot_id,
                    latest_hash,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """
                UPDATE tracked_pages
                SET depth = ?,
                    last_seen_run_id = ?,
                    miss_count = 0,
                    is_active = 1,
                    latest_snapshot_id = COALESCE(?, latest_snapshot_id),
                    latest_hash = CASE WHEN ? <> '' THEN ? ELSE latest_hash END
                WHERE id = ?
                """,
                (
                    depth,
                    run_id,
                    latest_snapshot_id,
                    latest_hash,
                    latest_hash,
                    row_id,
                ),
            )
        self._commit()
        return self.get_tracked_page(row_id)

    def get_tracked_page(self, page_id: int) -> Optional[TrackedPage]:
        row = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE id = ?",
            (page_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tracked_page(row)

    def list_tracked_pages(self, scope_id: int) -> List[TrackedPage]:
        rows = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE scope_id = ? ORDER BY canonical_url ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_tracked_page(row) for row in rows]

    def _row_to_tracked_page(self, row) -> TrackedPage:
        return TrackedPage(
            id=row["id"],
            scope_id=row["scope_id"],
            canonical_url=row["canonical_url"],
            depth=row["depth"] or 0,
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            miss_count=row["miss_count"] or 0,
            is_active=bool(row["is_active"]),
            latest_snapshot_id=row["latest_snapshot_id"],
            latest_hash=row["latest_hash"] or "",
        )

    def add_page_snapshot(self, snapshot: PageSnapshot) -> PageSnapshot:
        self._validate_accepted_attempt(
            snapshot.attempt_id, snapshot.scope_id, snapshot.run_id, "page"
        )
        text_fields = {
            field: redact_persisted_value(getattr(snapshot, field))
            for field in (
                "raw_html",
                "cleaned_html",
                "content_text",
                "markdown",
                "fit_markdown",
            )
        }
        hash_basis = snapshot.metadata_json.get("hash_basis")
        content_hash = snapshot.content_hash
        if hash_basis in text_fields and text_fields[hash_basis] != getattr(
            snapshot, hash_basis
        ):
            content_hash = compute_hash(text_fields[hash_basis])
        snapshot = snapshot.model_copy(
            update={
                **text_fields,
                "content_hash": content_hash,
                "metadata_json": redact_persisted_value(snapshot.metadata_json),
                "final_url": redact_persisted_value(snapshot.final_url),
                "links": redact_persisted_value(snapshot.links),
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO page_snapshots (
                scope_id, page_id, run_id, captured_at, content_hash, raw_html, cleaned_html,
                content_text, markdown, fit_markdown, metadata_json, fetch_mode,
                final_url, status_code, links, attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.scope_id,
                snapshot.page_id,
                snapshot.run_id,
                snapshot.captured_at.isoformat() if snapshot.captured_at else now,
                snapshot.content_hash,
                snapshot.raw_html,
                snapshot.cleaned_html,
                snapshot.content_text,
                snapshot.markdown,
                snapshot.fit_markdown,
                json.dumps(snapshot.metadata_json),
                snapshot.fetch_mode,
                snapshot.final_url,
                snapshot.status_code,
                json.dumps(snapshot.links),
                snapshot.attempt_id,
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return self._row_to_page_snapshot(row)

    def list_page_snapshots(self, page_id: int) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE page_id = ? ORDER BY captured_at DESC",
            (page_id,),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def list_page_snapshots_for_run(
        self, scope_id: int, run_id: int
    ) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
            (scope_id, run_id),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def list_scope_page_snapshots(self, scope_id: int) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE scope_id = ? ORDER BY captured_at ASC, id ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def _row_to_page_snapshot(self, row) -> PageSnapshot:
        return PageSnapshot(
            id=row["id"],
            scope_id=row["scope_id"],
            page_id=row["page_id"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            raw_html=row["raw_html"] or "",
            cleaned_html=row["cleaned_html"] or "",
            content_text=row["content_text"] or "",
            markdown=row["markdown"] or "",
            fit_markdown=row["fit_markdown"] or "",
            metadata_json=json.loads(row["metadata_json"] or "{}"),
            fetch_mode=row["fetch_mode"] or "http",
            final_url=row["final_url"] or "",
            status_code=row["status_code"],
            links=json.loads(row["links"] or "[]"),
        )

    def add_page_edge(self, edge: PageEdge) -> PageEdge:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO page_edges (
                scope_id, run_id, from_page_id, to_page_id
            ) VALUES (?, ?, ?, ?)
            """,
            (
                edge.scope_id,
                edge.run_id,
                edge.from_page_id,
                edge.to_page_id,
            ),
        )
        self._commit()
        if cur.lastrowid:
            row_id = cur.lastrowid
        else:
            row = self.conn.execute(
                """
                SELECT * FROM page_edges
                WHERE scope_id = ? AND run_id = ? AND from_page_id = ? AND to_page_id = ?
                """,
                (edge.scope_id, edge.run_id, edge.from_page_id, edge.to_page_id),
            ).fetchone()
            row_id = row["id"]
        row = self.conn.execute(
            "SELECT * FROM page_edges WHERE id = ?", (row_id,)
        ).fetchone()
        return self._row_to_page_edge(row)

    def list_page_edges(
        self, scope_id: int, run_id: Optional[int] = None
    ) -> List[PageEdge]:
        if run_id is None:
            rows = self.conn.execute(
                "SELECT * FROM page_edges WHERE scope_id = ? ORDER BY id ASC",
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM page_edges WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_page_edge(row) for row in rows]

    def _row_to_page_edge(self, row) -> PageEdge:
        return PageEdge(
            id=row["id"],
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            from_page_id=row["from_page_id"],
            to_page_id=row["to_page_id"],
        )

    def upsert_tracked_file(
        self,
        *,
        scope_id: int,
        canonical_url: str,
        run_id: int,
        latest_document_id: Optional[int] = None,
        latest_sha256: str = "",
    ) -> TrackedFile:
        existing = self.conn.execute(
            "SELECT * FROM tracked_files WHERE scope_id = ? AND canonical_url = ?",
            (scope_id, canonical_url),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO tracked_files (
                    scope_id, canonical_url, first_seen_run_id, last_seen_run_id,
                    miss_count, is_active, latest_document_id, latest_sha256
                ) VALUES (?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    scope_id,
                    canonical_url,
                    run_id,
                    run_id,
                    latest_document_id,
                    latest_sha256,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """
                UPDATE tracked_files
                SET last_seen_run_id = ?,
                    miss_count = 0,
                    is_active = 1,
                    latest_document_id = COALESCE(?, latest_document_id),
                    latest_sha256 = CASE WHEN ? <> '' THEN ? ELSE latest_sha256 END
                WHERE id = ?
                """,
                (
                    run_id,
                    latest_document_id,
                    latest_sha256,
                    latest_sha256,
                    row_id,
                ),
            )
        self._commit()
        return self.get_tracked_file(row_id)

    def get_tracked_file(self, file_id: int) -> Optional[TrackedFile]:
        row = self.conn.execute(
            "SELECT * FROM tracked_files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tracked_file(row)

    def list_tracked_files(self, scope_id: int) -> List[TrackedFile]:
        rows = self.conn.execute(
            "SELECT * FROM tracked_files WHERE scope_id = ? ORDER BY canonical_url ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_tracked_file(row) for row in rows]

    def _row_to_tracked_file(self, row) -> TrackedFile:
        return TrackedFile(
            id=row["id"],
            scope_id=row["scope_id"],
            canonical_url=row["canonical_url"],
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            miss_count=row["miss_count"] or 0,
            is_active=bool(row["is_active"]),
            latest_document_id=row["latest_document_id"],
            latest_sha256=row["latest_sha256"] or "",
        )

    def add_file_observation(self, observation: FileObservation) -> FileObservation:
        self._validate_accepted_attempt(
            observation.attempt_id, observation.scope_id, observation.run_id, "document"
        )
        observation = observation.model_copy(
            update={
                "discovered_url": redact_persisted_value(observation.discovered_url),
                "download_url": redact_persisted_value(observation.download_url),
                "tracked_local_path": redact_persisted_value(
                    observation.tracked_local_path
                ),
            }
        )
        cur = self.conn.execute(
            """
            INSERT INTO file_observations (
                scope_id, run_id, page_id, file_id, document_id, discovered_url, download_url, tracked_local_path, attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.scope_id,
                observation.run_id,
                observation.page_id,
                observation.file_id,
                observation.document_id,
                observation.discovered_url,
                observation.download_url,
                observation.tracked_local_path,
                observation.attempt_id,
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM file_observations WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return self._row_to_file_observation(row)

    def list_file_observations(
        self, scope_id: int, run_id: Optional[int] = None
    ) -> List[FileObservation]:
        if run_id is None:
            rows = self.conn.execute(
                "SELECT * FROM file_observations WHERE scope_id = ? ORDER BY id ASC",
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM file_observations WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_file_observation(row) for row in rows]

    def _row_to_file_observation(self, row) -> FileObservation:
        return FileObservation(
            id=row["id"],
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            page_id=row["page_id"],
            file_id=row["file_id"],
            document_id=row["document_id"],
            discovered_url=row["discovered_url"],
            download_url=row["download_url"],
            tracked_local_path=row["tracked_local_path"] or "",
        )

    def add_acquisition_attempt(
        self, attempt: AcquisitionAttempt
    ) -> AcquisitionAttempt:
        self._validate_acquisition_attempt_semantics(attempt)
        sanitized_payload = redact_persisted_value(
            attempt.model_dump(mode="python", exclude={"canonical_json", "artifacts"})
        )
        sanitized = AcquisitionAttempt(**sanitized_payload, canonical_json="")
        payload = self._canonical_attempt_payload(attempt, sanitized)
        existing = self.conn.execute(
            "SELECT canonical_json FROM acquisition_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        if existing is not None:
            persisted = self.get_acquisition_attempt(attempt.attempt_id)

            def comparable(value):
                return value.model_dump(
                    mode="json", exclude={"canonical_json", "artifacts"}
                )

            if existing["canonical_json"] != payload or comparable(
                persisted
            ) != comparable(sanitized):
                raise ValueError("conflicting acquisition attempt id")
            return self.get_acquisition_attempt(attempt.attempt_id)
        self.conn.execute(
            """INSERT INTO acquisition_attempts (
                attempt_id, request_id, scope_id, run_id, position, content_kind, profile_id,
                site_skill_id, site_skill_version, site_skill_package_sha256, recipe_id,
                script_sha256, executor_id, executor_version, requested_url, final_url,
                requested_at, started_at, finished_at, acquisition_fingerprint, classification,
                accepted, reason, validation_json, canonical_json, redaction_status, authority_mode
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sanitized.attempt_id,
                sanitized.request_id,
                sanitized.scope_id,
                sanitized.run_id,
                sanitized.position,
                sanitized.content_kind,
                sanitized.profile_id,
                sanitized.site_skill_id,
                sanitized.site_skill_version,
                sanitized.site_skill_package_sha256,
                sanitized.recipe_id,
                sanitized.script_sha256,
                sanitized.executor_id,
                sanitized.executor_version,
                sanitized.requested_url,
                sanitized.final_url,
                sanitized.requested_at.isoformat(),
                sanitized.started_at.isoformat() if sanitized.started_at else None,
                sanitized.finished_at.isoformat() if sanitized.finished_at else None,
                sanitized.acquisition_fingerprint,
                sanitized.classification,
                int(sanitized.accepted),
                sanitized.reason,
                json.dumps(sanitized.validation, sort_keys=True),
                payload,
                sanitized.redaction_status,
                sanitized.authority_mode,
            ),
        )
        self._commit()
        return self.get_acquisition_attempt(attempt.attempt_id)

    @staticmethod
    def _validate_acquisition_attempt_semantics(attempt: AcquisitionAttempt) -> None:
        if attempt.classification == "accepted" and not attempt.accepted:
            raise ValueError("accepted classification requires accepted=true")
        if attempt.accepted and (
            attempt.classification != "accepted"
            or (attempt.reason and attempt.reason != "accepted")
        ):
            raise ValueError("conflicting accepted attempt classification or reason")
        if (
            not attempt.accepted
            and attempt.reason
            and attempt.reason != attempt.classification
        ):
            raise ValueError("conflicting rejected attempt classification or reason")

    def add_legacy_compatibility_attempt(
        self,
        *,
        scope_id: int,
        run_id: int,
        identity: str,
        content_kind: str = "page",
    ) -> AcquisitionAttempt:
        """Create explicit lineage for compatibility fixtures/imports that predate governance."""
        request_id = hashlib.sha256(
            f"legacy-compatibility\0{scope_id}\0{run_id}\0{content_kind}\0{identity}".encode()
        ).hexdigest()
        existing = self.get_acquisition_attempt(request_id)
        if existing is not None:
            expected = (
                scope_id,
                run_id,
                content_kind,
                "legacy_compatibility_import",
                "legacy-compatibility",
                redact_persisted_value(identity),
                True,
            )
            actual = (
                existing.scope_id,
                existing.run_id,
                existing.content_kind,
                existing.executor_id,
                existing.executor_version,
                existing.requested_url,
                existing.accepted,
            )
            if actual != expected:
                raise ValueError("conflicting legacy compatibility attempt id")
            return existing
        now = datetime.now(timezone.utc)
        return self.add_acquisition_attempt(
            AcquisitionAttempt(
                attempt_id=request_id,
                request_id=request_id,
                scope_id=scope_id,
                run_id=run_id,
                position=0,
                content_kind=content_kind,
                executor_id="legacy_compatibility_import",
                executor_version="legacy-compatibility",
                requested_url=identity,
                final_url=identity,
                requested_at=now,
                started_at=now,
                finished_at=now,
                classification="accepted",
                accepted=True,
                reason="accepted",
                validation={"decision": "accepted"},
                authority_mode="legacy_compatibility",
            )
        )

    def admit_inline_acquisition_artifacts(
        self, attempt_id: str, payloads
    ) -> List[AcquisitionArtifact]:
        if not payloads:
            return []
        if (
            not isinstance(payloads, Sequence)
            or isinstance(payloads, (str, bytes, bytearray))
            or len(payloads) > 8
        ):
            raise ValueError("inline acquisition artifacts must be a bounded sequence")
        payloads = tuple(payloads)
        self._validate_portable_component(attempt_id)
        if (
            self.conn.execute(
                "SELECT 1 FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            is None
        ):
            raise ValueError(
                "acquisition artifact requires an existing persisted attempt"
            )
        allowed = {
            "screenshot": {"image/png", "image/jpeg", "image/webp"},
            "trace": {"application/json", "application/zip"},
            "raw_capture": {
                "text/html",
                "application/octet-stream",
                "application/json",
            },
        }
        staged: list[tuple[int, str, AcquisitionArtifact]] = []
        open_artifact_descriptors: list[int] = []
        created: list[tuple[str, int, int]] = []
        root_fd = artifacts_fd = attempt_fd = None
        artifacts_directory_created = attempt_directory_created = False
        artifacts_directory_identity = attempt_directory_identity = None
        nested_execution = self.execution_transaction_active
        savepoint_started = False
        pending_created: tuple[str, int, int] | None = None
        durable_commit = False
        failure_active = False
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_fd = os.open(self.db_path.parent, directory_flags)
            try:
                os.mkdir("acquisition_artifacts", 0o700, dir_fd=root_fd)
                artifacts_directory_created = True
            except FileExistsError:
                pass
            artifacts_fd = os.open(
                "acquisition_artifacts", directory_flags, dir_fd=root_fd
            )
            artifacts_info = os.fstat(artifacts_fd)
            artifacts_directory_identity = (
                artifacts_info.st_dev,
                artifacts_info.st_ino,
            )
            try:
                os.mkdir(attempt_id, 0o700, dir_fd=artifacts_fd)
                attempt_directory_created = True
            except FileExistsError:
                pass
            attempt_fd = os.open(attempt_id, directory_flags, dir_fd=artifacts_fd)
            attempt_info = os.fstat(attempt_fd)
            attempt_directory_identity = (attempt_info.st_dev, attempt_info.st_ino)
            for index, item in enumerate(payloads):
                if not isinstance(item, Mapping):
                    raise ValueError("inline artifact descriptor must be an object")
                kind, mime = str(item.get("kind", "")), str(item.get("mime_type", ""))
                if kind not in allowed or mime not in allowed[kind]:
                    raise ValueError("inline artifact kind or MIME is not allowed")
                try:
                    data = base64.b64decode(
                        str(item.get("data_base64", "")), validate=True
                    )
                except (binascii.Error, ValueError):
                    raise ValueError(
                        "inline artifact payload is not valid base64"
                    ) from None
                if len(data) > 4 * 1024 * 1024 or int(
                    item.get("size_bytes", -1)
                ) != len(data):
                    raise ValueError(
                        "inline artifact byte size mismatch or limit exceeded"
                    )
                digest = hashlib.sha256(data).hexdigest()
                if item.get("sha256") != digest:
                    raise ValueError("inline artifact SHA-256 mismatch")
                data, redaction_status = self._govern_artifact_bytes(kind, mime, data)
                digest = hashlib.sha256(data).hexdigest()
                suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "application/json": ".json",
                    "application/zip": ".zip",
                    "text/html": ".html",
                    "application/octet-stream": ".bin",
                }[mime]
                portable = (
                    f"acquisition_artifacts/{attempt_id}/{index:02d}-{kind}{suffix}"
                )
                target = f"{index:02d}-{kind}{suffix}"
                descriptor = os.open(
                    ".", os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=attempt_fd
                )
                open_artifact_descriptors.append(descriptor)
                try:
                    created_identity = os.stat(descriptor)
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        created_identity.st_dev,
                        created_identity.st_ino,
                    ):
                        raise ValueError(
                            "temporary acquisition artifact identity changed"
                        )
                    artifact = AcquisitionArtifact(
                        attempt_id=attempt_id,
                        kind=kind,
                        portable_path=portable,
                        mime_type=mime,
                        size_bytes=len(data),
                        sha256=digest,
                        redaction_status=redaction_status,
                    )
                    with os.fdopen(descriptor, "wb", closefd=False) as stream:
                        stream.write(data)
                        stream.flush()
                    os.fsync(descriptor)
                except BaseException:
                    raise
                staged.append((descriptor, target, artifact))
            self._verify_pinned_artifact_directories(
                root_fd, artifacts_fd, attempt_fd, attempt_id
            )
            if nested_execution:
                self.conn.execute("SAVEPOINT inline_acquisition_artifacts")
                savepoint_started = True
            else:
                self.conn.execute("BEGIN IMMEDIATE")
            for descriptor, target, artifact in staged:
                row = self.conn.execute(
                    """SELECT mime_type, size_bytes, sha256, redaction_status FROM acquisition_artifacts
                    WHERE attempt_id=? AND kind=? AND portable_path=?""",
                    (artifact.attempt_id, artifact.kind, artifact.portable_path),
                ).fetchone()
                if row is not None and (
                    row["mime_type"],
                    row["size_bytes"],
                    row["sha256"],
                    row["redaction_status"],
                ) != (
                    artifact.mime_type,
                    artifact.size_bytes,
                    artifact.sha256,
                    artifact.redaction_status,
                ):
                    raise ValueError("conflicting acquisition artifact metadata")
                try:
                    source_info = os.fstat(descriptor)
                    pending_created = (
                        target,
                        source_info.st_dev,
                        source_info.st_ino,
                    )
                    self._link_unnamed_temporary(descriptor, attempt_fd, target)
                except FileExistsError:
                    pending_created = None
                    self._verify_existing_artifact(attempt_fd, target, artifact)
                else:
                    created.append(pending_created)
                    pending_created = None
                self.conn.execute(
                    """INSERT INTO acquisition_artifacts
                    (attempt_id, kind, portable_path, mime_type, size_bytes, sha256, redaction_status)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(attempt_id, kind, portable_path) DO NOTHING""",
                    (
                        artifact.attempt_id,
                        artifact.kind,
                        artifact.portable_path,
                        artifact.mime_type,
                        artifact.size_bytes,
                        artifact.sha256,
                        artifact.redaction_status,
                    ),
                )
            for _, target, artifact in staged:
                self._verify_existing_artifact(attempt_fd, target, artifact)
            # Verify every final named target before checking that the pinned
            # directory chain is still the one reachable by its published path.
            for _, target, artifact in staged:
                self._verify_existing_artifact(attempt_fd, target, artifact)
            # This directory rebind is intentionally the final filesystem
            # operation immediately before the database commit.
            self._verify_pinned_artifact_directories(
                root_fd, artifacts_fd, attempt_fd, attempt_id
            )
            if nested_execution:
                if (
                    artifacts_directory_created
                    and artifacts_directory_identity is not None
                ):
                    self._register_open_execution_created_directory(
                        self.db_path.parent / "acquisition_artifacts",
                        cleanup_root=self.db_path.parent,
                        source_descriptor=artifacts_fd,
                        expected_identity=artifacts_directory_identity,
                    )
                if attempt_directory_created and attempt_directory_identity is not None:
                    self._register_open_execution_created_directory(
                        self.db_path.parent / "acquisition_artifacts" / attempt_id,
                        cleanup_root=self.db_path.parent,
                        source_descriptor=attempt_fd,
                        expected_identity=attempt_directory_identity,
                    )
                for target, device, inode in created:
                    self.register_execution_created_path(
                        self.db_path.parent
                        / "acquisition_artifacts"
                        / attempt_id
                        / target,
                        cleanup_root=self.db_path.parent,
                        expected_identity=(device, inode),
                    )
                self.conn.execute("RELEASE inline_acquisition_artifacts")
                savepoint_started = False
            else:
                try:
                    self._commit()
                except BaseException:
                    try:
                        durable_commit = not self.conn.in_transaction
                    except BaseException:
                        durable_commit = False
                    if durable_commit:
                        created.clear()
                        pending_created = None
                        artifacts_directory_created = False
                        attempt_directory_created = False
                        self._execution_transaction_depth = 0
                        try:
                            self._release_execution_created_paths()
                        except BaseException:
                            pass
                    raise
            return [item[2] for item in staged]
        except BaseException:
            failure_active = True
            if durable_commit:
                pass
            elif savepoint_started:
                try:
                    self.conn.execute("ROLLBACK TO inline_acquisition_artifacts")
                except BaseException:
                    pass
                try:
                    self.conn.execute("RELEASE inline_acquisition_artifacts")
                except BaseException:
                    pass
            elif not nested_execution:
                try:
                    self.conn.rollback()
                except BaseException:
                    pass
            owned_paths = list(created)
            if pending_created is not None:
                owned_paths.append(pending_created)
            for target, device, inode in owned_paths:
                try:
                    self._unlink_if_identity(attempt_fd, target, device, inode)
                except BaseException:
                    pass
            if attempt_directory_created and attempt_directory_identity is not None:
                try:
                    self._rmdir_if_identity(
                        artifacts_fd, attempt_id, *attempt_directory_identity
                    )
                except BaseException:
                    pass
            if artifacts_directory_created and artifacts_directory_identity is not None:
                try:
                    self._rmdir_if_identity(
                        root_fd,
                        "acquisition_artifacts",
                        *artifacts_directory_identity,
                    )
                except BaseException:
                    pass
            raise
        finally:
            close_failure = None
            for descriptor in open_artifact_descriptors:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    if close_failure is None:
                        close_failure = exc
            for descriptor in (attempt_fd, artifacts_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException as exc:
                        if close_failure is None:
                            close_failure = exc
            if close_failure is not None and not failure_active:
                raise close_failure

    @staticmethod
    def _link_unnamed_temporary(descriptor: int, parent_fd: int, target: str) -> None:
        # procfs resolves this magic link to the already-open unnamed inode;
        # the destination remains relative to the pinned attempt directory.
        os.link(
            f"/proc/self/fd/{descriptor}",
            target,
            dst_dir_fd=parent_fd,
            follow_symlinks=True,
        )

    @staticmethod
    def _validate_portable_component(value: str) -> None:
        try:
            validated = validate_portable_relative_path(value, field_name="attempt_id")
        except ValueError:
            raise ValueError(
                "attempt_id must be one safe portable path component"
            ) from None
        if validated is None or "/" in validated:
            raise ValueError("attempt_id must be one safe portable path component")

    def _canonical_attempt_payload(
        self, supplied: AcquisitionAttempt, indexed: AcquisitionAttempt
    ) -> str:
        if indexed.authority_mode not in {"governed", "legacy_runtime"}:
            return self._compatibility_attempt_payload(indexed)
        if not supplied.canonical_json:
            raise ValueError(
                "governed acquisition attempt requires canonical JSON authority"
            )
        contract = ContractAcquisitionAttempt.model_validate_json(
            supplied.canonical_json
        )
        redacted_json = json.dumps(
            redact_persisted_value(contract.model_dump(mode="json")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        redacted = ContractAcquisitionAttempt.model_validate_json(redacted_json)
        redacted_plain = json.loads(redacted_json)
        expected = (
            indexed.attempt_id,
            indexed.request_id,
            str(indexed.scope_id),
            str(indexed.run_id),
            indexed.executor_id,
            indexed.requested_url,
            indexed.final_url,
            indexed.accepted,
        )
        actual = (
            redacted.attempt_id,
            redacted.request.request_id,
            redacted.request.scope_id,
            redacted.request.run_id,
            redacted.request.executor_id,
            str(redacted.request.url),
            str(redacted.result.final_url) if redacted.result.final_url else None,
            redacted.accepted,
        )
        if actual != expected:
            raise ValueError(
                "canonical acquisition authority conflicts with relational indexes"
            )
        request_metadata = redacted.request.metadata
        result_metadata = redacted.result.metadata
        governed_request_metadata = {
            "acquisition_fingerprint",
            "scope_fingerprint",
            "profile_id",
            "authority_mode",
            "content_kind",
            "fallback_position",
            "executor_version",
            "entrypoint",
            "script_sha256",
            "required_capabilities",
            "executor_capabilities",
            "requires_authorized_access",
            "verification_rules",
            "resource_limits",
            "quality_gates",
            "scope_budgets",
        }
        legacy_runtime_request_metadata = {
            "authority_mode",
            "content_kind",
            "fallback_position",
            "profile_id",
            "legacy_fetch_mode",
            "legacy_executor_label",
            "site_skill_lineage",
            "executor_version",
        }
        required_request_metadata = (
            governed_request_metadata
            if indexed.authority_mode == "governed"
            else legacy_runtime_request_metadata
        )
        required_result_metadata = {
            "acquisition_classification",
            "acquisition_validation",
        }
        if not required_request_metadata.issubset(request_metadata):
            raise ValueError(
                "canonical acquisition authority lacks required request metadata"
            )
        if not required_result_metadata.issubset(result_metadata):
            raise ValueError(
                "canonical acquisition authority lacks required result metadata"
            )
        canonical_validation = redacted_plain["result"]["metadata"][
            "acquisition_validation"
        ]
        semantic_expected = (
            indexed.attempt_id,
            indexed.request_id,
            indexed.requested_at,
            indexed.started_at,
            indexed.finished_at,
            indexed.classification,
            indexed.reason or indexed.classification,
            indexed.accepted,
            indexed.authority_mode,
            indexed.content_kind,
            indexed.requested_url,
            indexed.final_url,
        )
        semantic_actual = (
            redacted.attempt_id,
            redacted.request.request_id,
            redacted.request.requested_at,
            redacted.result.started_at,
            redacted.result.finished_at,
            result_metadata["acquisition_classification"],
            redacted.acceptance_reason,
            redacted.accepted,
            request_metadata["authority_mode"],
            request_metadata["content_kind"],
            str(redacted.request.url),
            str(redacted.result.final_url) if redacted.result.final_url else None,
        )
        if semantic_actual != semantic_expected:
            raise ValueError(
                "conflicting canonical acquisition authority and relational semantics"
            )
        if request_metadata["profile_id"] != indexed.profile_id:
            raise ValueError(
                "conflicting canonical acquisition authority and relational profile"
            )
        if request_metadata["fallback_position"] != indexed.position:
            raise ValueError(
                "conflicting canonical acquisition authority and relational position"
            )
        if (
            "status_code" in indexed.validation
            and redacted.result.status_code != indexed.validation.get("status_code")
        ):
            raise ValueError(
                "conflicting canonical acquisition authority and relational status"
            )
        if canonical_validation != indexed.validation:
            raise ValueError(
                "canonical acquisition authority conflicts with relational validation"
            )
        indexed_authority = (
            indexed.site_skill_id,
            indexed.site_skill_version,
            indexed.site_skill_package_sha256,
            indexed.recipe_id,
            indexed.script_sha256,
            indexed.executor_version,
            indexed.acquisition_fingerprint,
            indexed.content_kind,
        )
        canonical_authority = (
            redacted.request.site_skill_id,
            redacted.request.site_skill_version,
            redacted.request.site_skill_digest,
            redacted.request.recipe_id,
            request_metadata.get("script_sha256"),
            request_metadata.get("executor_version"),
            request_metadata.get("acquisition_fingerprint"),
            request_metadata.get("content_kind"),
        )
        if (
            any(value is not None for value in indexed_authority)
            and indexed_authority != canonical_authority
        ):
            raise ValueError(
                "canonical acquisition authority conflicts with governed indexes"
            )
        return json.dumps(
            redacted.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _compatibility_attempt_payload(attempt: AcquisitionAttempt) -> str:
        """Keep non-governed lineage useful without claiming the frozen governed contract."""
        return json.dumps(
            redact_persisted_value(
                {
                    "schema_version": "acquisition-attempt-compatibility.v1",
                    "authority_mode": attempt.authority_mode,
                    "attempt": attempt.model_dump(
                        mode="json", exclude={"canonical_json", "artifacts"}
                    ),
                }
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _govern_artifact_bytes(kind: str, mime: str, data: bytes) -> tuple[bytes, str]:
        Storage._verify_artifact_mime(mime, data)
        if mime == "application/json":
            try:
                decoded = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(
                    "textual acquisition artifact is not valid governed JSON"
                ) from None
            sanitized = redact_persisted_value(decoded)
            return (
                json.dumps(
                    sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode(),
                "structurally_redacted",
            )
        if mime == "text/html":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(
                    "textual acquisition artifact is not valid UTF-8"
                ) from None
            text = re.sub(
                r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s<]+",
                r"\1[REDACTED]",
                text,
            )
            text = redact_persisted_value(text)
            return str(text).encode(), "structurally_redacted"
        if kind in {"trace", "raw_capture"}:
            raise ValueError(
                "opaque trace or raw capture cannot be verified for redaction"
            )
        return data, "opaque_unverified"

    @staticmethod
    def _verify_artifact_mime(mime: str, data: bytes) -> None:
        valid = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": (
                len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            ),
            "application/zip": data.startswith(
                (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
            ),
            "application/json": True,
            "text/html": True,
            "application/octet-stream": True,
        }
        if not valid.get(mime, False):
            raise ValueError("inline artifact bytes do not match declared MIME")

    @staticmethod
    def _unlink_if_identity(
        parent_fd: int | None, target: str, device: int, inode: int
    ) -> None:
        if parent_fd is None:
            return
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = None
        try:
            descriptor = os.open(target, flags, dir_fd=parent_fd)
            info = os.fstat(descriptor)
            named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (info.st_dev, info.st_ino)
                == (device, inode)
                == (named.st_dev, named.st_ino)
            ):
                os.unlink(target, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _rmdir_if_identity(
        parent_fd: int | None, target: str, device: int, inode: int
    ) -> None:
        if parent_fd is None:
            return
        try:
            named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) == (device, inode):
                os.rmdir(target, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            return

    def _verify_pinned_artifact_directories(
        self, root_fd: int, artifacts_fd: int, attempt_fd: int, attempt_id: str
    ) -> None:
        paths = (
            self.db_path.parent,
            self.db_path.parent / "acquisition_artifacts",
            self.db_path.parent / "acquisition_artifacts" / attempt_id,
        )
        for descriptor, path in zip((root_fd, artifacts_fd, attempt_fd), paths):
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                raise ValueError("artifact parent changed during publication") from None
            pinned = os.fstat(descriptor)
            if not stat.S_ISDIR(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (pinned.st_dev, pinned.st_ino):
                raise ValueError("artifact parent changed during publication")

    @staticmethod
    def _verify_existing_artifact(
        parent_fd: int, target: str, artifact: AcquisitionArtifact
    ) -> None:
        digest = hashlib.sha256()
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(target, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(
                "existing acquisition artifact is symlinked or nonregular"
            ) from exc
        stream = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("existing acquisition artifact is not a regular file")
            if info.st_size != artifact.size_bytes:
                raise ValueError("conflicting acquisition artifact bytes")
            stream = os.fdopen(descriptor, "rb", closefd=False)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            try:
                named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(
                    "existing acquisition artifact changed during verification"
                ) from exc
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError(
                    "existing acquisition artifact changed during verification"
                )
        finally:
            if stream is not None:
                stream.close()
            os.close(descriptor)
        if digest.hexdigest() != artifact.sha256:
            raise ValueError("conflicting acquisition artifact bytes")

    def get_acquisition_attempt(self, attempt_id: str) -> Optional[AcquisitionAttempt]:
        row = self.conn.execute(
            "SELECT * FROM acquisition_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._row_to_acquisition_attempt(row) if row else None

    def list_acquisition_attempts(
        self, scope_id: int, run_id: int
    ) -> List[AcquisitionAttempt]:
        rows = self.conn.execute(
            """SELECT * FROM acquisition_attempts WHERE scope_id=? AND run_id=?
               ORDER BY requested_at, COALESCE(started_at, requested_at),
                        COALESCE(finished_at, started_at, requested_at), request_id, position, attempt_id""",
            (scope_id, run_id),
        ).fetchall()
        return [self._row_to_acquisition_attempt(row) for row in rows]

    def _row_to_acquisition_attempt(self, row) -> AcquisitionAttempt:
        artifacts = [
            AcquisitionArtifact(**dict(item))
            for item in self.conn.execute(
                "SELECT * FROM acquisition_artifacts WHERE attempt_id=? ORDER BY kind, portable_path",
                (row["attempt_id"],),
            ).fetchall()
        ]
        return AcquisitionAttempt(
            attempt_id=row["attempt_id"],
            request_id=row["request_id"],
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            position=row["position"],
            content_kind=row["content_kind"],
            profile_id=row["profile_id"],
            site_skill_id=row["site_skill_id"],
            site_skill_version=row["site_skill_version"],
            site_skill_package_sha256=row["site_skill_package_sha256"],
            recipe_id=row["recipe_id"],
            script_sha256=row["script_sha256"],
            executor_id=row["executor_id"],
            executor_version=row["executor_version"],
            requested_url=row["requested_url"],
            final_url=row["final_url"],
            requested_at=_parse_dt(row["requested_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            acquisition_fingerprint=row["acquisition_fingerprint"],
            classification=row["classification"],
            accepted=bool(row["accepted"]),
            reason=row["reason"] or "",
            validation=json.loads(row["validation_json"] or "{}"),
            canonical_json=row["canonical_json"],
            redaction_status=row["redaction_status"],
            authority_mode=row["authority_mode"],
            artifacts=artifacts,
        )

    def _validate_accepted_attempt(
        self,
        attempt_id: Optional[str],
        scope_id: int,
        run_id: int,
        content_kind: str,
    ) -> None:
        if attempt_id is None:
            raise ValueError(
                "new tracked state requires a non-null accepted acquisition attempt"
            )
        row = self.conn.execute(
            "SELECT accepted, scope_id, run_id, content_kind FROM acquisition_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if (
            row is None
            or not row["accepted"]
            or row["scope_id"] != scope_id
            or row["run_id"] != run_id
            or row["content_kind"] != content_kind
        ):
            raise ValueError(
                f"tracked state requires an accepted acquisition attempt with content_kind={content_kind} "
                "for the same run and scope"
            )
