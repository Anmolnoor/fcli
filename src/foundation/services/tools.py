"""Typed local-context tooling wrappers for Foundation CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pathspec import PathSpec
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

logger = logging.getLogger("foundation.services.tools")

_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_CAPTURE_LIMIT_KB = 256
_SNIPPET_LENGTH = 240
_IGNORE_FILE_NAMES = (".gitignore", ".ignore", ".fdignore", ".rgignore")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ToolAvailabilityStatus(StrEnum):
    """Availability states for external tool binaries."""

    AVAILABLE = "available"
    MISSING = "missing"


class ToolErrorCode(StrEnum):
    """Normalized tool failure codes."""

    EXECUTION_FAILED = "execution_failed"
    INVALID_OUTPUT = "invalid_output"
    INVALID_SCOPE = "invalid_scope"
    MISSING_BINARY = "missing_binary"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"


class HelpLookupSource(StrEnum):
    """Supported local help sources."""

    MAN = "man"
    TLDR = "tldr"


class FileDiscoveryType(StrEnum):
    """Supported file discovery filters."""

    ANY = "any"
    FILE = "file"
    DIRECTORY = "directory"


class ToolBinaryStatus(BaseModel):
    """Availability details for one external binary capability."""

    name: str
    candidates: list[str] = Field(default_factory=list)
    status: ToolAvailabilityStatus
    required: bool = False
    resolved_command: str | None = None
    path: Path | None = None
    install_hint: str | None = None


class ToolError(BaseModel):
    """CLI-friendly and model-friendly normalized tool error."""

    code: ToolErrorCode
    tool: str
    message: str
    detail: str | None = None
    command: list[str] = Field(default_factory=list)
    install_hint: str | None = None


class SearchRequest(BaseModel):
    """Typed request for ripgrep-backed content search."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    scope: Path | None = None
    max_results: PositiveInt = 50
    case_sensitive: bool = False

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()


class SearchMatch(BaseModel):
    """One normalized content-search hit."""

    path: str
    line_number: PositiveInt
    column_number: PositiveInt
    line_text: str


class SearchResult(BaseModel):
    """Normalized content-search result set."""

    tool: str = "rg"
    query: str
    scope: str
    matches: list[SearchMatch] = Field(default_factory=list)
    truncated: bool = False


class FileDiscoveryRequest(BaseModel):
    """Typed request for fd-backed path discovery."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)
    scope: Path | None = None
    file_type: FileDiscoveryType = FileDiscoveryType.ANY
    max_results: PositiveInt = 100

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()


class FileDiscoveryResult(BaseModel):
    """Normalized path-discovery result set."""

    tool: str = "fd"
    pattern: str
    scope: str
    file_type: FileDiscoveryType
    paths: list[str] = Field(default_factory=list)
    truncated: bool = False


class GitContextRequest(BaseModel):
    """Typed request for repository context."""

    model_config = ConfigDict(extra="forbid")

    scope: Path | None = None
    max_status_entries: PositiveInt = 100
    max_recent_commits: PositiveInt = 5

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()


class GitStatusEntry(BaseModel):
    """One parsed `git status --short` entry."""

    path: str
    index_status: str
    worktree_status: str
    raw: str


class GitDiffStat(BaseModel):
    """One parsed `git diff --numstat` entry."""

    path: str
    additions: int | None
    deletions: int | None
    binary: bool = False


class GitCommitSummary(BaseModel):
    """One parsed recent commit summary."""

    commit_sha: str
    short_sha: str
    summary: str


class GitContextResult(BaseModel):
    """Normalized repository context."""

    tool: str = "git"
    scope: str
    branch: str
    status: list[GitStatusEntry] = Field(default_factory=list)
    unstaged_diff: list[GitDiffStat] = Field(default_factory=list)
    staged_diff: list[GitDiffStat] = Field(default_factory=list)
    recent_commits: list[GitCommitSummary] = Field(default_factory=list)
    truncated_status: bool = False


class HelpLookupRequest(BaseModel):
    """Typed request for manual-page or TLDR lookup."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    source: HelpLookupSource
    max_characters: PositiveInt = 8000


class HelpLookupResult(BaseModel):
    """Normalized local help lookup result."""

    tool: str
    source: HelpLookupSource
    topic: str
    content: str
    truncated: bool = False


