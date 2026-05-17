# Stage 6: Memory, Approvals, and Guardrails

## Goal
Add persistence and safety controls so Foundation CLI can remember what happened and require explicit user approval for risky actions.

## Entry Criteria
- Stage 5 exit criteria are met.
- The orchestrator can already produce and execute validated actions.

## Locked Decisions
- Use stdlib `sqlite3` directly for MVP persistence.
- Keep approval policy separate from execution logic.
- Treat the configured workspace root as the default file-operation boundary.
- Require explicit approval for destructive, boundary-crossing, or install/network actions.

## Public Interfaces Introduced
- Session and event persistence schema
- Approval request/result schema
- Policy classification and guardrail services
- History query surface for CLI consumption

## Step-by-Step Plan
1. Design the SQLite schema for:
   - sessions
   - user messages
   - assistant plans
   - tool calls
   - executed commands
   - approval decisions
   - summarized outcomes
2. Implement persistence services with clear migration or bootstrap logic appropriate for an MVP.
3. Implement policy classification for:
   - safe read-only actions
   - workspace modifications
   - destructive actions
   - recursive operations
   - network installs or downloads
   - permission changes
4. Implement workspace confinement:
   - normalize candidate paths
   - reject disallowed paths
   - enforce ignore rules where relevant
5. Add approval flows that the orchestrator can invoke before risky execution.
6. Add history queries so prior commands and approvals can be inspected later.
7. Add temp-file staging for risky file rewrites that need a safer replacement path.

## Deliverables
- SQLite-backed history and audit trail
- Approval system
- Guardrail and path-confinement services
- CLI-visible history capability

## Exit Criteria
- Risky actions cannot execute without the right approval path.
- Every executed action and approval decision is persisted.
- Guardrail rejections explain why execution was blocked.
- History retrieval is reliable enough to support user inspection and later REPL features.

## Test Focus
- Policy classification rules
- Path confinement edge cases
- Approval-required and approval-denied flows
- Persistence correctness and retrieval ordering
- Large payload and oversized-output handling

## Handoff to Stage 7
Once safety and persistence are in place, build the interactive experience that exposes them cleanly to users.
