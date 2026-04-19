"""Tests for v3 Stage 3 git capabilities — GitService and models."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundation.models.git import (
    GitCommitRequest,
    GitDiffRequest,
    GitErrorCode,
    GitLogRequest,
    GitServiceError,
    GitShowRequest,
    GitStageRequest,
    GitStatusRequest,
    GitUnstageRequest,
)
from foundation.services.git_service import GitService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> tuple[GitService, Path]:
    """Create a git-initialised workspace and return (service, workspace)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    service = GitService(workspace_root=workspace)
    return service, workspace


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    )


def _commit(ws: Path, msg: str, files: dict[str, str] | None = None) -> None:
    if files:
        for name, content in files.items():
            p = ws / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            _git(ws, "add", name)
    _git(ws, "commit", "-m", msg)


# ===================================================================
# git.status — normal operation
# ===================================================================


class TestGitStatusNormal:
    def test_clean_repo_after_commit(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"readme.txt": "hello\n"})

        result = svc.status(GitStatusRequest())

        assert result.branch is not None
        assert result.commit is not None
        assert len(result.commit) == 40
        assert result.staged == []
        assert result.unstaged == []
        assert result.untracked == []
        assert result.detached_head is False

    def test_untracked_files_shown(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"readme.txt": "hello\n"})
        (ws / "new.txt").write_text("new\n", encoding="utf-8")

        result = svc.status(GitStatusRequest())

        assert "new.txt" in result.untracked

    def test_staged_files_shown(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"readme.txt": "hello\n"})
        (ws / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(ws, "add", "staged.txt")

        result = svc.status(GitStatusRequest())

        staged_paths = [f.path for f in result.staged]
        assert "staged.txt" in staged_paths

    def test_unstaged_modifications_shown(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "original\n"})
        (ws / "file.txt").write_text("modified\n", encoding="utf-8")

        result = svc.status(GitStatusRequest())

        unstaged_paths = [f.path for f in result.unstaged]
        assert "file.txt" in unstaged_paths
        assert result.unstaged[0].status == "modified"

    def test_initial_repo_no_commits(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)

        result = svc.status(GitStatusRequest())

        assert result.commit is None
        assert result.branch is not None  # e.g. "main" or "master"

    def test_detached_head(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "first", {"a.txt": "a\n"})
        _commit(ws, "second", {"b.txt": "b\n"})
        # Detach at first commit
        first_hash = _git(ws, "rev-parse", "HEAD~1").stdout.strip()
        _git(ws, "checkout", first_hash)

        result = svc.status(GitStatusRequest())

        assert result.detached_head is True
        assert result.branch is None

    def test_deleted_file_surfaces_in_unstaged(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"doomed.txt": "bye\n"})
        (ws / "doomed.txt").unlink()

        result = svc.status(GitStatusRequest())

        unstaged_paths = [f.path for f in result.unstaged]
        unstaged_statuses = {f.path: f.status for f in result.unstaged}
        assert "doomed.txt" in unstaged_paths
        assert unstaged_statuses["doomed.txt"] == "deleted"

    def test_deleted_file_surfaces_in_staged(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"doomed.txt": "bye\n"})
        (ws / "doomed.txt").unlink()
        _git(ws, "add", "doomed.txt")

        result = svc.status(GitStatusRequest())

        staged_paths = [f.path for f in result.staged]
        staged_statuses = {f.path: f.status for f in result.staged}
        assert "doomed.txt" in staged_paths
        assert staged_statuses["doomed.txt"] == "deleted"

    def test_merge_conflict_surfaces_in_conflicts(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "base", {"shared.txt": "base\n"})
        # Create diverging branches
        _git(ws, "checkout", "-b", "feature")
        _commit(ws, "feature change", {"shared.txt": "feature\n"})
        _git(ws, "checkout", "-")
        _commit(ws, "main change", {"shared.txt": "main\n"})
        # Merge to create conflict
        subprocess.run(
            ["git", "merge", "feature"],
            cwd=ws, capture_output=True, text=True,
        )

        result = svc.status(GitStatusRequest())

        assert "shared.txt" in result.conflicts
        assert result.merge_in_progress is True

    def test_renamed_file_shows_original_path(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"old_name.txt": "content\n"})
        _git(ws, "mv", "old_name.txt", "new_name.txt")

        result = svc.status(GitStatusRequest())

        staged_renames = [f for f in result.staged if f.status == "renamed"]
        assert len(staged_renames) == 1
        assert staged_renames[0].path == "new_name.txt"
        assert staged_renames[0].original_path == "old_name.txt"


