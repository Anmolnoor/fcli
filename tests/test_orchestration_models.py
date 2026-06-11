"""Kind/payload shape validation on PlannedAction (hardening stage 2).

The model validator must reject every cross-kind payload combination, so a
mismatched action can never reach policy evaluation or execution.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundation.models import (
    ActionKind,
    PlannedAction,
    QuestionAction,
    ShellAction,
    ToolCall,
)

_QUESTION = QuestionAction(prompt="Which file should I edit?")
_SHELL = ShellAction(command="ls")
_TOOL_CALL = ToolCall(capability_id="foundation.file.read", arguments={})


def _action(kind: ActionKind, **payloads: object) -> PlannedAction:
    return PlannedAction.model_validate(
        {
            "id": "a1",
            "kind": kind,
            "summary": "payload shape test",
            **payloads,
        }
    )


class TestValidShapes:
    def test_explanation_action_validates(self) -> None:
        action = _action(ActionKind.EXPLANATION, explanation="done")
        assert action.kind is ActionKind.EXPLANATION

    def test_shell_action_validates(self) -> None:
        action = _action(ActionKind.SHELL, shell=_SHELL)
        assert action.shell is not None

    def test_tool_call_action_validates(self) -> None:
        action = _action(ActionKind.TOOL_CALL, tool_call=_TOOL_CALL)
        assert action.tool_call is not None

    def test_question_action_validates(self) -> None:
        action = _action(ActionKind.QUESTION, question=_QUESTION)
        assert action.question is not None


class TestStrayQuestionPayload:
    def test_explanation_action_rejects_question_payload(self) -> None:
        with pytest.raises(ValidationError, match="question"):
            _action(ActionKind.EXPLANATION, explanation="done", question=_QUESTION)

    def test_shell_action_rejects_question_payload(self) -> None:
        with pytest.raises(ValidationError, match="question"):
            _action(ActionKind.SHELL, shell=_SHELL, question=_QUESTION)

    def test_tool_call_action_rejects_question_payload(self) -> None:
        with pytest.raises(ValidationError, match="question"):
            _action(ActionKind.TOOL_CALL, tool_call=_TOOL_CALL, question=_QUESTION)


class TestStrayExplanationPayload:
    def test_question_action_rejects_explanation_payload(self) -> None:
        with pytest.raises(ValidationError, match="explanation"):
            _action(ActionKind.QUESTION, question=_QUESTION, explanation="stray")
