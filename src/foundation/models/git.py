"""Typed git capability models for v3 Stage 3."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


# Git subcommands that mutate the working tree, index, or history. The planner
# (preflight review gating) and the guardrail policy engine (write-risk
# classification) must agree on this set, so it is defined exactly once here.
GIT_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "add",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "mv",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
        "tag",
    }
)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class GitErrorCode(StrEnum):
    """Machine-readable error codes for git capability failures."""

    NOT_A_REPO = "not_a_repo"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    PATH_OUTSIDE_REPO = "path_outside_repo"
    EMPTY_PATH_LIST = "empty_path_list"
    NO_STAGED_CHANGES = "no_staged_changes"
    GIT_BINARY_NOT_FOUND = "git_binary_not_found"
    GIT_COMMAND_FAILED = "git_command_failed"
    INVALID_REF = "invalid_ref"


class GitOperationError(StrictModel):
    """Structured error returned by git capability failures."""

    code: GitErrorCode
    message: str = Field(min_length=1)
    detail: str | None = None
    path: str | None = None
    suggestion: str | None = None


class GitServiceError(RuntimeError):
    """Runtime exception wrapping a structured GitOperationError."""

    def __init__(self, error: GitOperationError) -> None:
        super().__init__(error.message)
        self.error = error


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class GitFileChange(StrictModel):
    """One changed file entry from git status."""

    path: str
    status: str  # "modified", "added", "deleted", "renamed", "copied", "type_changed"
    original_path: str | None = None


class GitLogEntry(StrictModel):
    """One commit entry from git log."""

    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    message: str


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class GitStatusRequest(StrictModel):
    """Inspect repository status: branch, staged, unstaged, conflicts."""

    pass


class GitDiffRequest(StrictModel):
    """Inspect staged or unstaged diffs with optional path filter."""

    staged: bool = False
    paths: list[str] = Field(default_factory=list)
    stat_only: bool = False


class GitShowRequest(StrictModel):
    """Inspect a commit or object by ref."""

    ref: str = Field(min_length=1)


class GitLogRequest(StrictModel):
    """Inspect recent commit history."""

    max_count: int = Field(default=20, ge=1, le=100)


class GitStageRequest(StrictModel):
    """Stage explicit workspace paths."""

    paths: list[str] = Field(min_length=1)


class GitUnstageRequest(StrictModel):
    """Unstage explicit workspace paths."""

    paths: list[str] = Field(min_length=1)


class GitCommitRequest(StrictModel):
    """Commit staged changes with a non-empty message."""

    message: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class GitStatusResult(StrictModel):
    """Structured repository status."""

    branch: str | None = None
    commit: str | None = None
    detached_head: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[GitFileChange] = Field(default_factory=list)
    unstaged: list[GitFileChange] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    merge_in_progress: bool = False
    rebase_in_progress: bool = False


class GitDiffResult(StrictModel):
    """Diff output with optional stat summary."""

    diff: str
    stat: str
    truncated: bool = False


class GitShowResult(StrictModel):
    """Content of a commit or object inspection."""

    ref: str
    content: str
    truncated: bool = False


class GitLogResult(StrictModel):
    """Recent commit history entries."""

    entries: list[GitLogEntry] = Field(default_factory=list)
    truncated: bool = False


class GitMutationResult(StrictModel):
    """Result of a stage, unstage, or commit operation."""

    summary: str
    paths_changed: list[str] = Field(default_factory=list)
