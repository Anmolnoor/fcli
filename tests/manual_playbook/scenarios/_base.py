"""Shared scenario dataclass for the playbook."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pytest
from tests.manual_playbook.graders import Grader

from foundation.models import ProviderResponse, UserRequest
from foundation.settings import ApprovalMode


class SetupFn(Protocol):
    def __call__(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    setup: SetupFn
    stub_responses: Callable[[Path], list[ProviderResponse]]
    graders: list[Grader] = field(default_factory=list)
    approval_mode: ApprovalMode = ApprovalMode.AUTO

    def build_user_request(self) -> UserRequest:
        return UserRequest(message=self.prompt)
