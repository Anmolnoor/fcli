"""Stage 2 capability policy engine and workspace-aware shell classification."""

from __future__ import annotations

import shlex
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from foundation.models import (
    ActionKind,
    CapabilityConstraintSet,
    CapabilityHealth,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPolicyInput,
    CapabilityPolicyOutcome,
    CapabilityPolicyVerdict,
    CapabilityScopeKind,
    CapabilityScopeRule,
    CapabilitySideEffectMode,
    CapabilityState,
    CapabilityTransport,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEvaluationRecord,
    PolicyReasonCode,
    RiskClass,
    ShellAction,
    ToolCall,
    TrustTier,
)
from foundation.services.capabilities import SHELL_CAPABILITY_ID, CapabilityRegistry
from foundation.services.scope_grants import ScopeGrantStore
from foundation.settings import ApprovalMode

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
_DEFAULT_VERSION = "1.0.0"
POLICY_SNAPSHOT_VERSION = "v2-stage2"


def _utcnow() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class _RequestedInvocation:
    capability_id: str
    capability_version: str
    capability_kind: CapabilityKind
    transport: CapabilityTransport
    runtime_endpoint: str
    trust_tier: TrustTier
    risk_class: RiskClass
    capability_state: CapabilityState
    capability_health: CapabilityHealth
    constraints: CapabilityConstraintSet
    requested_cwd: str | None = None
    command_preview: str | None = None
    requested_paths: tuple[str, ...] = ()
    requested_network_hosts: tuple[str, ...] = ()
    requested_side_effects: tuple[str, ...] = ()
    requested_timeout_seconds: int | None = None
    requested_output_limit_kb: int | None = None
    invalid_summary: str | None = None
    invalid_reason_codes: tuple[PolicyReasonCode, ...] = ()
    planner_requires_approval: bool = False
    planner_approval_reason: str | None = None


