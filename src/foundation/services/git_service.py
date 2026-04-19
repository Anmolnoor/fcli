"""Workspace git service for v3 Stage 3 git capabilities."""

from __future__ import annotations

import subprocess
from pathlib import Path

from foundation.models.git import (
    GitCommitRequest,
    GitDiffRequest,
    GitDiffResult,
    GitErrorCode,
    GitFileChange,
    GitLogEntry,
    GitLogRequest,
    GitLogResult,
    GitMutationResult,
    GitOperationError,
    GitServiceError,
    GitShowRequest,
    GitShowResult,
    GitStageRequest,
    GitStatusRequest,
    GitStatusResult,
    GitUnstageRequest,
)

_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB

_STATUS_LETTER: dict[str, str] = {
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
}


def _raise(
    code: GitErrorCode,
    message: str,
    *,
    detail: str | None = None,
    path: str | None = None,
    suggestion: str | None = None,
) -> None:
    raise GitServiceError(
        GitOperationError(
            code=code,
            message=message,
            detail=detail,
            path=path,
            suggestion=suggestion,
        )
    )


def _status_name(letter: str) -> str:
    return _STATUS_LETTER.get(letter, letter.lower())


def _truncate(text: str, limit: int = _MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Truncate text to *limit* bytes, breaking at the last newline."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    truncated = raw[:limit].decode("utf-8", errors="ignore")
    # Break at the last newline to avoid partial lines
    nl = truncated.rfind("\n")
    if nl > 0:
        truncated = truncated[: nl + 1]
    return truncated, True


# ---------------------------------------------------------------------------
# GitService
# ---------------------------------------------------------------------------


class GitService:
    """Workspace-bound git operations for v3 git capabilities."""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._repo_root: Path | None = None
        self._git_dir: Path | None = None
        self._discovered = False

    # -- repo discovery -----------------------------------------------------

    def _discover(self) -> None:
        if self._discovered:
            return
        self._discovered = True
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel", "--git-dir"],
                cwd=self._workspace_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return
        except subprocess.TimeoutExpired:
            return
        if proc.returncode != 0:
            return
        lines = proc.stdout.strip().splitlines()
        if not lines:
            return
        self._repo_root = Path(lines[0]).resolve()
        if len(lines) > 1:
            git_dir = Path(lines[1])
            if not git_dir.is_absolute():
                git_dir = (self._repo_root / git_dir).resolve()
            self._git_dir = git_dir
        else:
            self._git_dir = self._repo_root / ".git"

    def _ensure_repo(self) -> Path:
        self._discover()
        if self._repo_root is None:
            _raise(
                GitErrorCode.NOT_A_REPO,
                "No git repository found at workspace root.",
                path=str(self._workspace_root),
            )
        return self._repo_root

    # -- subprocess helper --------------------------------------------------

    def _run_git(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        repo_root = self._ensure_repo()
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            _raise(
                GitErrorCode.GIT_BINARY_NOT_FOUND,
                "Git binary not found on PATH.",
            )
        except subprocess.TimeoutExpired:
            _raise(
                GitErrorCode.GIT_COMMAND_FAILED,
                "Git command timed out after 30 seconds.",
                detail=f"git {' '.join(args)}",
            )
        if check and proc.returncode != 0:
            stderr = proc.stderr.strip()
            _raise(
                GitErrorCode.GIT_COMMAND_FAILED,
                f"Git command failed with exit code {proc.returncode}.",
                detail=stderr or f"git {' '.join(args)}",
            )
        return proc

    # -- path validation ----------------------------------------------------

    def _validate_path(self, raw_path: str) -> Path:
        """Validate a user-provided path is within workspace and repo."""
        repo_root = self._ensure_repo()
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (repo_root / candidate).resolve()

        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            _raise(
                GitErrorCode.PATH_OUTSIDE_WORKSPACE,
                "Path escapes the workspace boundary.",
                path=raw_path,
            )

        try:
            resolved.relative_to(repo_root)
        except ValueError:
            _raise(
                GitErrorCode.PATH_OUTSIDE_REPO,
                "Path is outside the active git repository.",
                path=raw_path,
            )
        return resolved

    # -- public operations --------------------------------------------------

    def status(self, request: GitStatusRequest) -> GitStatusResult:
        """Inspect repository status: branch, staged, unstaged, conflicts."""
        self._ensure_repo()
        proc = self._run_git("status", "--porcelain=v2", "--branch")

        branch: str | None = None
        commit_hash: str | None = None
        detached_head = False
        upstream: str | None = None
        ahead = 0
        behind = 0
        staged: list[GitFileChange] = []
        unstaged: list[GitFileChange] = []
        untracked: list[str] = []
        conflicts: list[str] = []

        for line in proc.stdout.splitlines():
            if line.startswith("# branch.oid "):
                oid = line[len("# branch.oid "):]
                commit_hash = None if oid == "(initial)" else oid
            elif line.startswith("# branch.head "):
                head = line[len("# branch.head "):]
                if head == "(detached)":
                    detached_head = True
                else:
                    branch = head
            elif line.startswith("# branch.upstream "):
                upstream = line[len("# branch.upstream "):]
            elif line.startswith("# branch.ab "):
                parts = line[len("# branch.ab "):].split()
                if len(parts) >= 2:
                    ahead = int(parts[0].lstrip("+"))
                    behind = abs(int(parts[1]))
            elif line.startswith("1 "):
                # Ordinary changed: 1 XY sub mH mI mW hH hI <path>
                parts = line.split(maxsplit=8)
                if len(parts) < 9:
                    continue
                xy = parts[1]
                file_path = parts[8]
                if xy[0] != ".":
                    staged.append(GitFileChange(path=file_path, status=_status_name(xy[0])))
                if xy[1] != ".":
                    unstaged.append(GitFileChange(path=file_path, status=_status_name(xy[1])))
            elif line.startswith("2 "):
                # Rename/copy: 2 XY sub mH mI mW hH hI Xscore <path>\t<origPath>
                parts = line.split(maxsplit=9)
                if len(parts) < 10:
                    continue
                xy = parts[1]
                path_part = parts[9]
                if "\t" in path_part:
                    new_path, orig_path = path_part.split("\t", 1)
                else:
                    new_path = path_part
                    orig_path = None
                if xy[0] != ".":
                    staged.append(GitFileChange(
                        path=new_path,
                        status=_status_name(xy[0]),
                        original_path=orig_path,
                    ))
                if xy[1] != ".":
                    unstaged.append(GitFileChange(
                        path=new_path,
                        status=_status_name(xy[1]),
                        original_path=orig_path,
                    ))
            elif line.startswith("u "):
                # Unmerged: u XY sub m1 m2 m3 mW h1 h2 h3 <path>
                parts = line.split(maxsplit=10)
                if len(parts) >= 11:
                    conflicts.append(parts[10])
            elif line.startswith("? "):
                untracked.append(line[2:])

        # Detect merge / rebase state from git dir
        merge_in_progress = False
        rebase_in_progress = False
        if self._git_dir is not None:
            merge_in_progress = (self._git_dir / "MERGE_HEAD").exists()
            rebase_in_progress = (
                (self._git_dir / "rebase-merge").is_dir()
                or (self._git_dir / "rebase-apply").is_dir()
            )

        return GitStatusResult(
            branch=branch,
            commit=commit_hash,
            detached_head=detached_head,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            staged=staged,
            unstaged=unstaged,
            untracked=untracked,
            conflicts=conflicts,
            merge_in_progress=merge_in_progress,
            rebase_in_progress=rebase_in_progress,
        )

    def diff(self, request: GitDiffRequest) -> GitDiffResult:
        """Inspect staged or unstaged diffs with optional path filter."""
        self._ensure_repo()

        for p in request.paths:
            self._validate_path(p)

        # Get stat summary
        stat_args = ["diff", "--stat"]
        if request.staged:
            stat_args.append("--cached")
        if request.paths:
            stat_args.append("--")
            stat_args.extend(request.paths)

        stat_proc = self._run_git(*stat_args)
        stat_output = stat_proc.stdout

        if request.stat_only:
            return GitDiffResult(diff="", stat=stat_output, truncated=False)

        # Get full diff
        diff_args = ["diff"]
        if request.staged:
            diff_args.append("--cached")
        if request.paths:
            diff_args.append("--")
            diff_args.extend(request.paths)

        diff_proc = self._run_git(*diff_args)
        diff_output, truncated = _truncate(diff_proc.stdout)

        return GitDiffResult(diff=diff_output, stat=stat_output, truncated=truncated)

    def show(self, request: GitShowRequest) -> GitShowResult:
        """Inspect a commit or object by ref."""
        self._ensure_repo()
        proc = self._run_git("show", request.ref, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "unknown revision" in stderr or "bad object" in stderr or "not a valid" in stderr:
                _raise(
                    GitErrorCode.INVALID_REF,
                    f"Invalid ref: {request.ref!r}",
                    detail=stderr,
                )
            _raise(
                GitErrorCode.GIT_COMMAND_FAILED,
                f"Git show failed with exit code {proc.returncode}.",
                detail=stderr or f"git show {request.ref}",
            )

        content, truncated = _truncate(proc.stdout)
        return GitShowResult(ref=request.ref, content=content, truncated=truncated)

    def log(self, request: GitLogRequest) -> GitLogResult:
        """Inspect recent commit history."""
        self._ensure_repo()

        sep = "\x1e"
        fmt = f"%H{sep}%h{sep}%aN{sep}%aE{sep}%aI{sep}%s"
        proc = self._run_git(
            "log",
            f"--format={fmt}",
            f"-n{request.max_count + 1}",
            check=False,
        )
        # Empty repo with no commits returns exit code 128
        if proc.returncode != 0:
            if "does not have any commits" in proc.stderr:
                return GitLogResult(entries=[], truncated=False)
            stderr = proc.stderr.strip()
            _raise(
                GitErrorCode.GIT_COMMAND_FAILED,
                f"Git log failed with exit code {proc.returncode}.",
                detail=stderr or "git log",
            )

        entries: list[GitLogEntry] = []
        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(sep, 5)
            if len(parts) != 6:
                continue
            entries.append(GitLogEntry(
                hash=parts[0],
                short_hash=parts[1],
                author_name=parts[2],
                author_email=parts[3],
                date=parts[4],
                message=parts[5],
            ))

        truncated = len(entries) > request.max_count
        if truncated:
            entries = entries[: request.max_count]

        return GitLogResult(entries=entries, truncated=truncated)

    def stage(self, request: GitStageRequest) -> GitMutationResult:
        """Stage explicit workspace paths."""
        for p in request.paths:
            self._validate_path(p)

        self._run_git("add", "--", *request.paths)

        return GitMutationResult(
            summary=f"Staged {len(request.paths)} path(s).",
            paths_changed=list(request.paths),
        )

    def unstage(self, request: GitUnstageRequest) -> GitMutationResult:
        """Unstage explicit workspace paths."""
        for p in request.paths:
            self._validate_path(p)

        # git reset HEAD fails on initial commit; fall back to rm --cached
        proc = self._run_git("reset", "HEAD", "--", *request.paths, check=False)
        if proc.returncode != 0:
            self._run_git("rm", "--cached", "--force", "--", *request.paths)

        return GitMutationResult(
            summary=f"Unstaged {len(request.paths)} path(s).",
            paths_changed=list(request.paths),
        )

    def commit(self, request: GitCommitRequest) -> GitMutationResult:
        """Commit staged changes. Fails if nothing is staged."""
        self._ensure_repo()

        # Precondition: staged changes must exist
        proc = self._run_git("diff", "--cached", "--quiet", check=False)
        if proc.returncode == 0:
            _raise(
                GitErrorCode.NO_STAGED_CHANGES,
                "No staged changes to commit.",
                suggestion="Use foundation.git.stage to stage changes first.",
            )

        self._run_git("commit", "-m", request.message)

        # Get changed paths from the new commit
        tree_proc = self._run_git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
        )
        paths = [p for p in tree_proc.stdout.strip().splitlines() if p]

        return GitMutationResult(
            summary=f"Committed with message: {request.message[:80]}",
            paths_changed=paths,
        )
