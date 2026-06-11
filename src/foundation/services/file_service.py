"""Workspace file service for v3 Stage 2 file capabilities."""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import NoReturn

from foundation.models.file import (
    FileApplyDiffRequest,
    FileEditRequest,
    FileErrorCode,
    FileMutationResult,
    FileOperationError,
    FileReadChunkRequest,
    FileReadChunkResult,
    FileReadRequest,
    FileReadResult,
    FileServiceError,
    FileWriteRequest,
)
from foundation.services.scope_grants import ScopeGrantStore
from foundation.services.staging import WorkspaceRewriteStager

_MAX_READ_BYTES = 256 * 1024  # 256 KB
_BOM = b"\xef\xbb\xbf"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _raise(
    code: FileErrorCode,
    message: str,
    *,
    detail: str | None = None,
    path: str | None = None,
    suggestion: str | None = None,
) -> NoReturn:
    raise FileServiceError(
        FileOperationError(
            code=code,
            message=message,
            detail=detail,
            path=path,
            suggestion=suggestion,
        )
    )


def _sibling_hint(resolved: Path) -> str:
    """List the entries in a missing file's parent dir, as a 'did you mean' hint."""
    parent = resolved.parent
    try:
        if not parent.is_dir():
            return ""
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in parent.iterdir())
    except OSError:
        return ""
    if not names:
        return ""
    shown = names[:12]
    more = f" (+{len(names) - 12} more)" if len(names) > 12 else ""
    return f" Directory '{parent.name}/' contains: {', '.join(shown)}{more}."


def _raise_not_found(raw_path: str, resolved: Path) -> None:
    """Raise FILE_NOT_FOUND with a sibling listing so the model can self-correct."""
    hint = _sibling_hint(resolved)
    _raise(
        FileErrorCode.FILE_NOT_FOUND,
        f"File does not exist: {raw_path}.{hint}",
        path=raw_path,
        suggestion=(
            "Discover the correct path with foundation.files, or read one of the "
            "files listed in the message."
            if hint
            else None
        ),
    )


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _line_count(content: str) -> int:
    if not content:
        return 0
    return content.count("\n") + (1 if not content.endswith("\n") else 0)


def _diff_summary(old_content: str | None, new_content: str) -> str:
    if old_content is None:
        return f"new file, {_line_count(new_content)} lines"
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    added = 0
    removed = 0
    import difflib

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            removed += i2 - i1
            added += j2 - j1
    return f"+{added} -{removed} lines"


# ---------------------------------------------------------------------------
# Unified diff applier
# ---------------------------------------------------------------------------


def _norm_line(line: str) -> str:
    return line.rstrip("\n\r")


