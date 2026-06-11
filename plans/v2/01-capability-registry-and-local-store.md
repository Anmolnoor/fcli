# Stage 1: Capability Registry and Local Store

**Status: shipped (v2 complete; see git history).**

## Goal
Replace the hardcoded tool surface with a first-class capability system that can represent built-in tools, future skills, and user-created extensions through one typed registry. This stage establishes the local store and metadata model that every later v2 policy, audit, and execution path will depend on.

## Entry Criteria
- Stage 00 exit criteria are met.
- The v0.1 repository structure, runtime, and current planning loop are understood and stable enough to refactor.
- The current built-in tools, shell execution path, approval flow, and history model have been reviewed as migration inputs.
- v2 is treated as a clean-break architecture, not a compatibility-preserving patch on top of the fixed `ToolName` model.

## Locked Decisions
- The v2 store is local-first and does not require a remote marketplace.
- Capabilities are loaded through an external-service boundary rather than in-process Python plugins.
- Built-in tools and shell-backed execution must be represented as capabilities instead of special runtime exceptions.
- Registry metadata must be typed and validated before a capability becomes selectable by the planner.
- Capability lifecycle support includes create, register, install, inspect, enable, disable, version, and remove operations.

## Public Interfaces Introduced
- `CapabilityKind`
- `CapabilityTransport`
- `CapabilityManifest`
- `CapabilityVersion`
- `CapabilityId`
- `CapabilityInstallSource`
- `CapabilityState`
- `CapabilityHealth`
- `RiskClass`
- `TrustTier`
- `CapabilityRegistry`
- `CapabilityStore`
- `CapabilityResolver`
- `CapabilitySnapshot`

## Step-by-Step Plan
1. Define the core capability taxonomy:
   - capability identity and versioning
   - capability kind for tools and skills
   - transport and service endpoint metadata
   - declared input and output schema references
   - declared side effects, scopes, and risk metadata
2. Define the manifest contract for installed capabilities:
   - unique id and semantic version
   - human-readable name and description
   - transport configuration and runtime endpoint
   - schema references for input and output payloads
   - install source, owner, and provenance fields
   - risk class, trust tier, and declared side effects
   - enablement and health state
3. Define the local store layout and persistence model:
   - canonical on-disk root for installed capabilities
   - manifest storage, lifecycle metadata, and health cache
   - user-authored capability creation flow
   - version-aware registration and resolution rules
4. Implement the registry service:
   - list available capabilities
   - resolve one capability by id and version
   - register and validate manifests
   - enable, disable, and remove capabilities
   - emit a planner-facing snapshot of only healthy and enabled capabilities
5. Seed the registry with built-in capability manifests for the current local tools and shell-backed execution path so the planner and executor stop depending on a hardcoded built-in tool list.
6. Add capability health checks and doctor integration:
   - manifest validity
   - endpoint reachability or adapter health
   - schema compatibility
   - missing dependency reporting
7. Replace the planner’s fixed tool context with a capability snapshot that includes capability metadata, availability, trust tier, and declared risk so later stages can make policy-aware decisions from the same source of truth.

## Deliverables
- A typed capability manifest model
- A local-first capability store
- A registry service that manages capability lifecycle and planner snapshots
- Built-in capabilities registered through the same path as user-created capabilities
- Health and validation checks for installed capabilities

## Exit Criteria
- The planner no longer depends on a fixed tool enum or hardcoded built-in tool list.
- New capabilities can be registered, validated, enabled, disabled, and versioned locally.
- Built-in tools and shell-backed actions are exposed through the registry path.
- Only enabled and healthy capabilities are visible to the planner and executor.
- Invalid or unhealthy capabilities fail closed with clear diagnostics.

## Test Focus
- Manifest validation and schema compatibility failures
- Registry lifecycle operations and version resolution
- Store bootstrap and persistence behavior
- Built-in capability seeding and planner snapshot generation
- Health-check failures, disabled capability handling, and missing runtime dependencies

## Handoff to Stage 2
Do not expand execution autonomy until every runnable thing is represented as a capability with validated metadata. Stage 2 depends on registry metadata being the canonical source for risk, scope, trust, and lifecycle state.
