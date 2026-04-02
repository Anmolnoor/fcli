"""Stage 6 policy classification and workspace guardrails."""

from __future__ import annotations

import shlex
from pathlib import Path

from foundation.models import (
    ActionKind,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    ShellAction,
)
from foundation.services.tools import WorkspacePathFilter

_SIMPLE_NO_ARG_COMMANDS = {"date", "pwd", "uname", "whoami"}
_READ_ONLY_PATH_COMMANDS = {"cat", "head", "ls", "stat", "tail", "wc"}
_QUERY_THEN_PATH_COMMANDS = {"fd", "rg"}
_ENVIRONMENT_COMMANDS = {"env", "printenv"}
_WORKSPACE_WRITE_COMMANDS = {"cp", "mkdir", "mv", "sed", "tee", "touch"}
_DESTRUCTIVE_COMMANDS = {"rm", "rmdir"}
_NETWORK_COMMANDS = {"brew", "curl", "git-lfs", "npm", "pip", "scp", "ssh", "uv", "wget"}
_PERMISSION_COMMANDS = {"chmod", "chown"}
_UNKNOWN_RISK_COMMANDS = {"python", "python3"}
_READONLY_GIT_SUBCOMMANDS = {"branch", "diff", "log", "rev-parse", "show", "status"}
_WRITE_GIT_SUBCOMMANDS = {
    "add",
    "apply",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
}
_NETWORK_GIT_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "submodule"}
_UNSAFE_GIT_OPTIONS = {
    "-C",
    "-c",
    "--config-env",
    "--git-dir",
    "--namespace",
    "--no-index",
    "--work-tree",
}
_RECURSIVE_FLAGS = {"-R", "-r", "--recursive"}


