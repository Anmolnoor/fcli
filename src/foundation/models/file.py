"""Typed file capability models for v3 Stage 2."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class FileErrorCode(StrEnum):
    """Machine-readable error codes for file capability failures."""

    FILE_TOO_LARGE = "file_too_large"
    NOT_TEXT = "not_text"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    FILE_NOT_FOUND = "file_not_found"
    FILE_EXISTS = "file_exists"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    SHA256_CONFLICT = "sha256_conflict"
    DIFF_REJECTED = "diff_rejected"
    DIFF_APPLY_FAILED = "diff_apply_failed"
    PERMISSION_ERROR = "permission_error"


class FileOperationError(StrictModel):
    """Structured error returned by file capability failures."""

    code: FileErrorCode
    message: str = Field(min_length=1)
    detail: str | None = None
    path: str | None = None
    suggestion: str | None = None


class FileServiceError(RuntimeError):
    """Runtime exception wrapping a structured FileOperationError."""

    def __init__(self, error: FileOperationError) -> None:
        super().__init__(error.message)
        self.error = error


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FileReadRequest(StrictModel):
    """Read one workspace text file up to 256 KB."""

    path: str = Field(min_length=1)


class FileReadChunkRequest(StrictModel):
    """Read a line-based chunk from a workspace text file."""

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=400)


class FileWriteRequest(StrictModel):
    """Create a new file or overwrite an existing one."""

    path: str = Field(min_length=1)
    content: str = ""
    overwrite: bool = False


class FileEditRequest(StrictModel):
    """Rewrite an existing file with conflict detection."""

    path: str = Field(min_length=1)
    content: str = ""
    expected_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("expected_sha256")
    @classmethod
    def _normalize_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("expected_sha256 must be a 64-character lowercase hex string.")
        return normalized


class FileApplyDiffRequest(StrictModel):
    """Apply a unified diff to a workspace text file."""

    path: str = Field(min_length=1)
    diff: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

class FileReadResult(StrictModel):
    """Result of reading a full workspace text file."""

    path: str
    content: str
    encoding: str
    line_count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class FileReadChunkResult(StrictModel):
    """Result of reading a line-based chunk from a workspace text file."""

    path: str
    content: str
    encoding: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class FileMutationResult(StrictModel):
    """Result of a file write, edit, or apply-diff operation."""

    path: str
    sha256: str = Field(min_length=64, max_length=64)
    line_count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    diff_summary: str
    created: bool = False
