# Stage 5: Model Adapter and Orchestrator

## Goal
Introduce AI-assisted planning without surrendering control flow. This stage should add a model adapter and an orchestrator that turns user requests into typed, inspectable actions.

## Entry Criteria
- Stage 4 exit criteria are met.
- Config, runtime, and tool primitives already exist and are stable.

## Locked Decisions
- v0.1 targets a single provider implementation first.
- The provider boundary must still be abstract enough to add more providers later.
- Model outputs must validate against Pydantic contracts before any execution is allowed.
- Hidden autonomous loops are out of scope.

## Public Interfaces Introduced
- `UserRequest`
- `PlannedAction`
- `ToolCall`
- `PolicyDecision`
- `ExecutionResult`
- `AssistantMessage`
- Provider adapter interface for prompt submission and structured responses

## Step-by-Step Plan
1. Define the provider adapter contract:
   - prompt input
   - structured output request
   - token usage and latency metadata
   - normalized provider error model
2. Implement the first provider integration with retries around network and transient provider failures.
3. Define the orchestrator state flow:
   - intake request
   - gather context
   - ask for a structured plan
   - validate plan
   - pass actions to policy
   - execute allowed actions
   - summarize observations
4. Define the structured action schema so it can express:
   - shell commands
   - tool calls
   - explanation-only responses
   - approval-required markers
5. Implement safe handling for invalid model output:
   - validation failures
   - missing required fields
   - unsupported tool names
   - incomplete plans
6. Add orchestration summaries that show what happened and what still needs approval.

## Deliverables
- First provider adapter
- Typed orchestrator loop
- Structured plan validation
- Execution path that stays inspectable and auditable

## Exit Criteria
- The model can produce bounded structured plans for simple tasks.
- Invalid structured output fails safely and visibly.
- The orchestrator can call local tools and shell execution through typed contracts.
- Retries do not cause duplicate shell execution.

## Test Focus
- Adapter success and failure behavior
- Structured output validation
- Orchestrator branching for plan-only, tool-call, and shell-action paths
- Retry safety and idempotence of non-execution operations

## Handoff to Stage 6
Do not optimize UX next. Add persistence and approvals first so the AI loop becomes safe and auditable before it becomes convenient.