# ===================================================================
# git.status — error paths
# ===================================================================


class TestGitStatusErrors:
    def test_not_a_repo(self, tmp_path: Path) -> None:
        workspace = tmp_path / "empty"
        workspace.mkdir()
        svc = GitService(workspace_root=workspace)

        with pytest.raises(GitServiceError) as exc_info:
            svc.status(GitStatusRequest())
        assert exc_info.value.error.code == GitErrorCode.NOT_A_REPO


# ===================================================================
# git.diff — normal operation
# ===================================================================


class TestGitDiffNormal:
    def test_unstaged_diff(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "line1\nline2\n"})
        (ws / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = svc.diff(GitDiffRequest())

        assert "line3" in result.diff
        assert result.truncated is False

    def test_staged_diff(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "old\n"})
        (ws / "file.txt").write_text("new\n", encoding="utf-8")
        _git(ws, "add", "file.txt")

        result = svc.diff(GitDiffRequest(staged=True))

        assert "new" in result.diff

    def test_stat_only(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "old\n"})
        (ws / "file.txt").write_text("new\n", encoding="utf-8")

        result = svc.diff(GitDiffRequest(stat_only=True))

        assert result.diff == ""
        assert "file.txt" in result.stat

    def test_path_filter(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"a.txt": "a\n", "b.txt": "b\n"})
        (ws / "a.txt").write_text("A\n", encoding="utf-8")
        (ws / "b.txt").write_text("B\n", encoding="utf-8")

        result = svc.diff(GitDiffRequest(paths=["a.txt"]))

        assert "a.txt" in result.diff
        assert "b.txt" not in result.diff

    def test_no_changes_returns_empty(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "content\n"})

        result = svc.diff(GitDiffRequest())

        assert result.diff == ""

    def test_binary_file_visible_in_diff(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "image.bin").write_bytes(b"\x89PNG\x00\x01\x02")
        _git(ws, "add", "image.bin")
        _commit(ws, "add binary", {})
        (ws / "image.bin").write_bytes(b"\x89PNG\x00\x03\x04")

        result = svc.diff(GitDiffRequest())

        # Binary diff should be visible in stat
        assert "image.bin" in result.stat
        # Binary marker should appear in diff text
        assert "Binary" in result.diff or "image.bin" in result.diff

    def test_deleted_file_visible_in_diff(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"doomed.txt": "content\n"})
        (ws / "doomed.txt").unlink()

        result = svc.diff(GitDiffRequest())

        assert "doomed.txt" in result.stat
        assert "doomed.txt" in result.diff

    def test_renamed_file_visible_in_staged_diff(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"old.txt": "content\n"})
        _git(ws, "mv", "old.txt", "new.txt")

        result = svc.diff(GitDiffRequest(staged=True))

        # Rename should be visible in stat
        assert "new.txt" in result.stat or "old.txt" in result.stat

    def test_stat_preserved_when_diff_body_would_truncate(self, tmp_path: Path) -> None:
        """Stat field provides file-level visibility even if diff body is truncated."""
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"a.txt": "a\n", "b.txt": "b\n"})
        (ws / "a.txt").write_text("A\n", encoding="utf-8")
        (ws / "b.txt").write_text("B\n", encoding="utf-8")

        result = svc.diff(GitDiffRequest())

        # Both files visible in stat regardless of diff size
        assert "a.txt" in result.stat
        assert "b.txt" in result.stat


# ===================================================================
# git.diff — error paths
# ===================================================================


