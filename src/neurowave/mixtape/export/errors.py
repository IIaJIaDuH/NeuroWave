"""Стабильные ошибки USB writer для отчёта и CLI --json."""

from __future__ import annotations

from enum import Enum


class ExportErrorCode(str, Enum):
    TARGET_MISSING = "target_missing"
    UNSAFE_PATH = "unsafe_path"
    PATH_ESCAPE = "path_escape"
    COLLISION = "collision"
    EXISTS = "exists"
    SOURCE_MISSING = "source_missing"
    SOURCE_CHANGED = "source_changed"
    RIGHTS = "rights_denied"
    ENOSPC = "no_space"
    READ_ONLY = "read_only"
    IO = "io_error"
    VERIFY = "verify_failed"
    MANIFEST_INVALID = "manifest_invalid"
    MANIFEST_EXISTS = "manifest_exists"
    FOREIGN_MANIFEST = "foreign_manifest"
    DEVICE = "device_unverified"


class ExportError(Exception):
    def __init__(self, code: ExportErrorCode, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path
