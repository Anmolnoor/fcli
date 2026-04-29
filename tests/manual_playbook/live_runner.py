"""Run one playbook scenario against the configured real provider."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from tests.manual_playbook.harness import (
    PlaybookRun,
    build_playbook_orchestrator,
    run_playbook_scenario,
)
from tests.manual_playbook.scenarios import SCENARIOS
from tests.manual_playbook.scenarios._base import Scenario

from foundation.services import ProviderError, build_provider_adapter
from foundation.settings import SettingsLoadError, load_settings

_LIVE_MODE_ENV_VAR = "FOUNDATION_PLAYBOOK_LIVE"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one manual-playbook scenario against the configured live provider.",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=sorted(scenario.name for scenario in SCENARIOS),
        help="Scenario name to execute.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Optional config.toml path. Defaults to Foundation's standard config resolution.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Optional empty workspace directory to run the scenario in.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the generated temporary workspace instead of deleting it on exit.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _scenario_by_name(name: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise KeyError(name)


@contextmanager
def _workspace_context(
    requested_workspace: Path | None,
    *,
    keep_workspace: bool,
) -> Iterator[Path]:
    if requested_workspace is not None:
        workspace = requested_workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise RuntimeError(f"Workspace must be empty: {workspace}")
        yield workspace
        return

    if keep_workspace:
        workspace = Path(tempfile.mkdtemp(prefix="foundation-playbook-")).resolve()
        yield workspace
        return

    with tempfile.TemporaryDirectory(prefix="foundation-playbook-") as tmpdir:
        yield Path(tmpdir).resolve()


def _render_report(
    *,
    scenario: Scenario,
    workspace: Path,
    run: PlaybookRun,
    provider_name: str,
    provider_model: str,
) -> str:
    result = run.context.result
    status = "PASS" if not run.hard_failures else "FAIL"
    lines = [
        f"# Live Playbook Result: {status}",
        "",
        f"- Scenario: `{scenario.name}`",
        f"- Provider: `{provider_name}` / `{provider_model}`",
        f"- Workspace: `{workspace}`",
        f"- Stop reason: `{result.stop_reason.value}`",
        f"- Session status: `{run.context.session_status.value}`",
        f"- Pending approval actions: `{result.summary.pending_approval_actions}`",
    ]
    if result.governance_notice is not None:
        lines.append(
            "- Governance notice: "
            f"`{result.governance_notice.code.value}`"
            f" ({result.governance_notice.message})"
        )

    lines.extend([
        "",
        "## Graders",
    ])
    lines.extend(f"- {outcome.render()}" for outcome in run.outcomes)
    lines.extend([
        "",
        "## Assistant Message",
        result.assistant_message.content,
    ])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.environ.get(_LIVE_MODE_ENV_VAR) != "1":
        print(
            f"Set {_LIVE_MODE_ENV_VAR}=1 to enable live-provider playbook runs.",
            file=sys.stderr,
        )
        return 2

    try:
        scenario = _scenario_by_name(args.scenario)
        settings = load_settings(config_path=args.config)
        provider = build_provider_adapter(settings)
        with _workspace_context(args.workspace, keep_workspace=args.keep_workspace) as workspace:
            history_db_path = workspace / ".foundation" / "history.sqlite3"
            history_db_path.parent.mkdir(parents=True, exist_ok=True)
            orchestrator = build_playbook_orchestrator(
                workspace=workspace,
                provider=provider,
                approval_mode=scenario.approval_mode,
                history_database_path=history_db_path,
                shell_timeout_seconds=settings.shell.default_timeout_seconds,
                shell_max_timeout_seconds=settings.shell.max_timeout_seconds,
                shell_capture_limit_kb=settings.shell.capture_limit_kb,
                shell_allow_pty=settings.shell.allow_pty,
                shell_enforce_workspace_boundary=settings.shell.enforce_workspace_boundary,
                pass_through_foundation_env=settings.shell.pass_through_foundation_env,
            )
            run = run_playbook_scenario(
                scenario,
                workspace=workspace,
                orchestrator=orchestrator,
            )
            print(
                _render_report(
                    scenario=scenario,
                    workspace=workspace,
                    run=run,
                    provider_name=settings.provider.name,
                    provider_model=settings.provider.model,
                )
            )
    except (ProviderError, SettingsLoadError, RuntimeError) as exc:
        print(f"live playbook run failed: {exc}", file=sys.stderr)
        return 2

    return 1 if run.hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