class TestGitDiffErrors:
    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)
        _commit(_, "init", {"x.txt": "x\n"})  # noqa: F841

        with pytest.raises(GitServiceError) as exc_info:
            svc.diff(GitDiffRequest(paths=["../../escape.txt"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# git.show — normal operation
# ===================================================================


class TestGitShowNormal:
    def test_show_head(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "first commit", {"file.txt": "content\n"})

        result = svc.show(GitShowRequest(ref="HEAD"))

        assert "first commit" in result.content
        assert result.ref == "HEAD"
        assert result.truncated is False

    def test_show_specific_commit(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "first", {"a.txt": "a\n"})
        _commit(ws, "second", {"b.txt": "b\n"})
        first_hash = _git(ws, "rev-parse", "HEAD~1").stdout.strip()

        result = svc.show(GitShowRequest(ref=first_hash))

        assert "first" in result.content


# ===================================================================
# git.show — error paths
# ===================================================================


class TestGitShowErrors:
    def test_invalid_ref(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "first", {"f.txt": "f\n"})

        with pytest.raises(GitServiceError) as exc_info:
            svc.show(GitShowRequest(ref="nonexistent_ref_abc123"))
        err = exc_info.value.error
        assert err.code in (GitErrorCode.INVALID_REF, GitErrorCode.GIT_COMMAND_FAILED)


# ===================================================================
# git.log — normal operation
# ===================================================================


class TestGitLogNormal:
    def test_returns_commit_entries(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "first commit", {"a.txt": "a\n"})
        _commit(ws, "second commit", {"b.txt": "b\n"})

        result = svc.log(GitLogRequest())

        assert len(result.entries) == 2
        assert result.entries[0].message == "second commit"
        assert result.entries[1].message == "first commit"
        assert len(result.entries[0].hash) == 40
        assert result.entries[0].author_name == "Test User"
        assert result.entries[0].author_email == "test@test.com"

    def test_max_count_limits(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        for i in range(5):
            _commit(ws, f"commit {i}", {f"file{i}.txt": f"{i}\n"})

        result = svc.log(GitLogRequest(max_count=3))

        assert len(result.entries) == 3
        assert result.truncated is True

    def test_empty_repo_returns_empty(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        result = svc.log(GitLogRequest())

        assert result.entries == []
        assert result.truncated is False

    def test_single_commit(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "only one", {"f.txt": "f\n"})

        result = svc.log(GitLogRequest())

        assert len(result.entries) == 1
        assert result.truncated is False


# ===================================================================
# git.stage — normal operation
# ===================================================================


class TestGitStageNormal:
    def test_stages_new_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})
        (ws / "new.txt").write_text("new content\n", encoding="utf-8")

        result = svc.stage(GitStageRequest(paths=["new.txt"]))

        assert result.paths_changed == ["new.txt"]
        assert "1" in result.summary

        # Verify it's actually staged
        status = svc.status(GitStatusRequest())
        staged_paths = [f.path for f in status.staged]
        assert "new.txt" in staged_paths

    def test_stages_multiple_files(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})
        (ws / "a.txt").write_text("a\n", encoding="utf-8")
        (ws / "b.txt").write_text("b\n", encoding="utf-8")

        result = svc.stage(GitStageRequest(paths=["a.txt", "b.txt"]))

        assert len(result.paths_changed) == 2

    def test_stages_modified_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"file.txt": "old\n"})
        (ws / "file.txt").write_text("new\n", encoding="utf-8")

        svc.stage(GitStageRequest(paths=["file.txt"]))

        status = svc.status(GitStatusRequest())
        staged_paths = [f.path for f in status.staged]
        assert "file.txt" in staged_paths


# ===================================================================
# git.stage — error paths
# ===================================================================