def _parse_and_apply_diff(
    original: str, diff_text: str, *, file_path: str
) -> tuple[str, list[str]]:
    """Parse a unified diff and apply it atomically to *original*.

    Returns ``(new_content, leniency_notes)``. The notes name every tolerance
    that was exercised so callers can surface them in the execution artifact.

    Deliberate tolerances for model-generated diffs (each recorded when used):
    - body lines without a leading ``+``/``-``/space are treated as context;
    - a hunk that only matches after trailing-newline normalization (CRLF vs
      LF) is accepted as a fallback when the exact match fails.

    Never-valid shapes are rejected at parse time: hunks whose declared
    source-line count disagrees with their body, hunks containing no
    additions or removals, rename-style and delete-only diffs.

    Raises FileServiceError on malformed diffs, context mismatches, or
    policy violations (delete-only, rename-style).
    """
    diff_lines = diff_text.splitlines(keepends=True)

    # Strip file headers (--- / +++ lines) if present
    header_old: str | None = None
    header_new: str | None = None
    body_start = 0
    for i, line in enumerate(diff_lines):
        stripped = line.rstrip("\n\r")
        if stripped.startswith("--- "):
            header_old = stripped[4:].split("\t")[0].strip()
            body_start = i + 1
        elif stripped.startswith("+++ "):
            header_new = stripped[4:].split("\t")[0].strip()
            body_start = i + 1
            break
        elif stripped.startswith("@@"):
            break

    # Reject rename-style diffs where source and target differ
    if header_old is not None and header_new is not None:
        # Normalise a/ b/ prefixes
        norm_old = re.sub(r"^[ab]/", "", header_old)
        norm_new = re.sub(r"^[ab]/", "", header_new)
        if norm_old != norm_new and norm_old != "/dev/null" and norm_new != "/dev/null":
            _raise(
                FileErrorCode.DIFF_REJECTED,
                "Rename-style diffs are not supported by file.apply_diff.",
                path=file_path,
            )

    # Parse hunks
    hunks: list[tuple[int, int, list[str]]] = []  # (old_start, old_count, hunk_lines)
    current_start: int | None = None
    current_count = 0
    current_lines: list[str] = []

    for line in diff_lines[body_start:]:
        m = _HUNK_RE.match(line)
        if m:
            if current_start is not None:
                hunks.append((current_start, current_count, current_lines))
            current_start = int(m.group(1))
            current_count = int(m.group(2)) if m.group(2) is not None else 1
            current_lines = []
            continue
        if current_start is None:
            continue
        # Skip "\ No newline at end of file" markers
        if line.startswith("\\ "):
            continue
        current_lines.append(line)

    if current_start is not None:
        hunks.append((current_start, current_count, current_lines))

    if not hunks:
        _raise(
            FileErrorCode.DIFF_REJECTED,
            "Diff contains no hunks to apply.",
            path=file_path,
        )

    # Parse-time validation: each hunk's body must agree with its declared
    # source-line count and actually change something.
    leniency_notes: list[str] = []
    for hunk_idx, (_old_start, old_count, hunk_lines) in enumerate(hunks):
        old_side_lines = 0
        has_change = False
        bare_lines = 0
        for hl in hunk_lines:
            if hl.startswith("+"):
                has_change = True
            elif hl.startswith("-"):
                has_change = True
                old_side_lines += 1
            elif hl.startswith(" "):
                old_side_lines += 1
            else:
                bare_lines += 1
                old_side_lines += 1
        if not has_change:
            _raise(
                FileErrorCode.DIFF_REJECTED,
                f"Hunk {hunk_idx + 1} contains no additions or removals.",
                path=file_path,
            )
        if old_side_lines != old_count:
            _raise(
                FileErrorCode.DIFF_REJECTED,
                f"Hunk {hunk_idx + 1} declares {old_count} source lines "
                f"but its body has {old_side_lines}.",
                path=file_path,
            )
        if bare_lines:
            leniency_notes.append(
                f"hunk {hunk_idx + 1}: {bare_lines} line(s) without a diff "
                "prefix treated as context"
            )

    # Reject delete-only diffs (all hunks contain only removals, no additions)
    has_addition = False
    for _, _, hunk_lines in hunks:
        for hl in hunk_lines:
            if hl.startswith("+"):
                has_addition = True
                break
        if has_addition:
            break
    if not has_addition:
        _raise(
            FileErrorCode.DIFF_REJECTED,
            "Delete-only diffs are not supported by file.apply_diff.",
            path=file_path,
        )

    # Validate all hunks against the original before applying any
    original_lines = original.splitlines(keepends=True)

    for hunk_idx, (old_start, _old_count, hunk_lines) in enumerate(hunks):
        # Extract context and deletion lines expected from original
        expected: list[str] = []
        for hl in hunk_lines:
            if hl.startswith("+"):
                continue
            # Context line (starts with " ") or deletion line (starts with "-")
            if hl.startswith("-"):
                expected.append(hl[1:])
            elif hl.startswith(" "):
                expected.append(hl[1:])
            else:
                # Bare line without prefix — treat as context
                expected.append(hl)

        # old_start is 1-indexed
        src_start = old_start - 1
        src_slice = original_lines[src_start : src_start + len(expected)]

        if len(src_slice) != len(expected):
            _raise(
                FileErrorCode.DIFF_APPLY_FAILED,
                f"Hunk {hunk_idx + 1} does not match the source file at line {old_start}.",
                path=file_path,
            )
        if all(a == b for a, b in zip(src_slice, expected, strict=True)):
            continue
        # Fallback: accept the hunk when only trailing newlines (CRLF vs LF,
        # missing final newline) differ — but say so.
        if all(_norm_line(a) == _norm_line(b) for a, b in zip(src_slice, expected, strict=True)):
            leniency_notes.append(
                f"hunk {hunk_idx + 1} matched only after trailing-newline normalization"
            )
            continue
        _raise(
            FileErrorCode.DIFF_APPLY_FAILED,
            f"Hunk {hunk_idx + 1} does not match the source file at line {old_start}.",
            path=file_path,
        )

    # Apply hunks in reverse order to preserve line indices
    result_lines = list(original_lines)
    for old_start, _old_count, hunk_lines in reversed(hunks):
        new_lines: list[str] = []
        remove_count = 0
        for hl in hunk_lines:
            if hl.startswith("-"):
                remove_count += 1
            elif hl.startswith("+"):
                new_lines.append(hl[1:])
            elif hl.startswith(" "):
                new_lines.append(hl[1:])
                remove_count += 1
            else:
                new_lines.append(hl)
                remove_count += 1

        src_start = old_start - 1
        result_lines[src_start : src_start + remove_count] = new_lines

    return "".join(result_lines), leniency_notes


