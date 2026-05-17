# Stage 3: Trace, Audit, and Runtime Split

## Goal
Split the runtime into explicit subsystems and persist a full causal trace for each request so users can inspect why a capability was chosen, why policy allowed or blocked it, and what side effects followed. This stage prioritizes audit-first traceability while storing enough detail to support future replay-oriented features without redesigning persistence later.

## Entry Criteria
- Stage 2 exit criteria are met.
- Capability execution is fully registry-driven and policy-enforced.
- Capability policy and approval records are already persisted.

## Locked Decisions
- The runtime is split into planner, capability registry, policy engine, executor, memory, and observer modules.
- Traceability is audit-first in v2; user-facing rerun and branching replay are deferred.
- Replay-grade data is still recorded now so later rerun support does not require a storage redesign.
- Local trace persistence continues to use SQLite, but with a new v2 schema oriented around steps and causal links rather than the v0.1 session summary model.
- Observer owns event emission, redaction, and trace persistence responsibilities.

## Public Interfaces Introduced
- `PlanningStep`
- `ExecutionStep`
- `TraceRecord`
- `TraceEdge`
- `TraceArtifactRef`
- `SelectionReason`
- `StepSideEffect`
- `TraceStore`
- `TraceQuery`
- `TraceSummary`
- `AuditReport`
- `ObserverService`

## Step-by-Step Plan
1. Split the runtime into explicit modules with narrow responsibilities:
   - planner chooses candidate capabilities and emits selection reasons
   - registry resolves available capabilities and metadata
   - policy engine evaluates capability invocations
   - executor performs constrained execution
   - memory persists summaries and trace records
   - observer emits redacted events and audit artifacts
2. Define the v2 trace schema for each step:
   - request and session identifiers
   - candidate capabilities considered
   - selected capability and selection reason
   - policy verdict and policy reason codes
   - approval request and resolution
   - input, output, and side-effect artifact references
   - timestamps, duration, and dependency edges to prior steps
3. Replace summary-only history persistence with a trace store that can reconstruct the causal chain of a run at step granularity while still supporting compact session summaries for normal CLI output.
4. Add observer-driven event emission and redaction rules so logs and stored traces share stable event names and consistent sensitive-data handling.
5. Add audit surfaces for trace inspection:
   - list traces or sessions
   - inspect one trace summary
   - inspect one step and its causal predecessors
   - explain why a capability was selected and why policy allowed or denied it
6. Persist replay-grade references even though replay is deferred:
   - capability version and manifest fingerprint
   - policy snapshot or policy ruleset version
   - input and output artifact references
   - side-effect references and environment metadata needed for later forensic comparison
7. Add trace completeness evals so every executed step is checked for required causal, policy, and artifact fields before v2 is considered audit-ready.

## Deliverables
- A runtime split into planner, registry, policy engine, executor, memory, and observer
- A step-oriented trace store and audit schema
- Redacted structured events aligned with stored trace records
- CLI-visible trace and audit inspection surfaces
- Eval coverage for trace completeness

## Exit Criteria
- Every capability step is causally inspectable after execution.
- Audit output can explain what capability was chosen, why it was chosen, what policy decided, and what side effects occurred.
- Runtime subsystem boundaries are explicit enough to test independently.
- Stored traces contain the minimum replay-grade data needed for future rerun work even though rerun is not yet exposed.
- Trace completeness evals pass for normal success, approval, block, and failure scenarios.

## Test Focus
- Step-level trace persistence and retrieval ordering
- Trace edge integrity and causal reconstruction
- Redaction behavior across logs and stored artifacts
- Audit query accuracy for success, approval, block, and failure flows
- Trace completeness eval coverage and regression checks

## Handoff Beyond Stage 3
Once v2 traceability is stable, future work can add selective rerun, branch comparison, and stronger planner steering on top of the stored trace model without reopening the core registry or policy architecture.
