# Stage 9: Hardening and v0.1 Release

## Goal
Turn the integrated system into a stable MVP release. This stage should focus on reliability, clarity, and scope discipline rather than new features.

## Entry Criteria
- Stage 8 exit criteria are met.
- The full request loop works end to end.

## Locked Decisions
- No new major features should be added in this stage.
- Scope reductions are allowed if they improve reliability or clarity.
- The release must document known limitations explicitly.

## Step-by-Step Plan
1. Run end-to-end scenarios that reflect real usage:
   - configure provider
   - open chat
   - inspect a plan
   - approve a risky step
   - run shell commands
   - inspect history
   - recover from provider or command failure
2. Review failure handling for:
   - missing binaries
   - invalid config
   - provider outages
   - malformed model outputs
   - blocked destructive actions
   - database corruption or missing database path
3. Trim dependencies and features that are not required for v0.1.
4. Tighten user-facing output so errors are actionable and concise.
5. Finalize documentation:
   - install
   - quickstart
   - configuration
   - external tools
   - limitations
   - safety model
6. Prepare versioning and release packaging for the first public milestone.

## Deliverables
- Hardened MVP behavior
- Release documentation
- Final dependency list
- Known-limitations list
- Versioned v0.1 release candidate

## Exit Criteria
- Core end-to-end scenarios pass reliably.
- Major failure modes are understandable and recoverable.
- The release docs match the actual product behavior.
- Remaining limitations are explicit instead of accidental.

## Test Focus
- End-to-end command and chat flows
- Regression coverage across earlier stage contracts
- Install and startup smoke tests
- Failure and recovery scenarios

## Release Acceptance
Foundation CLI v0.1 is ready when a user can install it, configure one provider, interact through `foundation chat`, inspect actions before execution, approve risky operations, execute real shell tasks, and review what happened afterward with confidence.