class ToolExecutionError(RuntimeError):
    """Raised when a tool invocation fails in a normalized way."""

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True, slots=True)
class _BinarySpec:
    name: str
    candidates: tuple[str, ...]
    install_hint: str
    required: bool


_BINARY_SPECS: tuple[_BinarySpec, ...] = (
    _BinarySpec(
        name="rg",
        candidates=("rg",),
        install_hint="Install ripgrep with `brew install ripgrep`.",
        required=True,
    ),
    _BinarySpec(
        name="fd",
        candidates=("fd", "fdfind"),
        install_hint="Install fd with `brew install fd`.",
        required=False,
    ),
    _BinarySpec(
        name="git",
        candidates=("git",),
        install_hint="Install Git via Xcode Command Line Tools or `brew install git`.",
        required=True,
    ),
    _BinarySpec(
        name="man",
        candidates=("man",),
        install_hint="Install the system manual-page tools via Xcode Command Line Tools.",
        required=False,
    ),
    _BinarySpec(
        name="tldr",
        candidates=("tldr", "tealdeer"),
        install_hint="Install TLDR support with `brew install tealdeer`.",
        required=False,
    ),
)


def _truncate_text(value: str, *, limit: int = _SNIPPET_LENGTH) -> str:
    text = value.rstrip("\n")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _strip_overstrikes(text: str) -> str:
    while True:
        cleaned = re.sub(r".\x08", "", text)
        if cleaned == text:
            return text
        text = cleaned


