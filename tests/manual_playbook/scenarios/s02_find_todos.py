"""S2 — find TODO comments under ``src/``.

Ground truth in this synthetic workspace: no TODOs exist. The scenario
fails if the assistant invents file paths that don't exist. Uses
``foundation.search`` as the preferred capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.graders import (
    assert_capability_used,
    assert_no_invented_paths,
    assert_session_outcome,
)
from tests.manual_playbook.provider_stubs import (
    provider_response,
    zero_action_response,
)
from tests.manual_playbook.scenarios._base import Scenario

from foundation.models import ProviderResponse

PROMPT = "Find TODO comments under src/ and list the file paths."


def setup(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    del monkeypatch  # unused for S2
    src = workspace / "src"
    src.mkdir()
    (src / "mod_a.py").write_text("def a(): return 1\n", encoding="utf-8")
    (src / "mod_b.py").write_text("def b(): return 2\n", encoding="utf-8")
    return {"expected_hits": 0}


def stub_responses(workspace: Path) -> list[ProviderResponse]:
    plan = {
        "assistant_message": "Searching for TODOs.",
        "actions": [
            {
                "id": "search",
                "kind": "tool_call",
                "summary": "Search for TODO",
                "tool_call": {
                    "capability_id": "foundation.search",
                    "arguments": {
                        "query": "TODO",
                        "scope": str(workspace / "src"),
                    },
                },
            }
        ],
    }
    summary = zero_action_response(
        "No TODO comments were found under src/."
    )
    return [provider_response(plan), summary]


SCENARIO = Scenario(
    name="s02_find_todos",
    prompt=PROMPT,
    setup=setup,
    stub_responses=stub_responses,
    graders=[
        assert_no_invented_paths(path_roots=[]),  # roots filled at run time
        assert_session_outcome(
            assistant_message_contains=["no todo"],
        ),
        assert_capability_used("foundation.search", at_least=1),
    ],
)
