"""Tests for the append-only agent-action ledger."""

from __future__ import annotations

import json
from pathlib import Path

from foundation.ledger import Ledger, LedgerEntry, build_entry
from foundation.models import (
    ActionKind,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    PlannedAction,
    ShellAction,
    ToolCall,
)


def _tool_action(capability_id: str, arguments: dict) -> PlannedAction:
    return PlannedAction(
        id="a1",
        kind=ActionKind.TOOL_CALL,
        summary="dispatch",
        tool_call=ToolCall(capability_id=capability_id, arguments=arguments),
    )


def _shell_action(command: str, args: list[str]) -> PlannedAction:
    return PlannedAction(
        id="sh1",
        kind=ActionKind.SHELL,
        summary="run a shell command",
        shell=ShellAction(command=command, args=args),
    )


def _executed_result(action_id: str, summary: str = "ok") -> ExecutionResult:
    return ExecutionResult(
        action_id=action_id,
        status=ExecutionStatus.EXECUTED,
        summary=summary,
    )


def test_ledger_record_creates_jsonl_with_one_record_per_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "actions.jsonl"
    ledger = Ledger(path=path)

    ledger.record(
        build_entry(
            _tool_action("foundation.file.read", {"path": "x.txt"}),
            _executed_result("a1", summary="Read x.txt"),
        )
    )
    ledger.record(
        build_entry(
            _shell_action("pytest", ["-q"]),
            _executed_result("sh1", summary="pytest ran"),
        )
    )

    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["action_id"] == "a1"
    assert first["action_type"] == "tool_call"
    assert first["capability_id"] == "foundation.file.read"
    assert first["status"] == "executed"
    assert "x.txt" in first["input_summary"]

    second = json.loads(lines[1])
    assert second["action_id"] == "sh1"
    assert second["action_type"] == "shell"
    assert second["capability_id"] is None
    assert "pytest" in second["input_summary"]


def test_ledger_record_is_append_only_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    first_ledger = Ledger(path=path)
    first_ledger.record(
        build_entry(
            _tool_action("foundation.file.read", {"path": "first.txt"}),
            _executed_result("a1"),
        )
    )

    # Open a second ledger handle on the same path — old entries must survive.
    second_ledger = Ledger(path=path)
    second_ledger.record(
        build_entry(
            _tool_action("foundation.file.read", {"path": "second.txt"}),
            _executed_result("a2"),
        )
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "first.txt" in lines[0]
    assert "second.txt" in lines[1]


def test_ledger_redacts_dict_key_secrets(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    ledger = Ledger(path=path)

    ledger.record(
        build_entry(
            _tool_action(
                "foundation.file.write",
                {"path": "config.toml", "api_key": "super-secret-value-12345"},
            ),
            _executed_result("a1"),
        )
    )

    line = path.read_text(encoding="utf-8").strip()
    assert "super-secret-value-12345" not in line
    assert "[redacted]" in line


def test_ledger_redacts_text_level_secret_patterns(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    ledger = Ledger(path=path)

    # An OpenAI-style key embedded in a free-form string field. The
    # dict-level redactor (which only masks values under sensitive keys)
    # would not catch this; only the text-level scrubber does.
    ledger.record(
        build_entry(
            _tool_action(
                "foundation.file.write",
                {
                    "path": "notes.md",
                    "content": "remember: sk-abcdefghijklmnopqrstuvwxyz1234567890ABCD",
                },
            ),
            _executed_result("a1"),
        )
    )

    line = path.read_text(encoding="utf-8").strip()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in line
    assert "[redacted]" in line


def test_ledger_redacts_bearer_and_jwt_patterns_in_output_summary(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    ledger = Ledger(path=path)

    bearer = "Bearer abcdef1234567890abcdef1234567890"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SignaturePartHere1234"
    result = ExecutionResult(
        action_id="a1",
        status=ExecutionStatus.EXECUTED,
        summary=f"Server replied with {bearer} and token {jwt}",
    )

    ledger.record(build_entry(_tool_action("foundation.file.read", {"path": "x"}), result))

    line = path.read_text(encoding="utf-8").strip()
    assert bearer not in line
    assert jwt not in line


def test_ledger_truncates_oversized_records(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    ledger = Ledger(path=path, max_record_bytes=512)

    huge = "x" * 5000
    ledger.record(
        build_entry(
            _tool_action("foundation.file.write", {"path": "big.txt", "content": huge}),
            ExecutionResult(
                action_id="a1",
                status=ExecutionStatus.EXECUTED,
                summary=huge,
            ),
        )
    )

    line = path.read_text(encoding="utf-8").strip()
    # Stays under the cap; the parsed record is still valid JSON.
    assert len(line.encode("utf-8")) <= 512
    parsed = json.loads(line)
    assert parsed["action_id"] == "a1"
    assert parsed["status"] == "executed"


def test_build_entry_fills_required_fields_for_tool_call() -> None:
    action = _tool_action("foundation.git.status", {})
    result = ExecutionResult(
        action_id="a1",
        status=ExecutionStatus.EXECUTED,
        summary="Branch main, clean",
        artifact_type=ExecutionArtifactType.GIT_STATUS,
        artifact={"branch": "main"},
    )

    entry = build_entry(action, result)

    assert isinstance(entry, LedgerEntry)
    assert entry.action_id == "a1"
    assert entry.action_type == "tool_call"
    assert entry.capability_id == "foundation.git.status"
    assert entry.status == "executed"
    assert "main" in entry.output_summary
    assert entry.error is None


def test_build_entry_captures_error_for_failed_action() -> None:
    action = _tool_action("foundation.file.read", {"path": "missing.txt"})
    result = ExecutionResult(
        action_id="a1",
        status=ExecutionStatus.FAILED,
        summary="File operation failed: missing",
        error="missing.txt does not exist",
    )

    entry = build_entry(action, result)

    assert entry.status == "failed"
    assert entry.error == "missing.txt does not exist"


def test_ledger_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "actions.jsonl"
    Ledger(path=nested)  # constructor mkdirs

    assert nested.parent.is_dir()
