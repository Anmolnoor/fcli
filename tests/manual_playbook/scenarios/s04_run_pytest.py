"""S4 — run pytest verification.

The scenario installs a wrapper ``pytest`` on PATH that dumps its own
environment to a sidecar JSON before exiting with the configured code.
Grader reads the dump and fails if any ``FOUNDATION_*`` key survived,
proving the §1 env scrub actually reaches the subprocess.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.graders import (
    assert_session_outcome,
    assert_subprocess_env_clean,
)
from tests.manual_playbook.provider_stubs import (
    provider_response,
    zero_action_response,
)
from tests.manual_playbook.scenarios._base import Scenario

from foundation.models import ProviderResponse

PROMPT = "Run pytest and report whether the tests pass."


def install_wrapper_pytest(
    bin_dir: Path,
    *,
    env_dump_path: Path,
    exit_code: int = 0,
) -> Path:
    """Write a shim ``pytest`` that dumps ``os.environ`` to ``env_dump_path``."""
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "pytest"
    shim.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json, os, sys
            dump = {str(env_dump_path)!r}
            with open(dump, "w", encoding="utf-8") as fh:
                json.dump(dict(os.environ), fh)
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def setup(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bin_dir = workspace / ".bin"
    env_dump_path = workspace / "env_dump.json"
    install_wrapper_pytest(bin_dir, env_dump_path=env_dump_path)
    # Prepend the wrapper dir to PATH so the scenario's shell action finds it.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    # Pre-seed a FOUNDATION_* var so the scrub has something to strip.
    monkeypatch.setenv("FOUNDATION_APP__STATE_DIR", "/leak/state")
    return {"env_dump_path": str(env_dump_path), "bin_dir": str(bin_dir)}


def stub_responses(_workspace: Path) -> list[ProviderResponse]:
    plan = {
        "assistant_message": "Running pytest.",
        "actions": [
            {
                "id": "pytest",
                "kind": "shell",
                "summary": "Run the test suite",
                "shell": {"command": "pytest", "args": ["-q"]},
            }
        ],
    }
    summary = zero_action_response("Tests passed.")
    return [provider_response(plan), summary]


SCENARIO = Scenario(
    name="s04_run_pytest",
    prompt=PROMPT,
    setup=setup,
    stub_responses=stub_responses,
    graders=[
        assert_subprocess_env_clean(env_dump_path_key="env_dump_path"),
        assert_session_outcome(assistant_message_contains=["passed"]),
    ],
)