class GuardrailPolicyEngine:
    """Classify planned actions and decide whether they need approval."""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._path_filter = WorkspacePathFilter(self._workspace_root)

    def decide(self, action: PlannedAction, *, request_cwd: Path) -> PolicyDecision:
        if action.kind is ActionKind.EXPLANATION:
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.ALLOW,
                reason="Explanation-only actions do not execute anything.",
            )

        if action.kind is ActionKind.TOOL_CALL:
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.ALLOW,
                reason="Typed local tool calls stay within the local tool layer.",
            )

        assert action.shell is not None
        shell = action.shell
        command_preview = shlex.join([shell.command, *shell.args])
        if any(character.isspace() for character in shell.command):
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.BLOCK,
                reason="Shell actions must split the executable and args cleanly.",
                risk_categories=["unknown"],
                command_preview=command_preview,
            )

        effective_cwd = self._resolve_action_cwd(shell, request_cwd=request_cwd)
        if effective_cwd is None:
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.BLOCK,
                reason="Shell action cwd must stay within the configured workspace root.",
                risk_categories=["outside_workspace"],
                command_preview=command_preview,
            )

        risk_categories: list[str] = []
        path_decision = self._classify_paths(shell, base_cwd=effective_cwd)
        risk_categories.extend(path_decision["risk_categories"])

        if action.requires_approval:
            if not risk_categories:
                risk_categories.append("model_marked")
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason=(
                    action.approval_reason
                    or "The model marked this action as approval-required."
                ),
                risk_categories=sorted(set(risk_categories)),
                command_preview=command_preview,
                paths=path_decision["paths"],
            )

        if shell.command == "git":
            git_decision = self._classify_git(shell.args)
            risk_categories.extend(git_decision["risk_categories"])
            if git_decision["decision"] is PolicyDecisionType.ALLOW and not risk_categories:
                return PolicyDecision(
                    action_id=action.id,
                    decision=PolicyDecisionType.ALLOW,
                    reason="The git subcommand is read-only within the workspace boundary.",
                    command_preview=command_preview,
                    paths=path_decision["paths"],
                )
            return PolicyDecision(
                action_id=action.id,
                decision=git_decision["decision"],
                reason=git_decision["reason"],
                risk_categories=sorted(set(risk_categories)),
                command_preview=command_preview,
                paths=path_decision["paths"],
            )

        if self._is_safe_read_only_command(
            shell,
            base_cwd=effective_cwd,
            paths=path_decision["paths"],
        ):
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.ALLOW,
                reason="The shell command is a read-only workspace inspection command.",
                command_preview=command_preview,
                paths=path_decision["paths"],
            )

        risk_categories.extend(self._command_risks(shell))
        if not risk_categories:
            risk_categories.append("unknown")

        return PolicyDecision(
            action_id=action.id,
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=self._approval_reason_for_categories(risk_categories),
            risk_categories=sorted(set(risk_categories)),
            command_preview=command_preview,
            paths=path_decision["paths"],
        )

    def _resolve_action_cwd(self, shell: ShellAction, *, request_cwd: Path) -> Path | None:
        if shell.cwd is None:
            resolved = request_cwd.resolve()
        else:
            candidate = Path(shell.cwd)
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                resolved = (self._workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            return None
        return resolved

    def _classify_paths(self, shell: ShellAction, *, base_cwd: Path) -> dict[str, list[str]]:
        risk_categories: list[str] = []
        paths: list[str] = []
        for raw_path in self._candidate_paths(shell):
            resolved = self._resolve_candidate_path(raw_path, base_cwd=base_cwd)
            if resolved is None:
                continue
            paths.append(str(resolved))
            if self._is_within_workspace(resolved):
                relative = resolved.relative_to(self._workspace_root)
                if relative.parts and self._path_filter.is_ignored(relative):
                    risk_categories.append("ignored_path")
            else:
                risk_categories.append("outside_workspace")
        return {"paths": paths, "risk_categories": risk_categories}

    def _candidate_paths(self, shell: ShellAction) -> list[str]:
        args = shell.args
        if shell.command in (
            _READ_ONLY_PATH_COMMANDS | _WORKSPACE_WRITE_COMMANDS | _DESTRUCTIVE_COMMANDS
        ):
            return [argument for argument in args if argument and not argument.startswith("-")]
        if shell.command in _PERMISSION_COMMANDS:
            return [argument for argument in args[1:] if argument and not argument.startswith("-")]
        if shell.command in _QUERY_THEN_PATH_COMMANDS:
            positional = [
                argument for argument in args if argument and not argument.startswith("-")
            ]
            return positional[1:]
        if shell.command in _UNKNOWN_RISK_COMMANDS:
            if not args:
                return []
            if args[0] in {"-c", "-m"}:
                return []
            if args[0].startswith("-"):
                return []
            return [args[0]]
        if shell.command == "git":
            if "--" in args:
                marker = args.index("--")
                return [argument for argument in args[marker + 1 :] if argument]
            return []
        return []

    def _resolve_candidate_path(self, raw_path: str, *, base_cwd: Path) -> Path | None:
        candidate = Path(raw_path)
        if not raw_path or raw_path == "-":
            return None
        if candidate.is_absolute():
            return candidate.resolve()
        return (base_cwd / candidate).resolve()

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self._workspace_root)
        except ValueError:
            return False
        return True

    def _is_safe_read_only_command(
        self,
        shell: ShellAction,
        *,
        base_cwd: Path,
        paths: list[str],
    ) -> bool:
        command = shell.command
        if "/" in command:
            return False
        if command in _UNKNOWN_RISK_COMMANDS | _ENVIRONMENT_COMMANDS:
            return False
        if any(flag in shell.args for flag in _RECURSIVE_FLAGS):
            return False
        if command in _SIMPLE_NO_ARG_COMMANDS:
            return not shell.args
        if command in _READ_ONLY_PATH_COMMANDS:
            return bool(paths) and all(
                self._is_within_workspace(Path(path)) for path in paths
            )
        if command in _QUERY_THEN_PATH_COMMANDS:
            positional = [
                argument
                for argument in shell.args
                if argument and not argument.startswith("-")
            ]
            return bool(positional) and all(
                self._is_within_workspace(Path(path)) for path in paths
            )
        if command == "which":
            return bool(shell.args) and all(
                argument
                and not argument.startswith("-")
                and "/" not in argument
                and not argument.startswith(("~", "."))
                for argument in shell.args
            )
        return False

    def _classify_git(self, args: list[str]) -> dict[str, object]:
        if not args:
            return {
                "decision": PolicyDecisionType.REQUIRE_APPROVAL,
                "reason": "Git commands without an explicit subcommand require approval.",
                "risk_categories": ["unknown"],
            }
        if args[0] in _UNSAFE_GIT_OPTIONS or any(
            option in _UNSAFE_GIT_OPTIONS for option in args[1:]
        ):
            return {
                "decision": PolicyDecisionType.REQUIRE_APPROVAL,
                "reason": "Git commands that override repository boundaries require approval.",
                "risk_categories": ["outside_workspace"],
            }

        subcommand = args[0]
        if subcommand in _READONLY_GIT_SUBCOMMANDS:
            return {
                "decision": PolicyDecisionType.ALLOW,
                "reason": "The git subcommand is read-only.",
                "risk_categories": [],
            }
        if subcommand in _NETWORK_GIT_SUBCOMMANDS:
            return {
                "decision": PolicyDecisionType.REQUIRE_APPROVAL,
                "reason": "Networked git operations require approval.",
                "risk_categories": ["network"],
            }
        if subcommand in _WRITE_GIT_SUBCOMMANDS:
            categories = ["workspace_write"]
            if subcommand in {"clean", "reset", "restore", "rm"}:
                categories.append("destructive")
            return {
                "decision": PolicyDecisionType.REQUIRE_APPROVAL,
                "reason": "Mutating git operations require approval.",
                "risk_categories": categories,
            }
        return {
            "decision": PolicyDecisionType.REQUIRE_APPROVAL,
            "reason": "Unsupported git operations require approval before execution.",
            "risk_categories": ["unknown"],
        }

    def _command_risks(self, shell: ShellAction) -> list[str]:
        risks: list[str] = []
        if shell.command in _WORKSPACE_WRITE_COMMANDS:
            risks.append("workspace_write")
        if shell.command in _DESTRUCTIVE_COMMANDS:
            risks.extend(["workspace_write", "destructive"])
        if shell.command in _NETWORK_COMMANDS:
            risks.append("network")
        if shell.command in _PERMISSION_COMMANDS:
            risks.extend(["workspace_write", "permission"])
        if shell.command in _ENVIRONMENT_COMMANDS:
            risks.append("environment")
        if shell.command in _UNKNOWN_RISK_COMMANDS:
            risks.append("unknown")
        if any(flag in shell.args for flag in _RECURSIVE_FLAGS):
            risks.append("recursive")
        return risks

    def _approval_reason_for_categories(self, categories: list[str]) -> str:
        category_set = set(categories)
        if "destructive" in category_set:
            return "Destructive filesystem operations require approval."
        if "network" in category_set:
            return "Networked installs or downloads require approval."
        if "permission" in category_set:
            return "Permission-changing commands require approval."
        if "outside_workspace" in category_set:
            return "Actions that cross the workspace boundary require approval."
        if "environment" in category_set:
            return "Environment dumps require approval."
        if "workspace_write" in category_set:
            return "Workspace modifications require approval."
        if "recursive" in category_set:
            return "Recursive operations require approval."
        return "This shell action requires approval before execution."


SimplePolicyEngine = GuardrailPolicyEngine
