# Stage 2: Capability Policies and Governance

**Status: shipped (v2 complete; see git history).**

## Goal
Move policy enforcement from shell-specific guardrails to a capability-wide governance layer. This stage ensures that every tool, skill, and shell-backed capability is evaluated through the same policy engine before execution, with explicit approval and audit behavior based on capability metadata and invocation context.

## Entry Criteria
- Stage 1 exit criteria are met.
- Every runnable action is represented as a capability with validated metadata.
- The planner consumes registry snapshots rather than a fixed tool list.

## Locked Decisions
- Policy applies to every capability invocation, not just shell commands.
- Default posture is allow trusted capabilities and gate risky ones.
- The executor must enforce policy; planner intent alone never authorizes execution.
- Capability metadata is the primary policy input, supplemented by run context and user approval mode.
- Policy decisions must be machine-readable, persisted, and explainable after the fact.

## Public Interfaces Introduced
- `CapabilityPolicyInput`
- `CapabilityPolicyVerdict`
- `CapabilityConstraintSet`
- `PolicyReasonCode`
- `CapabilityApprovalRequest`
- `CapabilityApprovalResolution`
- `CapabilityInvocationBudget`
- `CapabilityScopeRule`
- `CapabilitySideEffectRule`
- `CapabilityPolicyEngine`
- `PolicyEvaluationRecord`

## Step-by-Step Plan
1. Define the policy input model from Stage 1 registry data plus invocation context:
   - capability id, version, kind, trust tier, and risk class
   - declared path, network, and side-effect scopes
   - user approval mode and session context
   - invocation count, timeout, and output budget
   - upstream dependency or prior-step context where relevant
2. Define the policy verdict model:
   - allow
   - allow with constraints
   - require approval
   - block
   - machine-readable reason codes and human-readable explanations
3. Define the initial governance rules:
   - trusted low-risk capabilities may run by default
   - medium and high-risk capabilities require approval or executor constraints
   - out-of-scope path or network access is blocked
   - undeclared side effects are blocked
   - unhealthy, disabled, or untrusted capabilities are never executed as if they were healthy and trusted
4. Implement executor-side enforcement for capability-level constraints:
   - path scope
   - network scope
   - timeout and output limits
   - invocation count and rate limits
   - side-effect class validation
5. Replace shell-only approval prompts with capability-aware approval flows that reference the capability id, declared risk, constrained scopes, and requested side effects.
6. Persist policy evaluation and approval outcomes as first-class records attached to capability invocations so later trace and audit features can explain not just what ran, but why it was allowed.
7. Add policy and selection evals before increasing autonomy:
   - policy correctness corpus for allow, gate, and block cases
   - capability selection quality checks against curated tasks
   - regression fixtures for risky capabilities and trust-tier transitions

## Deliverables
- A capability policy engine with typed verdicts
- Executor-side enforcement of capability constraints
- Capability-aware approval flows
- Persisted policy evaluation records
- Eval suites for policy correctness and capability selection quality

## Exit Criteria
- Every capability invocation receives a policy verdict before execution.
- Risky capabilities cannot execute without the configured approval path or policy allowance.
- Executor enforcement prevents scope and side-effect violations even if planner output is incorrect.
- Policy and approval records are persisted with enough detail for later audit.
- Selection and policy evals exist and run reliably enough to guard future autonomy work.

## Test Focus
- Allow, constrain, require-approval, and block verdict paths
- Constraint enforcement for path, network, timeout, and output budgets
- Approval-required, approval-denied, and approval-granted capability flows
- Disabled, unhealthy, or low-trust capability handling
- Policy correctness and capability selection eval coverage

## Handoff to Stage 3
Do not add replay or deeper autonomy until every capability call is governed through one policy path and emits stable policy records. Stage 3 depends on these records to build a full causal trace.
