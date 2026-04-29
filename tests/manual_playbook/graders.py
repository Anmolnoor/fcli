"""Structured grader helpers for playbook scenarios.

Each grader takes a :class:`GradeContext` (the orchestration result plus
workspace handles) and returns a :class:`GradeOutcome`. Scenarios compose
graders via ``Scenario.graders``. Failures are reported with a short,
actionable reason so both CI failures and live-mode Markdown verdicts stay
readable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foundation.models import (
    ExecutionResult,
    OrchestrationResult,
    PlannedAction,
    SessionStatus,
)


@dataclass(frozen=True)
class GradeContext:
    scenario_name: str
    workspace_root: Path
    result: OrchestrationResult
    session_status: SessionStatus
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GradeOutcome:
    name: str
    passed: bool
    reason: str = ""
    severity: str = "error"  # "error" | "warning"

    def render(self) -> str:
        marker = "PASS" if self.passed else ("WARN" if self.severity == "warning" else "FAIL")
        suffix = f" — {self.reason}" if self.reason else ""
        return f"[{marker}] {self.name}{suffix}"


Grader = Callable[[GradeContext], GradeOutcome]


def assert_no_invented_paths(path_roots: Iterable[Path]) -> Grader:
    """Fail when the assistant message mentions filesystem paths that don't exist.

    Path detection is conservative: we look for tokens containing at least one
    path separator and a recognizable file extension or known directory marker.
    """
    roots = [Path(p).resolve() for p in path_roots]
    pattern = re.compile(r"(?:/|\./|\b)[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}\b")

    def _grader(ctx: GradeContext) -> GradeOutcome:
        message = _message_text(ctx.result.assistant_message)
        candidates = {token for token in pattern.findall(message)}
        # Keep only candidates that look like they reference real filesystem paths.
        candidates = {c for c in candidates if "/" in c or c.endswith((".py", ".md", ".toml"))}

        missing: list[str] = []
        for candidate in sorted(candidates):
            resolved_hits = [
                (root / candidate.lstrip("./")).resolve()
                for root in roots
            ]
            if not any(hit.exists() for hit in resolved_hits):
                # Try as an absolute path as well.
                if Path(candidate).exists():
                    continue
                missing.append(candidate)

        if missing:
            return GradeOutcome(
                name="no_invented_paths",
                passed=False,
                reason=f"assistant mentioned paths that don't exist: {missing}",
            )
        return GradeOutcome(name="no_invented_paths", passed=True)

    return _grader


def assert_covers_behaviors(required_behaviors: dict[str, list[str]]) -> Grader:
    """Fail when assistant_message doesn't cover each named behavior.

    ``required_behaviors`` maps a behavior name to a list of lowercase keyword
    alternatives. A behavior is considered covered if any of its alternatives
    appears (case-insensitively) in the assistant message.
    """

    def _grader(ctx: GradeContext) -> GradeOutcome:
        message = _message_text(ctx.result.assistant_message).lower()
        missing = [
            name
            for name, alternatives in required_behaviors.items()
            if not any(alt.lower() in message for alt in alternatives)
        ]
        if missing:
            return GradeOutcome(
                name="covers_behaviors",
                passed=False,
                reason=f"missing behavior(s): {missing}",
            )
        return GradeOutcome(name="covers_behaviors", passed=True)

    return _grader


def assert_capability_used(capability_id: str, *, at_least: int = 1) -> Grader:
    def _grader(ctx: GradeContext) -> GradeOutcome:
        count = _count_capability_calls(ctx.result, capability_id)
        if count < at_least:
            return GradeOutcome(
                name=f"uses_{capability_id}",
                passed=False,
                reason=f"expected >={at_least} calls, saw {count}",
            )
        return GradeOutcome(name=f"uses_{capability_id}", passed=True)

    return _grader


def assert_no_shell_equivalent(banned_commands: set[str]) -> Grader:
    """Flag shell actions whose ``command`` has a clearly-typed equivalent."""

    def _grader(ctx: GradeContext) -> GradeOutcome:
        hits: list[str] = []
        for action in _iter_planned_actions(ctx.result):
            if action.shell is not None and action.shell.command in banned_commands:
                hits.append(action.shell.command)
        if hits:
            return GradeOutcome(
                name="no_shell_equivalent",
                passed=False,
                reason=f"shell command(s) with typed equivalent used: {hits}",
            )
        return GradeOutcome(name="no_shell_equivalent", passed=True)

    return _grader


def assert_subprocess_env_clean(env_dump_path_key: str = "env_dump_path") -> Grader:
    """Read a sidecar JSON dumped by the scenario's wrapper script and
    assert no ``FOUNDATION_*`` keys survived into the subprocess env.
    """

    def _grader(ctx: GradeContext) -> GradeOutcome:
        dump_path = ctx.artifacts.get(env_dump_path_key)
        if dump_path is None:
            return GradeOutcome(
                name="subprocess_env_clean",
                passed=False,
                reason=f"scenario did not record '{env_dump_path_key}'",
            )
        dump_file = Path(dump_path)
        if not dump_file.exists():
            return GradeOutcome(
                name="subprocess_env_clean",
                passed=False,
                reason=f"env dump missing at {dump_file}",
            )
        payload = json.loads(dump_file.read_text(encoding="utf-8"))
        leaked = sorted(k for k in payload if k.startswith("FOUNDATION_"))
        if leaked:
            return GradeOutcome(
                name="subprocess_env_clean",
                passed=False,
                reason=f"FOUNDATION_* leaked into subprocess: {leaked}",
            )
        return GradeOutcome(name="subprocess_env_clean", passed=True)

    return _grader


def assert_session_outcome(
    *,
    assistant_message_contains: list[str] | None = None,
    allow_zero_actions: bool = True,
) -> Grader:
    """Generic outcome grader — correctness-first, action-count is secondary."""

    def _grader(ctx: GradeContext) -> GradeOutcome:
        message = _message_text(ctx.result.assistant_message)
        for needle in assistant_message_contains or []:
            if needle.lower() not in message.lower():
                return GradeOutcome(
                    name="session_outcome",
                    passed=False,
                    reason=f"assistant_message missing '{needle}'",
                )
        if not allow_zero_actions:
            total_actions = sum(
                len(it.plan.actions) for it in ctx.result.iterations
            )
            if total_actions == 0:
                return GradeOutcome(
                    name="session_outcome",
                    passed=False,
                    reason="no actions planned but scenario requires at least one",
                )
        return GradeOutcome(name="session_outcome", passed=True)

    return _grader


def _iter_planned_actions(result: OrchestrationResult) -> list[PlannedAction]:
    actions: list[PlannedAction] = []
    for iteration in result.iterations:
        actions.extend(iteration.plan.actions)
    return actions


def _count_capability_calls(result: OrchestrationResult, capability_id: str) -> int:
    count = 0
    for action in _iter_planned_actions(result):
        if action.tool_call is not None and action.tool_call.capability_id == capability_id:
            count += 1
    return count


def executed_statuses(result: OrchestrationResult) -> list[ExecutionResult]:
    """Convenience re-export so scenarios can introspect without importing models."""
    return list(result.execution_results)


def _message_text(assistant_message: Any) -> str:
    content = getattr(assistant_message, "content", assistant_message)
    return content or ""