class CapabilityPolicyEngine:
    """Evaluate every runnable capability invocation through one Stage 2 policy path."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        capability_registry: CapabilityRegistry | None = None,
        grant_store: ScopeGrantStore | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._capability_registry = capability_registry
        self._grant_store = grant_store
        self._invocation_counts: dict[str, int] = defaultdict(int)
        self._invocation_log: dict[str, deque[float]] = defaultdict(deque)

    def evaluate(
        self,
        action: PlannedAction,
        *,
        request_cwd: Path,
        approval_mode: ApprovalMode,
    ) -> PolicyEvaluationRecord | None:
        if action.kind in (ActionKind.EXPLANATION, ActionKind.QUESTION):
            return None

        requested = self._requested_invocation(action, request_cwd=request_cwd)
        now = time.monotonic()
        policy_input = CapabilityPolicyInput(
            action_id=action.id,
            capability_id=requested.capability_id,
            capability_version=requested.capability_version,
            capability_kind=requested.capability_kind,
            transport=requested.transport,
            runtime_endpoint=requested.runtime_endpoint,
            trust_tier=requested.trust_tier,
            risk_class=requested.risk_class,
            capability_state=requested.capability_state,
            capability_health=requested.capability_health,
            approval_mode=approval_mode.value,
            request_cwd=str(request_cwd.resolve()),
            requested_cwd=requested.requested_cwd,
            command_preview=requested.command_preview,
            planner_requires_approval=requested.planner_requires_approval,
            planner_approval_reason=requested.planner_approval_reason,
            requested_paths=list(requested.requested_paths),
            requested_network_hosts=list(requested.requested_network_hosts),
            requested_side_effects=list(requested.requested_side_effects),
            requested_timeout_seconds=requested.requested_timeout_seconds,
            requested_output_limit_kb=requested.requested_output_limit_kb,
            invocation_count=self._invocation_counts[requested.capability_id],
            prior_invocations_in_window=self._prior_invocations_in_window(
                requested.capability_id,
                requested.constraints,
                now=now,
            ),
            constraints=requested.constraints,
        )
        verdict = self._evaluate_input(policy_input, requested=requested)
        return PolicyEvaluationRecord(
            action_id=action.id,
            capability_id=policy_input.capability_id,
            capability_version=policy_input.capability_version,
            verdict=verdict,
            policy_input=policy_input,
            evaluated_at=_utcnow(),
        )

    def decide(
        self,
        action: PlannedAction,
        *,
        request_cwd: Path,
        approval_mode: ApprovalMode = ApprovalMode.PROMPT,
    ) -> PolicyDecision:
        evaluation = self.evaluate(
            action,
            request_cwd=request_cwd,
            approval_mode=approval_mode,
        )
        if evaluation is None:
            return PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.ALLOW,
                reason="Explanation-only actions do not execute anything.",
            )
        return self.to_policy_decision(evaluation)

    def to_policy_decision(self, evaluation: PolicyEvaluationRecord) -> PolicyDecision:
        outcome = evaluation.verdict.outcome
        if outcome is CapabilityPolicyOutcome.BLOCK:
            decision = PolicyDecisionType.BLOCK
        elif outcome is CapabilityPolicyOutcome.REQUIRE_APPROVAL:
            decision = PolicyDecisionType.REQUIRE_APPROVAL
        else:
            decision = PolicyDecisionType.ALLOW
        return PolicyDecision(
            action_id=evaluation.action_id,
            decision=decision,
            reason=evaluation.verdict.summary,
            risk_categories=self._risk_categories_for_evaluation(evaluation),
            command_preview=evaluation.policy_input.command_preview,
            paths=list(evaluation.policy_input.requested_paths),
            reason_codes=list(evaluation.verdict.reason_codes),
        )

    def register_invocation(self, evaluation: PolicyEvaluationRecord) -> None:
        budget = evaluation.policy_input.constraints.invocation_budget
        if budget is None:
            return
        capability_id = evaluation.capability_id
        self._invocation_counts[capability_id] += 1
        self._invocation_log[capability_id].append(time.monotonic())
        if budget.rate_limit_window_seconds is not None:
            self._trim_invocation_window(
                capability_id,
                budget.rate_limit_window_seconds,
                now=time.monotonic(),
            )

    def _requested_invocation(
        self,
        action: PlannedAction,
        *,
        request_cwd: Path,
    ) -> _RequestedInvocation:
        if self._capability_registry is None:
            return self._synthetic_invalid_invocation(
                action,
                summary="Capability policy requires an initialized capability registry.",
            )

        if action.kind is ActionKind.SHELL:
            assert action.shell is not None
            manifest = self._resolve_manifest(SHELL_CAPABILITY_ID, None)
            if manifest is None:
                return self._synthetic_invalid_invocation(
                    action,
                    summary="Shell capability is not available in the registry.",
                )
            return self._requested_shell_invocation(
                action,
                shell=action.shell,
                manifest=manifest,
                request_cwd=request_cwd,
            )

        assert action.tool_call is not None
        manifest = self._resolve_manifest(action.tool_call.capability_id, action.tool_call.version)
        if manifest is None:
            return self._synthetic_invalid_invocation(
                action,
                summary=f"Unknown capability {action.tool_call.capability_id!r}.",
                tool_call=action.tool_call,
            )
        if manifest.runtime_endpoint == "builtin.shell":
            try:
                shell = ShellAction.model_validate(action.tool_call.arguments)
            except (ValidationError, ValueError) as exc:
                return self._synthetic_invalid_invocation(
                    action,
                    summary=f"Shell capability arguments are invalid: {exc}",
                    tool_call=action.tool_call,
                )
            return self._requested_shell_invocation(
                action,
                shell=shell,
                manifest=manifest,
                request_cwd=request_cwd,
            )

        return self._requested_tool_invocation(
            action,
            tool_call=action.tool_call,
            manifest=manifest,
        )

    def _requested_tool_invocation(
        self,
        action: PlannedAction,
        *,
        tool_call: ToolCall,
        manifest: CapabilityManifest,
    ) -> _RequestedInvocation:
        arguments = dict(tool_call.arguments)
        requested_paths: list[str] = []
        requested_side_effects: list[str] = []
        invalid_summary: str | None = None

        scope = arguments.get("scope")
        endpoint = manifest.runtime_endpoint
        if endpoint in {"builtin.search", "builtin.files", "builtin.git"}:
            resolved_scope = self._resolve_tool_scope(scope)
            if resolved_scope is not None:
                requested_paths.append(str(resolved_scope))
            requested_side_effects.append("filesystem_read")
        elif endpoint in {"builtin.man", "builtin.tldr"}:
            requested_side_effects.append("local_help_read")
        elif endpoint in {
            "builtin.file.read",
            "builtin.file.read_chunk",
        }:
            path = arguments.get("path")
            normalized_path = self._normalize_workspace_tool_path(path)
            if normalized_path is not None:
                requested_paths.append(normalized_path)
            requested_side_effects.append("filesystem_read")
        elif endpoint in {
            "builtin.file.write",
            "builtin.file.edit",
            "builtin.file.apply_diff",
        }:
            path = arguments.get("path")
            normalized_path = self._normalize_workspace_tool_path(path)
            if normalized_path is not None:
                requested_paths.append(normalized_path)
            requested_side_effects.append("workspace_write")
        elif endpoint in {
            "builtin.git.status",
            "builtin.git.diff",
            "builtin.git.show",
            "builtin.git.log",
        }:
            if endpoint == "builtin.git.diff":
                requested_paths.extend(self._normalize_workspace_tool_paths(arguments.get("paths")))
            requested_side_effects.append("filesystem_read")
        elif endpoint in {"builtin.git.stage", "builtin.git.unstage"}:
            requested_paths.extend(self._normalize_workspace_tool_paths(arguments.get("paths")))
            requested_side_effects.append("workspace_write")
        elif endpoint == "builtin.git.commit":
            requested_side_effects.append("workspace_write")
        else:
            invalid_summary = f"Unsupported capability runtime endpoint: {endpoint}"

        budget = manifest.constraints.invocation_budget
        requested_output_limit_kb = None if budget is None else budget.output_limit_kb
        return _RequestedInvocation(
            capability_id=manifest.id,
            capability_version=str(manifest.version),
            capability_kind=manifest.kind,
            transport=manifest.transport,
            runtime_endpoint=manifest.runtime_endpoint,
            trust_tier=manifest.trust_tier,
            risk_class=manifest.risk_class,
            capability_state=manifest.state,
            capability_health=manifest.health,
            constraints=manifest.constraints.model_copy(deep=True),
            requested_paths=tuple(requested_paths),
            requested_side_effects=tuple(sorted(set(requested_side_effects))),
            requested_output_limit_kb=requested_output_limit_kb,
            invalid_summary=invalid_summary,
            invalid_reason_codes=(
                (PolicyReasonCode.INVALID_INVOCATION,) if invalid_summary is not None else ()
            ),
            planner_requires_approval=action.requires_approval,
            planner_approval_reason=action.approval_reason,
        )

    def _normalize_workspace_tool_path(self, raw_path: object) -> str | None:
        if not isinstance(raw_path, str):
            return None
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            return str(candidate.resolve())
        return str((self._workspace_root / candidate).resolve())

    def _normalize_workspace_tool_paths(self, raw_paths: object) -> list[str]:
        if not isinstance(raw_paths, list):
            return []
        normalized: list[str] = []
        for raw_path in raw_paths:
            resolved = self._normalize_workspace_tool_path(raw_path)
            if resolved is not None:
                normalized.append(resolved)
        return normalized

    def _requested_shell_invocation(
        self,
        action: PlannedAction,
        *,
        shell: ShellAction,
        manifest: CapabilityManifest,
        request_cwd: Path,
    ) -> _RequestedInvocation:
        command_preview = shlex.join([shell.command, *shell.args])
        if any(character.isspace() for character in shell.command):
            return _RequestedInvocation(
                capability_id=manifest.id,
                capability_version=str(manifest.version),
                capability_kind=manifest.kind,
                transport=manifest.transport,
                runtime_endpoint=manifest.runtime_endpoint,
                trust_tier=manifest.trust_tier,
                risk_class=manifest.risk_class,
                capability_state=manifest.state,
                capability_health=manifest.health,
                constraints=manifest.constraints.model_copy(deep=True),
                command_preview=command_preview,
                invalid_summary="Shell actions must split the executable and args cleanly.",
                invalid_reason_codes=(PolicyReasonCode.INVALID_INVOCATION,),
                planner_requires_approval=action.requires_approval,
                planner_approval_reason=action.approval_reason,
            )

        resolved_cwd = self._resolve_action_cwd(shell, request_cwd=request_cwd)
        if resolved_cwd is None:
            return _RequestedInvocation(
                capability_id=manifest.id,
                capability_version=str(manifest.version),
                capability_kind=manifest.kind,
                transport=manifest.transport,
                runtime_endpoint=manifest.runtime_endpoint,
                trust_tier=manifest.trust_tier,
                risk_class=manifest.risk_class,
                capability_state=manifest.state,
                capability_health=manifest.health,
                constraints=manifest.constraints.model_copy(deep=True),
                command_preview=command_preview,
                requested_cwd=str(request_cwd.resolve()),
                invalid_summary="Shell action cwd must stay within the configured workspace root.",
                invalid_reason_codes=(PolicyReasonCode.PATH_OUT_OF_SCOPE,),
                planner_requires_approval=action.requires_approval,
                planner_approval_reason=action.approval_reason,
            )

        requested_paths = self._classify_shell_paths(shell, base_cwd=resolved_cwd)
        requested_side_effects = {"subprocess"}
        if self._is_read_only_shell_command(shell):
            if shell.command in _READ_ONLY_PATH_COMMANDS | _QUERY_THEN_PATH_COMMANDS | {"git"}:
                requested_side_effects.add("filesystem_read")
        if shell.command == "git":
            requested_side_effects.update(self._classify_git_side_effects(shell.args))
        else:
            requested_side_effects.update(self._command_side_effects(shell))
        if requested_paths:
            requested_side_effects.add("filesystem_read")
        if shell.command in _UNKNOWN_RISK_COMMANDS:
            requested_side_effects.add("unknown")

        requested_network_hosts = ["*"] if "network" in requested_side_effects else []
        budget = manifest.constraints.invocation_budget
        requested_timeout_seconds = shell.timeout_seconds
        requested_output_limit_kb = None if budget is None else budget.output_limit_kb
        return _RequestedInvocation(
            capability_id=manifest.id,
            capability_version=str(manifest.version),
            capability_kind=manifest.kind,
            transport=manifest.transport,
            runtime_endpoint=manifest.runtime_endpoint,
            trust_tier=manifest.trust_tier,
            risk_class=manifest.risk_class,
            capability_state=manifest.state,
            capability_health=manifest.health,
            constraints=manifest.constraints.model_copy(deep=True),
            requested_cwd=str(resolved_cwd),
            command_preview=command_preview,
            requested_paths=tuple(requested_paths),
            requested_network_hosts=tuple(requested_network_hosts),
            requested_side_effects=tuple(sorted(requested_side_effects)),
            requested_timeout_seconds=requested_timeout_seconds,
            requested_output_limit_kb=requested_output_limit_kb,
            planner_requires_approval=action.requires_approval,
            planner_approval_reason=action.approval_reason,
        )

    def _synthetic_invalid_invocation(
        self,
        action: PlannedAction,
        *,
        summary: str,
        tool_call: ToolCall | None = None,
    ) -> _RequestedInvocation:
        capability_id = tool_call.capability_id if tool_call is not None else SHELL_CAPABILITY_ID
        capability_version = (
            tool_call.version
            if tool_call is not None and tool_call.version is not None
            else _DEFAULT_VERSION
        )
        command_preview = None
        if action.kind is ActionKind.SHELL and action.shell is not None:
            command_preview = shlex.join([action.shell.command, *action.shell.args])
        return _RequestedInvocation(
            capability_id=capability_id,
            capability_version=capability_version,
            capability_kind=CapabilityKind.TOOL,
            transport=CapabilityTransport.BUILTIN_TOOL,
            runtime_endpoint="invalid",
            trust_tier=TrustTier.EXTERNAL,
            risk_class=RiskClass.HIGH,
            capability_state=CapabilityState.DISABLED,
            capability_health=CapabilityHealth.UNHEALTHY,
            constraints=CapabilityConstraintSet(),
            command_preview=command_preview,
            invalid_summary=summary,
            invalid_reason_codes=(PolicyReasonCode.INVALID_INVOCATION,),
            planner_requires_approval=action.requires_approval,
            planner_approval_reason=action.approval_reason,
        )

    def _resolve_manifest(
        self,
        capability_id: str,
        version: str | None,
    ) -> CapabilityManifest | None:
        assert self._capability_registry is not None
        try:
            return self._capability_registry.resolve(
                capability_id,
                version,
                allow_disabled=True,
                allow_unhealthy=True,
            )
        except ValueError:
            return None

    def _evaluate_input(
        self,
        policy_input: CapabilityPolicyInput,
        *,
        requested: _RequestedInvocation,
    ) -> CapabilityPolicyVerdict:
        if requested.invalid_summary is not None:
            return CapabilityPolicyVerdict(
                outcome=CapabilityPolicyOutcome.BLOCK,
                summary=requested.invalid_summary,
                reason_codes=list(requested.invalid_reason_codes),
                constraints=policy_input.constraints.model_copy(deep=True),
            )

        if policy_input.capability_state is not CapabilityState.ENABLED:
            return self._blocked_verdict(
                "Capability is disabled.",
                PolicyReasonCode.CAPABILITY_DISABLED,
                policy_input,
            )
        if policy_input.capability_health is not CapabilityHealth.HEALTHY:
            return self._blocked_verdict(
                "Capability is unhealthy and cannot execute.",
                PolicyReasonCode.CAPABILITY_UNHEALTHY,
                policy_input,
            )
        if not self._paths_in_scope(policy_input):
            if self._can_escalate_scope(policy_input):
                return CapabilityPolicyVerdict(
                    outcome=CapabilityPolicyOutcome.REQUIRE_APPROVAL,
                    summary=(
                        "Requested path is outside the workspace; reading it needs your approval."
                    ),
                    reason_codes=[PolicyReasonCode.SCOPE_ESCALATION],
                    constraints=policy_input.constraints.model_copy(deep=True),
                )
            return self._blocked_verdict(
                "Requested paths are outside the capability's declared path scope.",
                PolicyReasonCode.PATH_OUT_OF_SCOPE,
                policy_input,
            )
        if not self._network_in_scope(policy_input):
            return self._blocked_verdict(
                "Requested network access is outside the capability's declared scope.",
                PolicyReasonCode.NETWORK_OUT_OF_SCOPE,
                policy_input,
            )
        undeclared = self._undeclared_side_effects(policy_input)
        if undeclared:
            return self._blocked_verdict(
                "Requested side effects are not declared by the capability metadata.",
                PolicyReasonCode.UNDECLARED_SIDE_EFFECT,
                policy_input,
            )
        budget = policy_input.constraints.invocation_budget
        if budget is not None and budget.max_invocations is not None:
            if policy_input.invocation_count >= budget.max_invocations:
                return self._blocked_verdict(
                    "Capability invocation budget has been exhausted for this session.",
                    PolicyReasonCode.INVOCATION_LIMIT_EXCEEDED,
                    policy_input,
                )
        if budget is not None and budget.rate_limit_count is not None:
            if policy_input.prior_invocations_in_window >= budget.rate_limit_count:
                return self._blocked_verdict(
                    "Capability rate limit has been exceeded for this session.",
                    PolicyReasonCode.RATE_LIMIT_EXCEEDED,
                    policy_input,
                )

        reason_codes: list[PolicyReasonCode] = []
        if policy_input.planner_requires_approval:
            reason_codes.append(PolicyReasonCode.MODEL_MARKED_APPROVAL)
        if policy_input.trust_tier is not TrustTier.FOUNDATION:
            reason_codes.append(PolicyReasonCode.UNTRUSTED_CAPABILITY)
        if policy_input.risk_class is RiskClass.HIGH:
            reason_codes.append(PolicyReasonCode.HIGH_RISK_CAPABILITY)
        if self._requires_side_effect_approval(policy_input):
            reason_codes.append(PolicyReasonCode.SIDE_EFFECT_REQUIRES_APPROVAL)

        if reason_codes:
            return CapabilityPolicyVerdict(
                outcome=CapabilityPolicyOutcome.REQUIRE_APPROVAL,
                summary=self._approval_summary(policy_input, reason_codes),
                reason_codes=reason_codes,
                constraints=self._effective_constraints(policy_input),
            )

        effective_constraints = self._effective_constraints(policy_input)
        outcome = (
            CapabilityPolicyOutcome.ALLOW_WITH_CONSTRAINTS
            if self._has_constraints(effective_constraints)
            else CapabilityPolicyOutcome.ALLOW
        )
        summary = (
            "Capability is allowed within its declared scope and executor budget."
            if outcome is CapabilityPolicyOutcome.ALLOW_WITH_CONSTRAINTS
            else "Capability is allowed."
        )
        return CapabilityPolicyVerdict(
            outcome=outcome,
            summary=summary,
            constraints=effective_constraints,
        )

    def _can_escalate_scope(self, policy_input: CapabilityPolicyInput) -> bool:
        """Whether an out-of-scope path may be escalated to the user.

        Only read-only typed file reads are eligible: a single grant can be
        honored by the file service, and reads are the lowest-risk escalation.
        Writes, shell, and discovery stay hard-blocked.
        """
        if self._grant_store is None:
            return False
        if policy_input.runtime_endpoint not in {
            "builtin.file.read",
            "builtin.file.read_chunk",
        }:
            return False
        effects = set(policy_input.requested_side_effects)
        return effects == {"filesystem_read"}

    def _blocked_verdict(
        self,
        summary: str,
        reason_code: PolicyReasonCode,
        policy_input: CapabilityPolicyInput,
    ) -> CapabilityPolicyVerdict:
        return CapabilityPolicyVerdict(
            outcome=CapabilityPolicyOutcome.BLOCK,
            summary=summary,
            reason_codes=[reason_code],
            constraints=policy_input.constraints.model_copy(deep=True),
        )

    def _effective_constraints(
        self,
        policy_input: CapabilityPolicyInput,
    ) -> CapabilityConstraintSet:
        constraints = policy_input.constraints.model_copy(deep=True)
        budget = constraints.invocation_budget
        if budget is None:
            return constraints
        if policy_input.requested_timeout_seconds is not None:
            if budget.timeout_seconds is None:
                budget.timeout_seconds = policy_input.requested_timeout_seconds
            else:
                budget.timeout_seconds = min(
                    budget.timeout_seconds,
                    policy_input.requested_timeout_seconds,
                )
        if policy_input.requested_output_limit_kb is not None:
            if budget.output_limit_kb is None:
                budget.output_limit_kb = policy_input.requested_output_limit_kb
            else:
                budget.output_limit_kb = min(
                    budget.output_limit_kb,
                    policy_input.requested_output_limit_kb,
                )
        return constraints

    def _has_constraints(self, constraints: CapabilityConstraintSet | None) -> bool:
        if constraints is None:
            return False
        return bool(
            constraints.path_rules
            or constraints.network_rules
            or constraints.side_effect_rules
            or constraints.invocation_budget is not None
        )

    def _paths_in_scope(self, policy_input: CapabilityPolicyInput) -> bool:
        rules = policy_input.constraints.path_rules
        if policy_input.requested_cwd is not None and not self._path_matches_rules(
            Path(policy_input.requested_cwd),
            rules,
            request_cwd=Path(policy_input.request_cwd),
        ):
            return False
        for raw_path in policy_input.requested_paths:
            if not self._path_matches_rules(
                Path(raw_path),
                rules,
                request_cwd=Path(policy_input.request_cwd),
            ):
                return False
        return True

    def _path_matches_rules(
        self,
        path: Path,
        rules: list[CapabilityScopeRule],
        *,
        request_cwd: Path,
    ) -> bool:
        if not rules:
            return False
        resolved_path = path.resolve()
        # A session-granted out-of-scope read root counts as in-scope, so a
        # second read under an already-approved root does not re-prompt.
        if self._grant_store is not None and self._grant_store.is_granted(resolved_path):
            return True
        for rule in rules:
            if rule.kind is CapabilityScopeKind.ANY:
                return True
            if rule.kind is CapabilityScopeKind.NONE:
                continue
            if rule.kind is CapabilityScopeKind.WORKSPACE and self._is_within(
                resolved_path,
                self._workspace_root,
            ):
                return True
            if rule.kind is CapabilityScopeKind.REQUEST_CWD and self._is_within(
                resolved_path,
                request_cwd.resolve(),
            ):
                return True
            if rule.value is None:
                continue
            scope_path = self._resolve_scope_value(rule.value)
            if rule.kind is CapabilityScopeKind.PREFIX and self._is_within(
                resolved_path,
                scope_path,
            ):
                return True
            if rule.kind is CapabilityScopeKind.EXACT and resolved_path == scope_path:
                return True
        return False

    def _network_in_scope(self, policy_input: CapabilityPolicyInput) -> bool:
        rules = policy_input.constraints.network_rules
        if not policy_input.requested_network_hosts:
            return True
        if not rules:
            return False
        for host in policy_input.requested_network_hosts:
            if not any(self._network_rule_matches(host, rule) for rule in rules):
                return False
        return True

    def _network_rule_matches(self, host: str, rule: CapabilityScopeRule) -> bool:
        if rule.kind is CapabilityScopeKind.ANY:
            return True
        if rule.kind is CapabilityScopeKind.NONE:
            return False
        if rule.value is None:
            return False
        if rule.kind is CapabilityScopeKind.EXACT:
            return host == rule.value
        if rule.kind is CapabilityScopeKind.PREFIX:
            return host.startswith(rule.value)
        return False

    def _undeclared_side_effects(self, policy_input: CapabilityPolicyInput) -> list[str]:
        declared = {
            rule.side_effect: rule.mode for rule in policy_input.constraints.side_effect_rules
        }
        return [
            side_effect
            for side_effect in policy_input.requested_side_effects
            if side_effect not in declared
        ]

    def _requires_side_effect_approval(self, policy_input: CapabilityPolicyInput) -> bool:
        declared = {
            rule.side_effect: rule.mode for rule in policy_input.constraints.side_effect_rules
        }
        return any(
            declared.get(side_effect) is CapabilitySideEffectMode.REQUIRE_APPROVAL
            for side_effect in policy_input.requested_side_effects
        )

    def _approval_summary(
        self,
        policy_input: CapabilityPolicyInput,
        reason_codes: list[PolicyReasonCode],
    ) -> str:
        if PolicyReasonCode.MODEL_MARKED_APPROVAL in reason_codes:
            return (
                policy_input.planner_approval_reason
                or "The model marked this capability invocation as approval-required."
            )
        if PolicyReasonCode.UNTRUSTED_CAPABILITY in reason_codes:
            return "Untrusted capabilities require approval before execution."
        if PolicyReasonCode.HIGH_RISK_CAPABILITY in reason_codes:
            return "High-risk capabilities require approval before execution."
        if PolicyReasonCode.SIDE_EFFECT_REQUIRES_APPROVAL in reason_codes:
            side_effects = ", ".join(policy_input.requested_side_effects)
            return f"Requested side effects require approval: {side_effects}."
        return "Capability execution requires approval."

    def _prior_invocations_in_window(
        self,
        capability_id: str,
        constraints: CapabilityConstraintSet,
        *,
        now: float,
    ) -> int:
        budget = constraints.invocation_budget
        if budget is None or budget.rate_limit_window_seconds is None:
            return 0
        self._trim_invocation_window(
            capability_id,
            budget.rate_limit_window_seconds,
            now=now,
        )
        return len(self._invocation_log[capability_id])

    def _trim_invocation_window(
        self,
        capability_id: str,
        window_seconds: int,
        *,
        now: float,
    ) -> None:
        threshold = now - window_seconds
        log = self._invocation_log[capability_id]
        while log and log[0] < threshold:
            log.popleft()

    def _resolve_tool_scope(self, scope: object) -> Path | None:
        if scope is None:
            return self._workspace_root
        candidate = Path(str(scope)).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._workspace_root / candidate).resolve()

    def _resolve_action_cwd(self, shell: ShellAction, *, request_cwd: Path) -> Path | None:
        if shell.cwd is None:
            resolved = request_cwd.resolve()
        else:
            candidate = Path(shell.cwd)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self._workspace_root / candidate).resolve()
            )
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            return None
        return resolved

    def _classify_shell_paths(self, shell: ShellAction, *, base_cwd: Path) -> list[str]:
        paths: list[str] = []
        for raw_path in self._candidate_paths(shell):
            resolved = self._resolve_candidate_path(raw_path, base_cwd=base_cwd)
            if resolved is not None:
                paths.append(str(resolved))
        return paths

    def _candidate_paths(self, shell: ShellAction) -> list[str]:
        args = shell.args
        path_commands = _READ_ONLY_PATH_COMMANDS | _WORKSPACE_WRITE_COMMANDS | _DESTRUCTIVE_COMMANDS
        if shell.command in path_commands:
            return [argument for argument in args if argument and not argument.startswith("-")]
        if shell.command in _PERMISSION_COMMANDS:
            return [argument for argument in args[1:] if argument and not argument.startswith("-")]
        if shell.command in _QUERY_THEN_PATH_COMMANDS:
            positional = [
                argument for argument in args if argument and not argument.startswith("-")
            ]
            return positional[1:]
        if shell.command in _UNKNOWN_RISK_COMMANDS:
            if not args or args[0] in {"-c", "-m"} or args[0].startswith("-"):
                return []
            return [args[0]]
        if shell.command == "git" and "--" in args:
            marker = args.index("--")
            return [argument for argument in args[marker + 1 :] if argument]
        return []

    def _resolve_candidate_path(self, raw_path: str, *, base_cwd: Path) -> Path | None:
        if not raw_path or raw_path == "-":
            return None
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate.resolve()
        return (base_cwd / candidate).resolve()

    def _is_read_only_shell_command(self, shell: ShellAction) -> bool:
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
            return True
        if command in _QUERY_THEN_PATH_COMMANDS:
            return True
        if command == "which":
            return bool(shell.args)
        if command == "git":
            return bool(shell.args) and shell.args[0] in _READONLY_GIT_SUBCOMMANDS
        return False

    def _classify_git_side_effects(self, args: list[str]) -> set[str]:
        if not args:
            return {"unknown"}
        if args[0] in _UNSAFE_GIT_OPTIONS or any(
            option in _UNSAFE_GIT_OPTIONS for option in args[1:]
        ):
            return {"workspace_write", "unknown"}

        subcommand = args[0]
        if subcommand in _READONLY_GIT_SUBCOMMANDS:
            return {"filesystem_read"}
        if subcommand in _NETWORK_GIT_SUBCOMMANDS:
            return {"network"}
        if subcommand in _WRITE_GIT_SUBCOMMANDS:
            effects = {"workspace_write"}
            if subcommand in {"clean", "reset", "restore", "rm"}:
                effects.add("destructive")
            return effects
        return {"unknown"}

    def _command_side_effects(self, shell: ShellAction) -> set[str]:
        effects: set[str] = set()
        if shell.command in _WORKSPACE_WRITE_COMMANDS:
            effects.add("workspace_write")
        if shell.command in _DESTRUCTIVE_COMMANDS:
            effects.update({"workspace_write", "destructive"})
        if shell.command in _NETWORK_COMMANDS:
            effects.add("network")
        if shell.command in _PERMISSION_COMMANDS:
            effects.update({"workspace_write", "permission"})
        if shell.command in _ENVIRONMENT_COMMANDS:
            effects.add("environment")
        if any(flag in shell.args for flag in _RECURSIVE_FLAGS):
            effects.add("recursive")
        return effects

    def _risk_categories_for_evaluation(
        self,
        evaluation: PolicyEvaluationRecord,
    ) -> list[str]:
        categories: set[str] = set(evaluation.policy_input.requested_side_effects)
        categories.update(code.value for code in evaluation.verdict.reason_codes)
        if evaluation.policy_input.trust_tier is not TrustTier.FOUNDATION:
            categories.add("untrusted_capability")
        if evaluation.policy_input.risk_class is RiskClass.HIGH:
            categories.add("high_risk")
        elif evaluation.policy_input.risk_class is RiskClass.MEDIUM:
            categories.add("moderate_risk")
        if PolicyReasonCode.PATH_OUT_OF_SCOPE in evaluation.verdict.reason_codes:
            categories.add("outside_workspace")
        return sorted(categories)

    def _resolve_scope_value(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._workspace_root / candidate).resolve()

    def _is_within(self, candidate: Path, root: Path) -> bool:
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return False
        return True


GuardrailPolicyEngine = CapabilityPolicyEngine
SimplePolicyEngine = CapabilityPolicyEngine
