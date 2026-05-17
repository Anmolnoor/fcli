from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from foundation.services import (
    FileDiscoveryRequest,
    GitContextRequest,
    HelpLookupRequest,
    HelpLookupSource,
    LocalToolService,
    SearchRequest,
    ToolAvailabilityStatus,
    ToolErrorCode,
    ToolExecutionError,
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: dict[str, str],
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in scripts.items():
        _write_executable(bin_dir / name, script)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _service(
    tmp_path: Path,
    *,
    path: str | None = None,
) -> tuple[LocalToolService, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
        environment=None if path is None else {"PATH": path},
    )
    return service, workspace_root


def test_availability_detection_accepts_fdfind_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "rg": "print('')",
            "fdfind": "print('')",
            "git": "print('')",
            "man": "print('')",
            "tldr": "print('')",
        },
    )

    service, _workspace_root = _service(tmp_path)
    availability = {item.name: item for item in service.availability_report()}

    assert availability["fd"].status is ToolAvailabilityStatus.AVAILABLE
    assert availability["fd"].resolved_command == "fdfind"


def test_search_filters_ignored_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "rg": """
                import json

                messages = [
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "keep.txt"},
                            "lines": {"text": "needle here\\n"},
                            "line_number": 1,
                            "submatches": [{"start": 0, "end": 6}],
                        },
                    },
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "ignored.log"},
                            "lines": {"text": "needle hidden\\n"},
                            "line_number": 2,
                            "submatches": [{"start": 0, "end": 6}],
                        },
                    },
                ]
                for item in messages:
                    print(json.dumps(item))
            """,
        },
    )
    service, workspace_root = _service(tmp_path)
    (workspace_root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")

    result = service.search(SearchRequest(query="needle"))

    assert [match.path for match in result.matches] == ["keep.txt"]
    assert result.truncated is False


def test_search_truncates_after_requested_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "rg": """
                import json

                messages = [
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "first.txt"},
                            "lines": {"text": "alpha\\n"},
                            "line_number": 1,
                            "submatches": [{"start": 0, "end": 5}],
                        },
                    },
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "second.txt"},
                            "lines": {"text": "beta\\n"},
                            "line_number": 2,
                            "submatches": [{"start": 0, "end": 4}],
                        },
                    },
                ]
                for item in messages:
                    print(json.dumps(item))
            """,
        },
    )
    service, _workspace_root = _service(tmp_path)

    result = service.search(SearchRequest(query="a", max_results=1))

    assert [match.path for match in result.matches] == ["first.txt"]
    assert result.truncated is True


def test_file_discovery_filters_ignored_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "fd": """
                print("keep.py")
                print("ignored.log")
            """,
        },
    )
    service, workspace_root = _service(tmp_path)
    (workspace_root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")

    result = service.discover_files(FileDiscoveryRequest(pattern="."))

    assert result.paths == ["keep.py"]


def test_git_context_parses_branch_status_diff_and_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "git": """
                import sys

                args = sys.argv[1:]
                if args[:2] == ["branch", "--show-current"]:
                    print("main")
                elif args[:2] == ["status", "--short"]:
                    print(" M tracked.py")
                    print("A  added.py")
                elif args[:3] == ["diff", "--numstat", "--cached"]:
                    print("1\\t0\\tadded.py")
                elif args[:2] == ["diff", "--numstat"]:
                    print("2\\t1\\ttracked.py")
                elif args[:1] == ["log"]:
                    print("0123456789abcdef\\t0123456\\tStage 4 commit")
                else:
                    sys.stderr.write(f"unexpected args: {args!r}")
                    raise SystemExit(2)
            """,
        },
    )
    service, workspace_root = _service(tmp_path)
    (workspace_root / "tracked.py").write_text("tracked\n", encoding="utf-8")
    (workspace_root / "added.py").write_text("added\n", encoding="utf-8")

    result = service.git_context(GitContextRequest())

    assert result.branch == "main"
    assert [entry.path for entry in result.status] == ["tracked.py", "added.py"]
    assert result.unstaged_diff[0].path == "tracked.py"
    assert result.unstaged_diff[0].additions == 2
    assert result.staged_diff[0].path == "added.py"
    assert result.recent_commits[0].short_sha == "0123456"


def test_git_context_normalizes_renamed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "git": """
                import sys

                args = sys.argv[1:]
                if args[:2] == ["branch", "--show-current"]:
                    print("main")
                elif args[:2] == ["status", "--short"]:
                    print("R  old_name.py -> new_name.py")
                elif args[:3] == ["diff", "--numstat", "--cached"]:
                    print("1\\t0\\told_name.py => new_name.py")
                elif args[:2] == ["diff", "--numstat"]:
                    print("2\\t1\\tsrc/{old.py => new.py}")
                elif args[:1] == ["log"]:
                    print("0123456789abcdef\\t0123456\\tRename file")
                else:
                    sys.stderr.write(f"unexpected args: {args!r}")
                    raise SystemExit(2)
            """,
        },
    )
    service, _workspace_root = _service(tmp_path)

    result = service.git_context(GitContextRequest())

    assert [entry.path for entry in result.status] == ["new_name.py"]
    assert [entry.path for entry in result.unstaged_diff] == ["src/new.py"]
    assert [entry.path for entry in result.staged_diff] == ["new_name.py"]


def test_lookup_help_raises_missing_binary_with_install_hint(tmp_path: Path) -> None:
    service, _workspace_root = _service(tmp_path, path="")

    with pytest.raises(ToolExecutionError) as exc_info:
        service.lookup_help(
            HelpLookupRequest(
                topic="git",
                source=HelpLookupSource.TLDR,
            )
        )

    assert exc_info.value.error.code is ToolErrorCode.MISSING_BINARY
    assert exc_info.value.error.install_hint is not None


def test_tool_scope_must_stay_within_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_binaries(
        tmp_path,
        monkeypatch,
        {
            "rg": "print('')",
        },
    )
    service, _workspace_root = _service(tmp_path)
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("outside\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as exc_info:
        service.search(SearchRequest(query="needle", scope=outside_path))

    assert exc_info.value.error.code is ToolErrorCode.INVALID_SCOPE


def test_local_tool_service_scrubs_foundation_env_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDATION_APP__STATE_DIR", "/leak/state")
    monkeypatch.setenv("FOUNDATION_HISTORY__DATABASE_PATH", "/leak/history.db")
    monkeypatch.setenv("UNRELATED_VAR", "keep-me")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    service = LocalToolService(workspace_root=workspace_root)

    env = service._environment
    assert not any(key.startswith("FOUNDATION_") for key in env)
    assert env.get("UNRELATED_VAR") == "keep-me"


def test_local_tool_service_pass_through_restores_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDATION_APP__STATE_DIR", "/legacy/state")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    service = LocalToolService(
        workspace_root=workspace_root,
        pass_through_foundation_env=True,
    )

    assert service._environment.get("FOUNDATION_APP__STATE_DIR") == "/legacy/state"


def test_local_tool_service_explicit_environment_overlay_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDATION_APP__STATE_DIR", "/ambient/state")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    service = LocalToolService(
        workspace_root=workspace_root,
        environment={"FOUNDATION_APP__STATE_DIR": "/explicit/state"},
    )

    assert service._environment.get("FOUNDATION_APP__STATE_DIR") == "/explicit/state"