def _clean_help_text(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", _strip_overstrikes(text)).strip()


class WorkspacePathFilter:
    """Shared ignore-rule matcher for file-oriented tooling."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._spec = PathSpec.from_lines("gitignore", self._load_patterns())

    def is_ignored(self, candidate: str | Path) -> bool:
        relative_path = self._relative_posix(candidate)
        if relative_path is None:
            return False
        return self._spec.match_file(relative_path) or self._spec.match_file(f"{relative_path}/")

    def _relative_posix(self, candidate: str | Path) -> str | None:
        raw_path = Path(candidate)
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        else:
            resolved = (self._workspace_root / raw_path).resolve()
        try:
            relative = resolved.relative_to(self._workspace_root)
        except ValueError:
            return None
        if not relative.parts:
            return "."
        return relative.as_posix()

    def _load_patterns(self) -> list[str]:
        patterns: list[str] = []
        seen: set[Path] = set()
        for name in _IGNORE_FILE_NAMES:
            for ignore_path in self._workspace_root.rglob(name):
                if ".git" in ignore_path.parts:
                    continue
                seen.add(ignore_path)

        git_exclude = self._workspace_root / ".git" / "info" / "exclude"
        if git_exclude.exists():
            seen.add(git_exclude)

        for ignore_path in sorted(seen):
            try:
                lines = ignore_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            if ignore_path == git_exclude:
                patterns.extend(lines)
                continue
            relative_parent = ignore_path.parent.relative_to(self._workspace_root)
            patterns.extend(self._rewrite_patterns(lines, base_dir=relative_parent))
        return patterns

    def _rewrite_patterns(self, lines: Iterable[str], *, base_dir: Path) -> list[str]:
        if base_dir == Path("."):
            return list(lines)

        base = PurePosixPath(base_dir.as_posix())
        base_prefix = base.as_posix()
        rewritten: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            negated = stripped.startswith("!")
            pattern = stripped[1:] if negated else stripped
            marker = "!" if negated else ""
            if pattern.startswith("/"):
                rewritten.append(f"{marker}{base_prefix}/{pattern.lstrip('/')}")
                continue
            if "/" in pattern.rstrip("/"):
                rewritten.append(f"{marker}{base_prefix}/{pattern}")
                continue
            rewritten.append(f"{marker}{base_prefix}/**/{pattern}")
        return rewritten


class LocalToolService:
    """Wrapper around local context tools with typed results and normalized failures."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        capture_limit_kb: int = _DEFAULT_CAPTURE_LIMIT_KB,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._default_timeout_seconds = default_timeout_seconds
        self._capture_limit_bytes = capture_limit_kb * 1024
        self._environment = os.environ.copy()
        if environment is not None:
            self._environment.update(environment)
        self._binary_status = {spec.name: self._detect_binary(spec) for spec in _BINARY_SPECS}

    def availability_report(self) -> list[ToolBinaryStatus]:
        """Return availability details for all supported tool binaries."""
        return [self._binary_status[spec.name] for spec in _BINARY_SPECS]

    def search(self, request: SearchRequest) -> SearchResult:
        """Run ripgrep content search with normalized parsing and filtering."""
        scope = self._resolve_scope(request.scope)
        binary = self._require_binary("rg")
        case_flag = "--case-sensitive" if request.case_sensitive else "--smart-case"
        scope_argument = self._path_argument(scope)
        command = [
            binary.resolved_command or "rg",
            "--json",
            "--line-number",
            case_flag,
            "--",
            request.query,
            scope_argument,
        ]
        logger.info("tool_search_started query=%s scope=%s", request.query, scope)
        matches: list[SearchMatch] = []
        truncated = False
        started_at = time.monotonic()
        process = self._spawn(command)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                payload = self._parse_json_line(line, tool="rg", command=command)
                if payload.get("type") != "match":
                    continue
                match = self._parse_rg_match(payload)
                if self._path_filter().is_ignored(match.path):
                    continue
                matches.append(match)
                if len(matches) >= request.max_results:
                    truncated = True
                    self._terminate_process(process)
                    break
            stderr = self._finish_process(
                process,
                tool="rg",
                command=command,
                started_at=started_at,
                accepted_exit_codes=(0, 1),
                ignore_failure=truncated,
            )
        finally:
            self._ensure_reaped(process)
        if stderr:
            logger.debug("tool_search_stderr stderr=%s", stderr.strip())
        logger.info(
            "tool_search_finished query=%s scope=%s matches=%s truncated=%s",
            request.query,
            scope,
            len(matches),
            truncated,
        )
        return SearchResult(
            query=request.query,
            scope=self._display_scope(scope),
            matches=matches,
            truncated=truncated,
        )

    def discover_files(self, request: FileDiscoveryRequest) -> FileDiscoveryResult:
        """Run fd-backed path discovery with shared ignore filtering."""
        scope = self._resolve_scope(request.scope)
        binary = self._require_binary("fd")
        command = [
            binary.resolved_command or "fd",
            "--color",
            "never",
        ]
        if request.file_type is FileDiscoveryType.FILE:
            command.extend(["--type", "f"])
        elif request.file_type is FileDiscoveryType.DIRECTORY:
            command.extend(["--type", "d"])
        command.extend([request.pattern, self._path_argument(scope)])
        logger.info(
            "tool_discover_started pattern=%s scope=%s file_type=%s",
            request.pattern,
            scope,
            request.file_type.value,
        )
        paths: list[str] = []
        truncated = False
        started_at = time.monotonic()
        process = self._spawn(command)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                relative_path = self._normalize_output_path(
                    line.rstrip("\n"),
                    cwd=self._workspace_root,
                )
                if not relative_path or self._path_filter().is_ignored(relative_path):
                    continue
                paths.append(relative_path)
                if len(paths) >= request.max_results:
                    truncated = True
                    self._terminate_process(process)
                    break
            stderr = self._finish_process(
                process,
                tool="fd",
                command=command,
                started_at=started_at,
                accepted_exit_codes=(0,),
                ignore_failure=truncated,
            )
        finally:
            self._ensure_reaped(process)
        if stderr:
            logger.debug("tool_discover_stderr stderr=%s", stderr.strip())
        logger.info(
            "tool_discover_finished pattern=%s scope=%s results=%s truncated=%s",
            request.pattern,
            scope,
            len(paths),
            truncated,
        )
        return FileDiscoveryResult(
            pattern=request.pattern,
            scope=self._display_scope(scope),
            file_type=request.file_type,
            paths=paths,
            truncated=truncated,
        )

    def git_context(self, request: GitContextRequest) -> GitContextResult:
        """Collect repository status, diff, and recent commit context."""
        scope = self._resolve_scope(request.scope)
        binary = self._require_binary("git")
        command_cwd, pathspec = self._git_scope(scope)

        branch = self._run_text_command(
            [binary.resolved_command or "git", "branch", "--show-current"],
            tool="git",
            cwd=command_cwd,
        ).strip()

        status_text = self._run_text_command(
            [binary.resolved_command or "git", "status", "--short", "--", pathspec],
            tool="git",
            cwd=command_cwd,
        )
        status_entries = self._parse_git_status(
            status_text,
            cwd=command_cwd,
            max_entries=request.max_status_entries,
        )

        unstaged_text = self._run_text_command(
            [binary.resolved_command or "git", "diff", "--numstat", "--", pathspec],
            tool="git",
            cwd=command_cwd,
        )
        staged_text = self._run_text_command(
            [binary.resolved_command or "git", "diff", "--numstat", "--cached", "--", pathspec],
            tool="git",
            cwd=command_cwd,
        )

        recent_commits = self._safe_recent_commits(
            binary.resolved_command or "git",
            cwd=command_cwd,
            pathspec=pathspec,
            limit=request.max_recent_commits,
        )
        logger.info(
            "tool_git_finished scope=%s branch=%s status=%s",
            scope,
            branch,
            len(status_entries[0]),
        )
        return GitContextResult(
            scope=self._display_scope(scope),
            branch=branch or "(detached)",
            status=status_entries[0],
            truncated_status=status_entries[1],
            unstaged_diff=self._parse_git_numstat(unstaged_text, cwd=command_cwd),
            staged_diff=self._parse_git_numstat(staged_text, cwd=command_cwd),
            recent_commits=recent_commits,
        )

    def lookup_help(self, request: HelpLookupRequest) -> HelpLookupResult:
        """Lookup manual-page or TLDR help content."""
        if request.source is HelpLookupSource.MAN:
            binary = self._require_binary("man")
            command = [binary.resolved_command or "man", "-P", "cat", request.topic]
            environment = {**self._environment, "MANPAGER": "cat"}
            tool_name = "man"
        else:
            binary = self._require_binary("tldr")
            command = [binary.resolved_command or "tldr", request.topic]
            environment = self._environment
            tool_name = "tldr"

        raw_content = self._run_text_command(
            command,
            tool=tool_name,
            cwd=self._workspace_root,
            environment=environment,
        )
        content = _clean_help_text(raw_content)
        truncated = False
        encoded = content.encode("utf-8")
        if len(encoded) > request.max_characters:
            content = encoded[: request.max_characters].decode("utf-8", errors="ignore").rstrip()
            truncated = True
        return HelpLookupResult(
            tool=tool_name,
            source=request.source,
            topic=request.topic,
            content=content,
            truncated=truncated,
        )

    def _detect_binary(self, spec: _BinarySpec) -> ToolBinaryStatus:
        search_path = self._environment.get("PATH")
        for candidate in spec.candidates:
            path = shutil.which(candidate, path=search_path)
            if path:
                return ToolBinaryStatus(
                    name=spec.name,
                    candidates=list(spec.candidates),
                    status=ToolAvailabilityStatus.AVAILABLE,
                    required=spec.required,
                    resolved_command=candidate,
                    path=Path(path),
                    install_hint=spec.install_hint,
                )
        return ToolBinaryStatus(
            name=spec.name,
            candidates=list(spec.candidates),
            status=ToolAvailabilityStatus.MISSING,
            required=spec.required,
            install_hint=spec.install_hint,
        )

    def _path_filter(self) -> WorkspacePathFilter:
        return WorkspacePathFilter(self._workspace_root)

    def _require_binary(self, name: str) -> ToolBinaryStatus:
        status = self._binary_status[name]
        if status.status is ToolAvailabilityStatus.AVAILABLE:
            return status
        raise ToolExecutionError(
            ToolError(
                code=ToolErrorCode.MISSING_BINARY,
                tool=name,
                message=f"`{name}` is not available in PATH.",
                install_hint=status.install_hint,
            )
        )

    def _resolve_scope(self, scope: Path | None) -> Path:
        if scope is None:
            return self._workspace_root
        candidate = scope if scope.is_absolute() else self._workspace_root / scope
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_SCOPE,
                    tool="workspace",
                    message="Tool scope must stay within the configured workspace root.",
                    detail=str(self._workspace_root),
                )
            ) from exc
        if not resolved.exists():
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_SCOPE,
                    tool="workspace",
                    message=f"Tool scope does not exist: {resolved}",
                )
            )
        return resolved

    def _display_scope(self, scope: Path) -> str:
        try:
            relative = scope.relative_to(self._workspace_root)
        except ValueError:
            return str(scope)
        return "." if not relative.parts else relative.as_posix()

    def _path_argument(self, scope: Path) -> str:
        relative = self._display_scope(scope)
        return "." if relative == "." else relative

    def _git_scope(self, scope: Path) -> tuple[Path, str]:
        if scope == self._workspace_root:
            return scope, "."
        if scope.is_dir():
            return scope, "."
        return scope.parent, scope.name

    def _spawn(self, command: list[str]) -> subprocess.Popen[str]:
        logger.info("tool_command_started argv=%s", command)
        try:
            return subprocess.Popen(
                command,
                cwd=str(self._workspace_root),
                env=self._environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.EXECUTION_FAILED,
                    tool=command[0],
                    message=f"Failed to start `{command[0]}`.",
                    detail=str(exc),
                    command=command,
                )
            ) from exc

    def _run_text_command(
        self,
        command: list[str],
        *,
        tool: str,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        logger.info("tool_command_started tool=%s cwd=%s argv=%s", tool, cwd, command)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=dict(environment or self._environment),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._default_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    tool=tool,
                    message=f"`{tool}` timed out after {self._default_timeout_seconds}s.",
                    command=command,
                )
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.EXECUTION_FAILED,
                    tool=tool,
                    message=f"Failed to start `{tool}`.",
                    detail=str(exc),
                    command=command,
                )
            ) from exc

        stderr = completed.stderr.strip()
        stdout = completed.stdout
        if completed.returncode == 0:
            return stdout

        detail = stderr or stdout.strip() or f"exit code {completed.returncode}"
        error_code = (
            ToolErrorCode.NOT_FOUND
            if self._looks_like_not_found(detail)
            else ToolErrorCode.EXECUTION_FAILED
        )
        raise ToolExecutionError(
            ToolError(
                code=error_code,
                tool=tool,
                message=f"`{tool}` failed with exit code {completed.returncode}.",
                detail=detail,
                command=command,
                install_hint=self._binary_status.get(tool, ToolBinaryStatus(
                    name=tool,
                    status=ToolAvailabilityStatus.MISSING,
                )).install_hint,
            )
        )

    def _finish_process(
        self,
        process: subprocess.Popen[str],
        *,
        tool: str,
        command: list[str],
        started_at: float,
        accepted_exit_codes: tuple[int, ...],
        ignore_failure: bool,
    ) -> str:
        remaining = max(0.1, self._default_timeout_seconds - (time.monotonic() - started_at))
        try:
            _stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            self._kill_process(process)
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.TIMEOUT,
                    tool=tool,
                    message=f"`{tool}` timed out after {self._default_timeout_seconds}s.",
                    command=command,
                )
            ) from exc

        if ignore_failure:
            return stderr
        if process.returncode not in accepted_exit_codes:
            detail = stderr.strip() or f"exit code {process.returncode}"
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.EXECUTION_FAILED,
                    tool=tool,
                    message=f"`{tool}` failed with exit code {process.returncode}.",
                    detail=detail,
                    command=command,
                    install_hint=self._binary_status[tool].install_hint,
                )
            )
        return stderr

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()

    def _kill_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)

    def _ensure_reaped(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            self._kill_process(process)

    def _parse_json_line(self, line: str, *, tool: str, command: list[str]) -> dict[str, object]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool=tool,
                    message=f"`{tool}` emitted invalid JSON output.",
                    detail=str(exc),
                    command=command,
                )
            ) from exc
        if not isinstance(payload, dict):
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool=tool,
                    message=f"`{tool}` emitted an unexpected JSON payload.",
                    command=command,
                )
            )
        return payload

    def _parse_rg_match(self, payload: dict[str, object]) -> SearchMatch:
        data = payload["data"]
        if not isinstance(data, dict):
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool="rg",
                    message="`rg` emitted malformed match data.",
                )
            )
        path_data = data.get("path")
        lines_data = data.get("lines")
        submatches = data.get("submatches")
        if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool="rg",
                    message="`rg` emitted incomplete match data.",
                )
            )
        raw_path = path_data.get("text")
        raw_lines = lines_data.get("text")
        if not isinstance(raw_path, str) or not isinstance(raw_lines, str):
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool="rg",
                    message="`rg` emitted non-text match data.",
                )
            )
        first_column = 1
        if isinstance(submatches, list) and submatches:
            first = submatches[0]
            if isinstance(first, dict) and isinstance(first.get("start"), int):
                first_column = first["start"] + 1
        line_number = data.get("line_number")
        if not isinstance(line_number, int):
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_OUTPUT,
                    tool="rg",
                    message="`rg` did not provide a line number.",
                )
            )
        return SearchMatch(
            path=self._normalize_output_path(raw_path, cwd=self._workspace_root),
            line_number=line_number,
            column_number=first_column,
            line_text=_truncate_text(raw_lines),
        )

    def _parse_git_status(
        self,
        output: str,
        *,
        cwd: Path,
        max_entries: int,
    ) -> tuple[list[GitStatusEntry], bool]:
        entries: list[GitStatusEntry] = []
        truncated = False
        for line in output.splitlines():
            if not line.strip():
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            path = self._normalize_git_path(line[3:] if len(line) > 3 else "")
            entries.append(
                GitStatusEntry(
                    path=self._normalize_output_path(path, cwd=cwd),
                    index_status=line[0],
                    worktree_status=line[1],
                    raw=line,
                )
            )
        return entries, truncated

    def _parse_git_numstat(self, output: str, *, cwd: Path) -> list[GitDiffStat]:
        entries: list[GitDiffStat] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, raw_path = parts
            binary = added == "-" or removed == "-"
            path = self._normalize_git_path(raw_path)
            entries.append(
                GitDiffStat(
                    path=self._normalize_output_path(path, cwd=cwd),
                    additions=None if binary else int(added),
                    deletions=None if binary else int(removed),
                    binary=binary,
                )
            )
        return entries

    def _safe_recent_commits(
        self,
        command_name: str,
        *,
        cwd: Path,
        pathspec: str,
        limit: int,
    ) -> list[GitCommitSummary]:
        try:
            output = self._run_text_command(
                [
                    command_name,
                    "log",
                    "--pretty=format:%H%x09%h%x09%s",
                    "-n",
                    str(limit),
                    "--",
                    pathspec,
                ],
                tool="git",
                cwd=cwd,
            )
        except ToolExecutionError as exc:
            detail = exc.error.detail or ""
            if "does not have any commits yet" in detail.lower():
                return []
            raise
        commits: list[GitCommitSummary] = []
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            commits.append(
                GitCommitSummary(
                    commit_sha=parts[0],
                    short_sha=parts[1],
                    summary=parts[2],
                )
            )
        return commits

    def _normalize_git_path(self, raw_path: str) -> str:
        candidate = raw_path.strip()
        if " -> " in candidate:
            return candidate.rsplit(" -> ", 1)[1]
        if " => " not in candidate:
            return candidate
        if "{" in candidate and "}" in candidate:
            start = candidate.index("{")
            end = candidate.index("}", start)
            inner = candidate[start + 1 : end]
            if " => " in inner:
                _old, new = inner.split(" => ", 1)
                return f"{candidate[:start]}{new}{candidate[end + 1:]}"
        return candidate.rsplit(" => ", 1)[1]

    def _normalize_output_path(self, raw_path: str, *, cwd: Path) -> str:
        cleaned = raw_path.removeprefix("./")
        path = Path(cleaned)
        resolved = path.resolve() if path.is_absolute() else (cwd / path).resolve()
        try:
            relative = resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ToolExecutionError(
                ToolError(
                    code=ToolErrorCode.INVALID_SCOPE,
                    tool="workspace",
                    message="Tool output escaped the configured workspace root.",
                    detail=str(resolved),
                )
            ) from exc
        return relative.as_posix()

    def _looks_like_not_found(self, detail: str) -> bool:
        lowered = detail.lower()
        return any(
            marker in lowered
            for marker in (
                "no manual entry",
                "page not found",
                "could not find",
                "not found",
                "unknown command",
            )
        )
