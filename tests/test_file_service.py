"""Tests for v3 Stage 2 file capabilities — FileService and models."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundation.models.file import (
    FileApplyDiffRequest,
    FileEditRequest,
    FileErrorCode,
    FileReadChunkRequest,
    FileReadRequest,
    FileServiceError,
    FileWriteRequest,
)
from foundation.services.file_service import FileService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> tuple[FileService, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    service = FileService(workspace_root=workspace, state_dir=state_dir)
    return service, workspace


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ===================================================================
# file.read — normal operation
# ===================================================================


class TestFileReadNormal:
    def test_returns_content_encoding_lines_sha256(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        content = "line one\nline two\nline three\n"
        (ws / "hello.txt").write_text(content, encoding="utf-8")

        result = svc.read(FileReadRequest(path="hello.txt"))

        assert result.content == content
        assert result.encoding == "utf-8"
        assert result.line_count == 3
        assert result.size_bytes == len(content.encode("utf-8"))
        assert result.sha256 == _sha256(content)
        assert result.path == str(ws / "hello.txt")

    def test_reads_utf8_bom_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        content = "bom content\n"
        raw = b"\xef\xbb\xbf" + content.encode("utf-8")
        (ws / "bom.txt").write_bytes(raw)

        result = svc.read(FileReadRequest(path="bom.txt"))

        assert result.content == content
        assert result.encoding == "utf-8-sig"
        assert result.sha256 == _sha256(content)

    def test_reads_empty_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "empty.txt").write_text("", encoding="utf-8")

        result = svc.read(FileReadRequest(path="empty.txt"))

        assert result.content == ""
        assert result.line_count == 0
        assert result.size_bytes == 0

    def test_reads_with_absolute_workspace_path(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        content = "absolute\n"
        (ws / "abs.txt").write_text(content, encoding="utf-8")

        result = svc.read(FileReadRequest(path=str(ws / "abs.txt")))

        assert result.content == content


# ===================================================================
# file.read — error paths
# ===================================================================


class TestFileReadErrors:
    def test_file_not_found(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="missing.txt"))
        assert exc_info.value.error.code == FileErrorCode.FILE_NOT_FOUND

    def test_file_too_large(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "big.txt").write_bytes(b"x" * (256 * 1024 + 1))

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="big.txt"))
        err = exc_info.value.error
        assert err.code == FileErrorCode.FILE_TOO_LARGE
        assert err.suggestion is not None
        assert "read_chunk" in err.suggestion

    def test_binary_file_nul_bytes(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "binary.dat").write_bytes(b"hello\x00world")

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="binary.dat"))
        assert exc_info.value.error.code == FileErrorCode.NOT_TEXT

    def test_unsupported_encoding(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        # Latin-1 encoded bytes invalid as UTF-8
        (ws / "latin.txt").write_bytes(b"caf\xe9\n")

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="latin.txt"))
        assert exc_info.value.error.code == FileErrorCode.UNSUPPORTED_ENCODING

    def test_path_traversal_outside_workspace(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="../secret.txt"))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_absolute_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path=str(outside)))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_symlink_escaping_workspace(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        link = ws / "link.txt"
        link.symlink_to(secret)

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="link.txt"))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# file.read_chunk — normal operation
# ===================================================================


class TestFileReadChunkNormal:
    def _write_numbered(self, ws: Path, name: str, n: int) -> str:
        lines = [f"line {i}\n" for i in range(1, n + 1)]
        content = "".join(lines)
        (ws / name).write_text(content, encoding="utf-8")
        return content

    def test_default_chunk(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        content = self._write_numbered(ws, "big.txt", 500)

        result = svc.read_chunk(FileReadChunkRequest(path="big.txt"))

        assert result.start_line == 1
        assert result.end_line == 200
        assert result.total_lines == 500
        assert result.sha256 == _sha256(content)
        # Should contain 200 lines
        assert result.content.count("\n") == 200

    def test_custom_start_and_max(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        self._write_numbered(ws, "big.txt", 500)

        result = svc.read_chunk(FileReadChunkRequest(
            path="big.txt", start_line=100, max_lines=50,
        ))

        assert result.start_line == 100
        assert result.end_line == 149
        assert result.total_lines == 500
        assert "line 100\n" in result.content
        assert "line 149\n" in result.content

    def test_past_eof_returns_empty(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        self._write_numbered(ws, "small.txt", 10)

        result = svc.read_chunk(FileReadChunkRequest(
            path="small.txt", start_line=100,
        ))

        assert result.content == ""
        assert result.end_line == 0
        assert result.total_lines == 10

    def test_near_eof_partial_chunk(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        self._write_numbered(ws, "big.txt", 500)

        result = svc.read_chunk(FileReadChunkRequest(
            path="big.txt", start_line=490, max_lines=200,
        ))

        assert result.start_line == 490
        assert result.end_line == 500
        assert result.total_lines == 500
        assert "line 500\n" in result.content

    def test_large_file_over_256kb(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        # Create a file that exceeds 256 KB
        line = "x" * 100 + "\n"
        count = (256 * 1024 // len(line)) + 100
        content = line * count
        (ws / "huge.txt").write_text(content, encoding="utf-8")

        result = svc.read_chunk(FileReadChunkRequest(path="huge.txt"))

        assert result.start_line == 1
        assert result.end_line == 200
        assert result.total_lines == count


# ===================================================================
# file.read_chunk — error paths
# ===================================================================


class TestFileReadChunkErrors:
    def test_max_lines_over_400_rejected_by_model(self) -> None:
        with pytest.raises(ValidationError):
            FileReadChunkRequest(path="x.txt", max_lines=401)

    def test_binary_file_rejected(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "bin.dat").write_bytes(b"\x00" * 100)

        with pytest.raises(FileServiceError) as exc_info:
            svc.read_chunk(FileReadChunkRequest(path="bin.dat"))
        assert exc_info.value.error.code == FileErrorCode.NOT_TEXT

    def test_file_not_found(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.read_chunk(FileReadChunkRequest(path="nope.txt"))
        assert exc_info.value.error.code == FileErrorCode.FILE_NOT_FOUND


# ===================================================================
# file.write — normal operation
# ===================================================================


class TestFileWriteNormal:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)

        result = svc.write(FileWriteRequest(path="new.txt", content="hello\n"))

        assert result.created is True
        assert (ws / "new.txt").read_text(encoding="utf-8") == "hello\n"
        assert result.sha256 == _sha256("hello\n")
        assert result.line_count == 1
        assert "new file" in result.diff_summary

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)

        result = svc.write(FileWriteRequest(path="sub/deep/file.txt", content="deep\n"))

        assert result.created is True
        assert (ws / "sub" / "deep" / "file.txt").read_text(encoding="utf-8") == "deep\n"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "existing.txt").write_text("old\n", encoding="utf-8")

        result = svc.write(FileWriteRequest(
            path="existing.txt", content="new\n", overwrite=True,
        ))

        assert result.created is False
        assert (ws / "existing.txt").read_text(encoding="utf-8") == "new\n"
        assert "+" in result.diff_summary and "-" in result.diff_summary

    def test_write_empty_file(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)

        result = svc.write(FileWriteRequest(path="empty.txt", content=""))

        assert result.created is True
        assert (ws / "empty.txt").read_text(encoding="utf-8") == ""
        assert result.line_count == 0


# ===================================================================
# file.write — error paths
# ===================================================================


class TestFileWriteErrors:
    def test_existing_without_overwrite(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "exists.txt").write_text("content", encoding="utf-8")

        with pytest.raises(FileServiceError) as exc_info:
            svc.write(FileWriteRequest(path="exists.txt", content="new"))
        assert exc_info.value.error.code == FileErrorCode.FILE_EXISTS

    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.write(FileWriteRequest(path="../escape.txt", content="x"))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# file.edit — normal operation
# ===================================================================


class TestFileEditNormal:
    def test_matching_sha256_succeeds(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        old = "original\n"
        (ws / "file.txt").write_text(old, encoding="utf-8")
        sha = _sha256(old)

        result = svc.edit(FileEditRequest(
            path="file.txt", content="updated\n", expected_sha256=sha,
        ))

        assert (ws / "file.txt").read_text(encoding="utf-8") == "updated\n"
        assert result.sha256 == _sha256("updated\n")
        assert "+" in result.diff_summary

    def test_preserves_permissions(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        f = ws / "script.sh"
        f.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        f.chmod(0o755)
        sha = _sha256(f.read_text(encoding="utf-8"))

        svc.edit(FileEditRequest(
            path="script.sh", content="#!/bin/sh\necho new\n", expected_sha256=sha,
        ))

        assert f.stat().st_mode & 0o777 == 0o755

    def test_diff_summary_format(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        old = "a\nb\nc\n"
        (ws / "f.txt").write_text(old, encoding="utf-8")
        sha = _sha256(old)

        result = svc.edit(FileEditRequest(
            path="f.txt", content="a\nX\nc\n", expected_sha256=sha,
        ))

        # Should show additions and removals
        assert result.diff_summary.startswith("+")


# ===================================================================
# file.edit — error paths
# ===================================================================


class TestFileEditErrors:
    def test_nonexistent_file(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.edit(FileEditRequest(
                path="missing.txt",
                content="x",
                expected_sha256="a" * 64,
            ))
        assert exc_info.value.error.code == FileErrorCode.FILE_NOT_FOUND

    def test_stale_sha256_raises_conflict(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("content\n", encoding="utf-8")

        with pytest.raises(FileServiceError) as exc_info:
            svc.edit(FileEditRequest(
                path="file.txt",
                content="new",
                expected_sha256="b" * 64,
            ))
        err = exc_info.value.error
        assert err.code == FileErrorCode.SHA256_CONFLICT
        assert err.detail is not None
        assert "Expected" in err.detail

    def test_path_outside_workspace(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.edit(FileEditRequest(
                path="../escape.txt",
                content="x",
                expected_sha256="a" * 64,
            ))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# file.apply_diff — normal operation
# ===================================================================


class TestFileApplyDiffNormal:
    def test_single_hunk(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("a\nb\nc\n", encoding="utf-8")

        diff = (
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            " c\n"
        )
        result = svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))

        assert (ws / "file.txt").read_text(encoding="utf-8") == "a\nB\nc\n"
        assert result.sha256 == _sha256("a\nB\nc\n")

    def test_multiple_hunks(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        original = "".join(f"line{i}\n" for i in range(1, 11))
        (ws / "file.txt").write_text(original, encoding="utf-8")

        diff = (
            "@@ -2,3 +2,3 @@\n"
            " line2\n"
            "-line3\n"
            "+LINE3\n"
            " line4\n"
            "@@ -8,3 +8,3 @@\n"
            " line8\n"
            "-line9\n"
            "+LINE9\n"
            " line10\n"
        )
        result = svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))

        new_content = (ws / "file.txt").read_text(encoding="utf-8")
        assert "LINE3\n" in new_content
        assert "LINE9\n" in new_content
        assert "line3\n" not in new_content
        assert "line9\n" not in new_content
        assert result.diff_summary == "+2 -2 lines"

    def test_adding_lines(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("a\nb\n", encoding="utf-8")

        diff = (
            "@@ -1,2 +1,4 @@\n"
            " a\n"
            "+x\n"
            "+y\n"
            " b\n"
        )
        result = svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))

        assert (ws / "file.txt").read_text(encoding="utf-8") == "a\nx\ny\nb\n"
        assert "+2 -0 lines" in result.diff_summary


# ===================================================================
# file.apply_diff — error paths
# ===================================================================


class TestFileApplyDiffErrors:
    def test_context_mismatch(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("a\nb\nc\n", encoding="utf-8")

        diff = (
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-WRONG\n"
            "+B\n"
            " c\n"
        )
        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))
        assert exc_info.value.error.code == FileErrorCode.DIFF_APPLY_FAILED

    def test_no_partial_changes_on_failure(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        original = "a\nb\nc\nd\ne\n"
        (ws / "file.txt").write_text(original, encoding="utf-8")

        # First hunk matches, second does not
        diff = (
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            "@@ -4,2 +4,2 @@\n"
            " d\n"
            "-WRONG\n"
            "+E\n"
        )
        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))
        assert exc_info.value.error.code == FileErrorCode.DIFF_APPLY_FAILED

        # Original content must be unchanged
        assert (ws / "file.txt").read_text(encoding="utf-8") == original

    def test_delete_only_rejected(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("a\nb\nc\n", encoding="utf-8")

        diff = (
            "@@ -1,3 +1,2 @@\n"
            " a\n"
            "-b\n"
            " c\n"
        )
        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff=diff))
        assert exc_info.value.error.code == FileErrorCode.DIFF_REJECTED

    def test_rename_diff_rejected(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "old.txt").write_text("a\n", encoding="utf-8")

        diff = (
            "--- a/old.txt\n"
            "+++ b/new.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+A\n"
        )
        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="old.txt", diff=diff))
        assert exc_info.value.error.code == FileErrorCode.DIFF_REJECTED

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        diff = "@@ -1,1 +1,1 @@\n-a\n+A\n"
        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="nope.txt", diff=diff))
        assert exc_info.value.error.code == FileErrorCode.FILE_NOT_FOUND

    def test_no_hunks_rejected(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        (ws / "file.txt").write_text("a\n", encoding="utf-8")

        with pytest.raises(FileServiceError) as exc_info:
            svc.apply_diff(FileApplyDiffRequest(path="file.txt", diff="nothing useful"))
        assert exc_info.value.error.code == FileErrorCode.DIFF_REJECTED


# ===================================================================
# Workspace confinement
# ===================================================================


class TestWorkspaceConfinement:
    def test_symlink_escape_on_write(self, tmp_path: Path) -> None:
        svc, ws = _make_service(tmp_path)
        target = tmp_path / "outside"
        target.mkdir()
        link = ws / "escape"
        link.symlink_to(target)

        with pytest.raises(FileServiceError) as exc_info:
            svc.write(FileWriteRequest(path="escape/file.txt", content="x"))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE

    def test_deep_traversal(self, tmp_path: Path) -> None:
        svc, _ = _make_service(tmp_path)

        with pytest.raises(FileServiceError) as exc_info:
            svc.read(FileReadRequest(path="sub/../../secret.txt"))
        assert exc_info.value.error.code == FileErrorCode.PATH_OUTSIDE_WORKSPACE


# ===================================================================
# Model validation
# ===================================================================


class TestModelValidation:
    def test_file_edit_sha256_normalized_to_lowercase(self) -> None:
        req = FileEditRequest(
            path="f.txt",
            content="x",
            expected_sha256="A" * 64,
        )
        assert req.expected_sha256 == "a" * 64

    def test_file_edit_rejects_invalid_sha256(self) -> None:
        with pytest.raises(ValidationError):
            FileEditRequest(path="f.txt", content="x", expected_sha256="short")

    def test_file_read_request_rejects_empty_path(self) -> None:
        with pytest.raises(ValidationError):
            FileReadRequest(path="")

    def test_file_write_request_defaults(self) -> None:
        req = FileWriteRequest(path="f.txt")
        assert req.content == ""
        assert req.overwrite is False

    def test_file_read_chunk_defaults(self) -> None:
        req = FileReadChunkRequest(path="f.txt")
        assert req.start_line == 1
        assert req.max_lines == 200


# ===================================================================
# Capability registration
# ===================================================================


class TestFileCapabilityRegistration:
    def test_all_file_capabilities_registered_and_healthy(self, tmp_path: Path) -> None:
        from foundation.services.capabilities import (
            FILE_APPLY_DIFF_CAPABILITY_ID,
            FILE_EDIT_CAPABILITY_ID,
            FILE_READ_CAPABILITY_ID,
            FILE_READ_CHUNK_CAPABILITY_ID,
            FILE_WRITE_CAPABILITY_ID,
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
            FILE_READ_CAPABILITY_ID,
            FILE_READ_CHUNK_CAPABILITY_ID,
            FILE_WRITE_CAPABILITY_ID,
            FILE_EDIT_CAPABILITY_ID,
            FILE_APPLY_DIFF_CAPABILITY_ID,
        }

        all_caps = registry.list_capabilities()
        registered_ids = {m.id for m in all_caps}
        assert expected_ids.issubset(registered_ids)

        for cap_id in expected_ids:
            manifest = registry.resolve(cap_id, allow_unhealthy=True)
            assert manifest.health.value == "healthy", (
                f"{cap_id} is not healthy: {manifest.health_detail}"
            )

    def test_file_capabilities_in_planner_snapshot(self, tmp_path: Path) -> None:
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

        assert "foundation.file.read" in snapshot_ids
        assert "foundation.file.read_chunk" in snapshot_ids
        assert "foundation.file.write" in snapshot_ids
        assert "foundation.file.edit" in snapshot_ids
        assert "foundation.file.apply_diff" in snapshot_ids
