# Stage 3: Native Git Capabilities and Approval Boundaries

## Goal
Split git operations into typed inspect and mutation capabilities so coding turns can inspect repository state, stage intended paths, and request commit approval without routing everything through shell commands or one broad git summary helper.

## Entry Criteria
- Stage 2 exit criteria are met.
- The new file layer can produce stable changed-path metadata.
- The current git summary helper, trace behavior, and approval model are understood.

## Locked Decisions
- The new built-ins are:
  - `foundation.git.status`
  - `foundation.git.diff`
  - `foundation.git.show`
  - `foundation.git.log`
  - `foundation.git.stage`
  - `foundation.git.unstage`
  - `foundation.git.commit`
- `git.status`, `git.diff`, `git.show`, and `git.log` are inspect-only helpers with structured outputs.
- `git.stage` and `git.unstage` operate only on explicit path lists inside the workspace.
- `git.commit` operates only on already staged changes and never stages implicitly.
- `git.commit` requires approval under the default v3 policy posture.
- Push, fetch, pull, and PR automation stay out of scope for v3.
- Planner guidance should prefer typed git capabilities over shell git commands for common repo-state inspection and staging tasks.

## Public Interfaces Introduced
- `foundation.git.status`
- `foundation.git.diff`
- `foundation.git.show`
- `foundation.git.log`
- `foundation.git.stage`
- `foundation.git.unstage`
- `foundation.git.commit`
- `GitStatusRequest`
- `GitStatusResult`
- `GitDiffRequest`
- `GitDiffResult`
- `GitShowRequest`
- `GitShowResult`
- `GitLogRequest`
- `GitLogResult`
- `GitStageRequest`
- `GitUnstageRequest`
- `GitCommitRequest`
- `GitMutationResult`
- `GitOperationError`

## Step-by-Step Plan
1. Introduce a dedicated git service that resolves the active repository from the request cwd or workspace root.
2. Define typed request and result models for inspect helpers with explicit truncation flags and size limits.
3. Register the new git capabilities in the registry with separated risk profiles:
   - low-risk inspect operations,
   - workspace mutation for stage and unstage,
   - approval-gated commit.
4. Implement inspect helpers:
   - `git.status` for branch, staged state, unstaged state, and conflict indicators,
   - `git.diff` for staged or unstaged diffs and diff stats,
   - `git.show` for commit or object inspection,
   - `git.log` for recent commit history.
5. Implement `git.stage` and `git.unstage` using explicit path lists only, with changed-path metadata returned for traces and concise notices.
6. Implement `git.commit` with precondition checks:
   - staged changes must already exist,
   - commit message must be non-empty,
   - no implicit staging,
   - approval must be resolved before commit runs.
7. Update planner instructions so common git flows use typed helpers:
   - inspect repo state,
   - stage intended files,
   - unstage mistaken files,
   - request commit approval when appropriate.

## Edge Cases and Failure Modes
- If the cwd is not inside a git repository, inspect and mutation helpers must fail with a structured not-a-repo error.
- Nested repositories should resolve against the repository that owns the request cwd, not blindly against the top-level workspace root.
- Detached HEAD, merge conflicts, rebase state, or unresolved index conflicts must surface clearly in `git.status`.
- Empty path lists for stage or unstage should fail fast instead of acting like broad `git add -A` behavior.
- Explicit paths that resolve outside the active repository or outside the workspace must be rejected.
- Binary diffs, renames, and deletions must stay visible in structured diff output even if full patch bodies are truncated.
- `git.commit` with no staged changes must fail with a precondition error.
- `git.commit` approval prompts should summarize staged paths so the user can spot unrelated staged changes before approving.
- Approval-denied or approval-pending commit flows must stop the orchestration loop cleanly without mutating staged state.
- Missing git binaries or unsupported git features must return structured capability errors instead of generic shell failures.

## Deliverables
- A dedicated git service with typed inspect and mutation helpers
- Seven registry-native git capabilities with clear policy boundaries
- Approval-gated commit behavior that never stages implicitly
- Planner guidance that uses typed git operations for common coding workflows

## Exit Criteria
- Git inspect flows no longer depend on one monolithic summary capability or generic shell commands for normal cases.
- Stage and unstage operate only on explicit workspace paths.
- Commit requires approval and fails cleanly when preconditions are not met.
- Git outputs are structured enough for replanning, concise notices, and trace artifacts.

## Test Focus
- Status, diff, show, and log structured outputs
- Path-bounded stage and unstage behavior
- Commit preconditions and approval handling
- Not-a-repo, detached-HEAD, and conflict scenarios
- Binary, rename, and delete visibility in diff output

## Handoff to Stage 4
Once file and git operations are both typed and policy-aware, upgrade the orchestrator from one planning pass to a bounded coding loop that can use those capabilities to repair its own failed verification steps.