# ---------------------------------------------------------------------------
# FileService
# ---------------------------------------------------------------------------


class FileService:
    """Workspace-bound text file operations for v3 file capabilities."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        state_dir: Path,
        read_grant_store: ScopeGrantStore | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._read_grant_store = read_grant_store
        self._stager = WorkspaceRewriteStager(
            workspace_root=self._workspace_root,
            state_dir=state_dir,
        )

    # -- path resolution ----------------------------------------------------

    def _resolve_path(self, raw_path: str) -> Path:
        """Resolve a user-provided path and enforce workspace containment."""
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self._workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            _raise(
                FileErrorCode.PATH_OUTSIDE_WORKSPACE,
                "Path escapes the workspace boundary.",
                path=raw_path,
            )
        return resolved

    def _resolve_read_path(self, raw_path: str) -> Path:
        """Resolve a read path, also allowing session-granted out-of-scope roots.

        Reads may target the workspace or any directory the user approved via a
        scope escalation. Writes never use this — they stay workspace-confined.
        """
        candidate = Path(raw_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._workspace_root / candidate).resolve()
        )
        try:
            resolved.relative_to(self._workspace_root)
            return resolved
        except ValueError:
            pass
        if self._read_grant_store is not None and self._read_grant_store.is_granted(resolved):
            return resolved
        _raise(
            FileErrorCode.PATH_OUTSIDE_WORKSPACE,
            "Path escapes the workspace boundary.",
            path=raw_path,
        )

    # -- raw I/O helpers ----------------------------------------------------

    def _read_raw(self, resolved: Path) -> tuple[str, str]:
        """Read bytes, detect encoding, and return (content, encoding).

        Rejects non-text (NUL bytes) and non-UTF-8 encodings.
        """
        raw = resolved.read_bytes()
        if b"\x00" in raw:
            _raise(
                FileErrorCode.NOT_TEXT,
                "File contains NUL bytes and appears to be binary.",
                path=str(resolved),
            )
        if raw.startswith(_BOM):
            try:
                content = raw[3:].decode("utf-8")
            except UnicodeDecodeError:
                _raise(
                    FileErrorCode.UNSUPPORTED_ENCODING,
                    "File has a UTF-8 BOM but contains invalid UTF-8 sequences.",
                    path=str(resolved),
                )
            return content, "utf-8-sig"
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            _raise(
                FileErrorCode.UNSUPPORTED_ENCODING,
                "File is not valid UTF-8. Only UTF-8 and UTF-8 with BOM are supported.",
                path=str(resolved),
            )
        return content, "utf-8"

    # -- atomic write -------------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> None:
        """Stage content to a temp file and atomically replace the target."""
        original_mode: int | None = None
        if target.exists():
            original_mode = stat.S_IMODE(target.stat().st_mode)

        staged = self._stager.stage_text(target_path=target, content=content)
        self._stager.commit(staged)

        if original_mode is not None:
            target.chmod(original_mode)

    # -- public operations --------------------------------------------------

    def read(self, request: FileReadRequest) -> FileReadResult:
        """Read one workspace text file up to 256 KB."""
        resolved = self._resolve_read_path(request.path)
        if not resolved.exists():
            _raise_not_found(request.path, resolved)
        file_size = resolved.stat().st_size
        if file_size > _MAX_READ_BYTES:
            _raise(
                FileErrorCode.FILE_TOO_LARGE,
                f"File is {file_size} bytes, exceeding the 256 KB limit.",
                path=request.path,
                suggestion="Use foundation.file.read_chunk to read large files in chunks.",
            )
        content, encoding = self._read_raw(resolved)
        return FileReadResult(
            path=str(resolved),
            content=content,
            encoding=encoding,
            line_count=_line_count(content),
            size_bytes=len(content.encode("utf-8")),
            sha256=_sha256(content),
        )

    def read_chunk(self, request: FileReadChunkRequest) -> FileReadChunkResult:
        """Read a line-based chunk from a workspace text file."""
        resolved = self._resolve_read_path(request.path)
        if not resolved.exists():
            _raise_not_found(request.path, resolved)

        # Peek at the first 8 KB for binary detection
        raw_head = resolved.read_bytes()[:8192]
        if b"\x00" in raw_head:
            _raise(
                FileErrorCode.NOT_TEXT,
                "File contains NUL bytes and appears to be binary.",
                path=request.path,
            )

        # Detect encoding from first bytes
        full_bytes = resolved.read_bytes()
        if full_bytes.startswith(_BOM):
            encoding = "utf-8-sig"
            decode_bytes = full_bytes[3:]
        else:
            encoding = "utf-8"
            decode_bytes = full_bytes

        try:
            full_text = decode_bytes.decode("utf-8")
        except UnicodeDecodeError:
            _raise(
                FileErrorCode.UNSUPPORTED_ENCODING,
                "File is not valid UTF-8. Only UTF-8 and UTF-8 with BOM are supported.",
                path=request.path,
            )

        all_lines = full_text.splitlines(keepends=True)
        total_lines = len(all_lines)
        file_sha256 = _sha256(full_text)

        # Extract the requested chunk (start_line is 1-indexed)
        start_idx = request.start_line - 1
        if start_idx >= total_lines:
            return FileReadChunkResult(
                path=str(resolved),
                content="",
                encoding=encoding,
                start_line=request.start_line,
                end_line=0,
                total_lines=total_lines,
                sha256=file_sha256,
            )

        end_idx = min(start_idx + request.max_lines, total_lines)
        chunk = "".join(all_lines[start_idx:end_idx])
        return FileReadChunkResult(
            path=str(resolved),
            content=chunk,
            encoding=encoding,
            start_line=request.start_line,
            end_line=end_idx,
            total_lines=total_lines,
            sha256=file_sha256,
        )

    def write(self, request: FileWriteRequest) -> FileMutationResult:
        """Create a new file or overwrite an existing one."""
        resolved = self._resolve_path(request.path)
        exists = resolved.exists()
        if exists and not request.overwrite:
            _raise(
                FileErrorCode.FILE_EXISTS,
                "File already exists. Set overwrite=true to replace it.",
                path=request.path,
            )
        old_content: str | None = None
        if exists:
            old_content, _ = self._read_raw(resolved)
        self._atomic_write(resolved, request.content)
        return FileMutationResult(
            path=str(resolved),
            sha256=_sha256(request.content),
            line_count=_line_count(request.content),
            size_bytes=len(request.content.encode("utf-8")),
            diff_summary=_diff_summary(old_content, request.content),
            created=not exists,
        )

    def edit(self, request: FileEditRequest) -> FileMutationResult:
        """Rewrite an existing file with conflict detection."""
        resolved = self._resolve_path(request.path)
        if not resolved.exists():
            _raise_not_found(request.path, resolved)
        old_content, _ = self._read_raw(resolved)
        actual_sha256 = _sha256(old_content)
        if actual_sha256 != request.expected_sha256:
            _raise(
                FileErrorCode.SHA256_CONFLICT,
                "File content has changed since it was last read.",
                detail=f"Expected {request.expected_sha256}, got {actual_sha256}.",
                path=request.path,
            )
        self._atomic_write(resolved, request.content)
        return FileMutationResult(
            path=str(resolved),
            sha256=_sha256(request.content),
            line_count=_line_count(request.content),
            size_bytes=len(request.content.encode("utf-8")),
            diff_summary=_diff_summary(old_content, request.content),
        )

    def apply_diff(self, request: FileApplyDiffRequest) -> FileMutationResult:
        """Apply a unified diff atomically to a workspace text file."""
        resolved = self._resolve_path(request.path)
        if not resolved.exists():
            _raise_not_found(request.path, resolved)
        old_content, _ = self._read_raw(resolved)
        new_content, leniency_notes = _parse_and_apply_diff(
            old_content,
            request.diff,
            file_path=request.path,
        )
        self._atomic_write(resolved, new_content)
        return FileMutationResult(
            path=str(resolved),
            sha256=_sha256(new_content),
            line_count=_line_count(new_content),
            size_bytes=len(new_content.encode("utf-8")),
            diff_summary=_diff_summary(old_content, new_content),
            leniency_notes=leniency_notes,
        )
