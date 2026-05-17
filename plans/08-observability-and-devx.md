# Stage 8: Observability and Developer Experience

## Goal
Make the system diagnosable for developers and maintainers. This stage should leave the project with clear logs, repeatable quality checks, and practical diagnostics.

## Entry Criteria
- Stage 7 exit criteria are met.
- The end-to-end request loop already exists, even if not yet fully hardened.

## Locked Decisions
- Use structured logging, preferably through `structlog`.
- Keep event names and payload shapes stable enough to support future telemetry export.
- Make local developer workflows simple and fast enough to run continuously during development.

## Public Interfaces Introduced
- Structured log schema for requests, plans, tool calls, and executions
- Diagnostics output surfaced through `foundation doctor`
- Repeatable quality-check commands documented for contributors

## Step-by-Step Plan
1. Define the event schema for:
   - session start and end
   - user request
   - plan generation
   - provider call
   - tool call
   - shell execution
   - approval request and response
   - exception and retry events
2. Implement structured log emission with redaction rules for secrets and sensitive payloads.
3. Add log directory handling using `platformdirs`.
4. Extend `foundation doctor` to report:
   - required binaries
   - config health
   - database health
   - provider readiness
   - log path information
5. Tighten the developer workflow:
   - lint
   - format
   - type-check
   - unit tests
   - async tests
   - coverage reporting
6. Add benchmark guidance or scripts for startup and common command latency.

## Deliverables
- Structured logging baseline
- Stronger doctor diagnostics
- Documented and automated developer quality workflow
- Basic performance measurement guidance

## Exit Criteria
- Common failures can be diagnosed from logs and doctor output.
- Sensitive data is not leaked in normal logs.
- Contributors have a clear, reproducible local quality loop.
- Performance checks exist for key shell and CLI flows.

## Test Focus
- Log emission and redaction behavior
- Doctor output correctness
- Quality command integration
- Failure-path observability

## Handoff to Stage 9
With diagnostics in place, move to hardening, release prep, and final scope trimming.
