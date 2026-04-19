# Stage 2: File Capabilities and Safe Text Editing

## Goal
Replace shell-based file inspection and editing with typed registry-native file capabilities. The v3 file layer should make workspace text reads and edits deterministic, conflict-aware, and traceable so the planner can stop treating shell commands like `cat`, `sed`, or heredoc-driven rewrites as the default editing path.

## Entry Criteria
- Stage 1 exit criteria are met.
- The capability registry, policy engine, and executor can already host built-in capabilities.
- The current shell-heavy file access patterns in planning and execution are understood.

## Locked Decisions
- File capabilities are workspace-only and text-only in v3.
- The new built-ins are:
  - `foundation.file.read`
  - `foundation.file.read_chunk`
  - `foundation.file.write`
  - `foundation.file.edit`
  - `foundation.file.apply_diff`
- `file.read` reads one text file up to 256 KB and returns content, encoding, line count, and `sha256`.
- Files above 256 KB must fail with a structured error that tells the caller to use `file.read_chunk`.
- `file.read_chunk` is line-based:
  - default `max_lines` is 200,
  - maximum `max_lines` is 400.
- `file.write` creates a new file or overwrites only when `overwrite=true`.
- `file.edit` rewrites an existing file with full content and requires `expected_sha256`.
- `file.apply_diff` accepts unified diffs for workspace text files only and applies atomically.
- Full-file rewrite is the canonical edit path in v3. `apply_diff` is supported but secondary.
- All mutations use temp staging plus atomic replace and return changed-path and diff-summary metadata.
- Shell mutation commands are no longer the default path for code edits when file capabilities are available.

## Public Interfaces Introduced
- `foundation.file.read`
- `foundation.file.read_chunk`
- `foundation.file.write`
- `foundation.file.edit`
- `foundation.file.apply_diff`
- `FileReadRequest`
- `FileReadResult`
- `FileReadChunkRequest`
- `FileReadChunkResult`
- `FileWriteRequest`
- `FileEditRequest`
- `FileApplyDiffRequest`
- `FileMutationResult`
- `FileOperationError`

## Step-by-Step Plan
1. Define typed request, result, and error models for every file capability.
2. Introduce a dedicated file service that owns:
   - workspace path resolution,
   - text-versus-binary detection,
   - supported encoding detection,
   - `sha256` calculation,
   - line counting,
   - diff-stat generation,
   - atomic temp-write and replace behavior.
3. Register the new built-in file capabilities in the registry with explicit scope and side-effect metadata.
4. Implement `file.read`:
   - reject non-text files,
   - reject oversized files with a structured fallback error,
   - return content, encoding, line count, and `sha256`.
5. Implement `file.read_chunk`:
   - clamp `max_lines` to the v3 limit,
   - return requested start line, actual end line, total line count, and `sha256`,
   - keep chunking stable even when the request starts near EOF.
6. Implement `file.write` and `file.edit`:
   - `write` allows create and explicit overwrite,
   - `edit` requires an existing file and exact `expected_sha256`,
   - both return changed-path and diff-summary metadata for trace and concise notices.
7. Implement `file.apply_diff` against a staged workspace snapshot so any failed hunk aborts the whole patch.
8. Update planner instructions and executor selection logic so:
   - reads prefer file capabilities over shell commands,
   - code edits prefer `file.edit` or `file.write`,
   - `file.apply_diff` is used only when a unified patch is the natural output.

## Edge Cases and Failure Modes
- Files containing NUL bytes or undecodable content must fail closed as non-text or unsupported-encoding inputs.
- v3 should support deterministic encodings instead of guess-heavy heuristics. UTF-8 and UTF-8 with BOM are safe starting targets; ambiguous legacy encodings should return structured unsupported-encoding errors instead of silent corruption.
- `file.read` on a file larger than 256 KB must not partially return content; it should return a structured “use read_chunk” failure.
- `file.read_chunk` requests past EOF should return an empty chunk with correct total line count instead of throwing an opaque crash.
- `file.write` with `overwrite=false` against an existing file must fail with a clear conflict error.
- `file.edit` with a stale `expected_sha256` must fail with a structured hash-conflict error so the next iteration can reread and retry.
- Symlinks or path traversals that escape the workspace must be rejected even if the final on-disk target exists.
- Atomic overwrite should preserve existing file permissions when replacing a file that already exists.
- `file.apply_diff` must not leave partial changes behind when one hunk fails.
- v3 should reject delete-only or rename-style diffs in `file.apply_diff` rather than silently treating them as generic destructive edits.

## Deliverables
- A dedicated file service for workspace text operations
- Five registry-native file capabilities with typed contracts
- Atomic file mutations with conflict detection and diff summaries
- Planner guidance that prefers typed file capabilities over shell editing hacks

## Exit Criteria
- The planner and executor can read and modify workspace text files without relying on shell mutation commands.
- Oversized files and non-text files fail with structured, actionable errors.
- File edits are atomic and conflict-aware.
- Changed-path and diff-summary metadata are available to traces and concise notices.
- Shell remains available for verification and environment inspection, not as the default code-editing path.

## Test Focus
- Normal text reads and chunk reads
- Oversized-file fallback to `read_chunk`
- Non-text or unsupported-encoding rejection
- `file.write` overwrite rules
- `file.edit` hash-conflict handling
- Atomic `apply_diff` success and atomic failure rollback
- Workspace-bound path enforcement and symlink escape rejection

## Handoff to Stage 3
Once file reads and edits are typed and safe, split git inspection and mutation out of the monolithic git summary path so coding turns can reason about repo state without shell-driven git hacks.