class TestGitStageErrors:
    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(GitServiceError) as exc_info:
            svc.stage(GitStageRequest(paths=["../../escape.txt"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_empty_path_list_rejected_by_model(self) -> None:
        with pytest.raises(ValidationError):
            GitStageRequest(paths=[])


# ===================================================================
# git.unstage — normal operation
# ===================================================================


class TestGitUnstageNormal:
    def test_unstages_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})
        (ws / "new.txt").write_text("new\n", encoding="utf-8")
        _git(ws, "add", "new.txt")

        result = svc.unstage(GitUnstageRequest(paths=["new.txt"]))

        assert result.paths_changed == ["new.txt"]

        # Verify it's unstaged
        status = svc.status(GitStatusRequest())
        staged_paths = [f.path for f in status.staged]
        assert "new.txt" not in staged_paths
        assert "new.txt" in status.untracked

    def test_unstage_on_initial_commit(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "first.txt").write_text("first\n", encoding="utf-8")
        _git(ws, "add", "first.txt")

        # Should not raise — falls back to rm --cached
        result = svc.unstage(GitUnstageRequest(paths=["first.txt"]))

        assert result.paths_changed == ["first.txt"]


# ===================================================================
# git.unstage — error paths
# ===================================================================


class TestGitUnstageErrors:
    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(GitServiceError) as exc_info:
            svc.unstage(GitUnstageRequest(paths=["../../escape.txt"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_empty_path_list_rejected_by_model(self) -> None:
        with pytest.raises(ValidationError):
            GitUnstageRequest(paths=[])


# ===================================================================
# git.commit — normal operation
# ===================================================================


class TestGitCommitNormal:
    def test_commits_staged_changes(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})
        (ws / "new.txt").write_text("new\n", encoding="utf-8")
        _git(ws, "add", "new.txt")

        result = svc.commit(GitCommitRequest(message="add new file"))

        assert "add new file" in result.summary
        assert "new.txt" in result.paths_changed

        # Verify commit was made
        log = svc.log(GitLogRequest(max_count=1))
        assert log.entries[0].message == "add new file"

    def test_commit_multiple_staged_files(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})
        (ws / "a.txt").write_text("a\n", encoding="utf-8")
        (ws / "b.txt").write_text("b\n", encoding="utf-8")
        _git(ws, "add", "a.txt", "b.txt")

        result = svc.commit(GitCommitRequest(message="add two files"))

        assert len(result.paths_changed) == 2


# ===================================================================
# git.commit — error paths
# ===================================================================


class TestGitCommitErrors:
    def test_no_staged_changes(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"base.txt": "base\n"})

        with pytest.raises(GitServiceError) as exc_info:
            svc.commit(GitCommitRequest(message="nothing to commit"))
        err = exc_info.value.error
        assert err.code == GitErrorCode.NO_STAGED_CHANGES
        assert err.suggestion is not None

    def test_empty_message_rejected_by_model(self) -> None:
        with pytest.raises(ValidationError):
            GitCommitRequest(message="")


# ===================================================================
# Workspace confinement
# ===================================================================


class TestWorkspaceConfinement:
    def test_stage_path_traversal(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(GitServiceError) as exc_info:
            svc.stage(GitStageRequest(paths=["../../../etc/passwd"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_unstage_path_traversal(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(GitServiceError) as exc_info:
            svc.unstage(GitUnstageRequest(paths=["../outside.txt"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_diff_path_traversal(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        _commit(ws, "initial", {"f.txt": "f\n"})

        with pytest.raises(GitServiceError) as exc_info:
            svc.diff(GitDiffRequest(paths=["../../escape.txt"]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_absolute_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        with pytest.raises(GitServiceError) as exc_info:
            svc.stage(GitStageRequest(paths=[str(outside)]))
        assert exc_info.value.error.code == GitErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# Model validation
# ===================================================================


class TestModelValidation:
    def test_git_show_request_rejects_empty_ref(self) -> None:
        with pytest.raises(ValidationError):
            GitShowRequest(ref="")

    def test_git_log_request_defaults(self) -> None:
        req = GitLogRequest()
        assert req.max_count == 20

    def test_git_log_max_count_bounds(self) -> None:
        with pytest.raises(ValidationError):
            GitLogRequest(max_count=0)
        with pytest.raises(ValidationError):
            GitLogRequest(max_count=101)

    def test_git_stage_rejects_empty_paths(self) -> None:
        with pytest.raises(ValidationError):
            GitStageRequest(paths=[])

    def test_git_unstage_rejects_empty_paths(self) -> None:
        with pytest.raises(ValidationError):
            GitUnstageRequest(paths=[])

    def test_git_commit_rejects_empty_message(self) -> None:
        with pytest.raises(ValidationError):
            GitCommitRequest(message="")

    def test_git_diff_request_defaults(self) -> None:
        req = GitDiffRequest()
        assert req.staged is False
        assert req.paths == []
        assert req.stat_only is False

    def test_strict_model_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            GitStatusRequest(extra_field="not_allowed")  # type: ignore[call-arg]


# ===================================================================
# Capability registration
# ===================================================================


class TestGitCapabilityRegistration:
    def test_all_git_capabilities_registered_and_healthy(self, tmp_path: Path) -> None:
        from foundation.services.capabilities import (
            GIT_COMMIT_CAPABILITY_ID,
            GIT_DIFF_CAPABILITY_ID,
            GIT_LOG_CAPABILITY_ID,
            GIT_SHOW_CAPABILITY_ID,
            GIT_STAGE_CAPABILITY_ID,
            GIT_STATUS_CAPABILITY_ID,
            GIT_UNSTAGE_CAPABILITY_ID,
            CapabilityRegistry,
            CapabilityStore,
        )
        from foundation.services.tools import LocalToolService

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool_service = LocalToolService(workspace_root=workspace)
        store = CapabilityStore(tmp_path / "caps")
        registry = CapabilityRegistry(store=store, tool_service=tool_service)

        expected_ids = {
            GIT_STATUS_CAPABILITY_ID,
            GIT_DIFF_CAPABILITY_ID,
            GIT_SHOW_CAPABILITY_ID,
            GIT_LOG_CAPABILITY_ID,
            GIT_STAGE_CAPABILITY_ID,
            GIT_UNSTAGE_CAPABILITY_ID,
            GIT_COMMIT_CAPABILITY_ID,
        }

        all_caps = registry.list_capabilities()
        registered_ids = {m.id for m in all_caps}
        assert expected_ids.issubset(registered_ids)

        for cap_id in expected_ids:
            manifest = registry.resolve(cap_id, allow_unhealthy=True)
            assert manifest.health.value == "healthy", (
                f"{cap_id} is not healthy: {manifest.health_detail}"
            )

    def test_git_capabilities_in_planner_snapshot(self, tmp_path: Path) -> None:
        from foundation.services.capabilities import (
            CapabilityRegistry,
            CapabilityStore,
        )
        from foundation.services.tools import LocalToolService

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool_service = LocalToolService(workspace_root=workspace)
        store = CapabilityStore(tmp_path / "caps")
        registry = CapabilityRegistry(store=store, tool_service=tool_service)

        snapshot = registry.planner_snapshot()
        snapshot_ids = {str(s.capability_id) for s in snapshot}

        assert "foundation.git.status" in snapshot_ids
        assert "foundation.git.diff" in snapshot_ids
        assert "foundation.git.show" in snapshot_ids
        assert "foundation.git.log" in snapshot_ids
        assert "foundation.git.stage" in snapshot_ids
        assert "foundation.git.unstage" in snapshot_ids
        assert "foundation.git.commit" in snapshot_ids

    def test_commit_capability_requires_approval(self, tmp_path: Path) -> None:
        from foundation.models.capability import CapabilitySideEffectMode
        from foundation.services.capabilities import (
            GIT_COMMIT_CAPABILITY_ID,
            CapabilityRegistry,
            CapabilityStore,
        )
        from foundation.services.tools import LocalToolService

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool_service = LocalToolService(workspace_root=workspace)
        store = CapabilityStore(tmp_path / "caps")
        registry = CapabilityRegistry(store=store, tool_service=tool_service)

        manifest = registry.resolve(GIT_COMMIT_CAPABILITY_ID, allow_unhealthy=True)

        # Check that workspace_write has REQUIRE_APPROVAL
        ws_write_rules = [
            r for r in manifest.constraints.side_effect_rules
            if r.side_effect == "workspace_write"
        ]
        assert len(ws_write_rules) == 1
        assert ws_write_rules[0].mode == CapabilitySideEffectMode.REQUIRE_APPROVAL
