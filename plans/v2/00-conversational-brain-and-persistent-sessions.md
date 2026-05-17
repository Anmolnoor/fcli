# Stage 00: Conversational Brain and Persistent Sessions

## Goal
Turn `foundation chat` into a real terminal-first agent shell on top of the existing v1 runtime. This stage should let a user talk to Foundation continuously, use the current v1 tools and approvals during the conversation, and persist memory and session state across turns and restarts in a way that feels closer to modern terminal coding agents.

## Entry Criteria
- The current v1 interactive chat loop, one-shot orchestrator, approval flow, and history store are working as an MVP baseline.
- The current limitations around long-lived memory, transcript growth, and session continuity are understood from the existing code and docs.
- The v2 registry, policy, and trace refactors have not yet replaced the current v1 execution stack.

## Locked Decisions
- This stage is a bridge stage that reuses the existing v1 runtime rather than waiting for the later v2 capability registry.
- The terminal experience remains `prompt_toolkit` plus `Rich`, not a full-screen TUI.
- Memory stays local, explicit, and inspectable through markdown files and SQLite-backed session state, not a hidden vector database or cloud-only memory service.
- The memory model is layered:
  - global user memory at `~/.config/foundation/FOUNDATION.md`
  - project memory at `<workspace-root>/FOUNDATION.md`
  - active session memory as persisted transcript, compacted summary, and session metadata in Foundation state storage
- Memory files are plain markdown that the user can inspect and edit directly.
- Existing v1 tool wrappers, direct shell execution, provider adapters, approvals, and history persistence remain the execution substrate for this stage.

## Public Interfaces Introduced
- `BrainSession`
- `SessionManager`
- `SessionCheckpoint`
- `SessionSnapshot`
- `MemoryLayer`
- `MemorySource`
- `MemoryEnvelope`
- `ConversationCompactor`
- `ResumeTarget`
- `InteractiveCommand`

## Step-by-Step Plan
1. Define the long-lived chat session model:
   - stable session ids for interactive chat
   - per-turn records for user input, assistant output, planned actions, approvals, and execution results
   - session checkpoints for resume and crash recovery
2. Define the layered memory model and prompt assembly order:
   - system and developer prompts
   - global user memory from `~/.config/foundation/FOUNDATION.md`
   - project memory from `<workspace-root>/FOUNDATION.md`
   - compacted session summary and recent turn window
   - current workspace context from the existing v1 tool layer
   - current user request
3. Extend the `foundation chat` session surface so it behaves like a persistent agent shell:
   - `foundation chat` resumes the latest compatible session by default
   - `foundation chat --new` starts a fresh session
   - `foundation chat --resume <session-id>` resumes a specific session
   - add slash commands for `/memory`, `/sessions`, `/resume`, `/compact`, `/model`, `/tools`, while keeping the existing `/help`, `/history`, `/config`, `/cwd`, `/approval`, `/clear`, and `/reset`
4. Implement context compaction and checkpointing:
   - compact long transcripts into a durable session summary when thresholds are exceeded or the user explicitly requests `/compact`
   - checkpoint after each completed turn and direct shell action
   - restore the last clean checkpoint when a session is resumed after interruption
5. Reuse the existing v1 runtime inside the new session shell:
   - current local tool wrappers remain available
   - direct shell commands via `!` remain available
   - the current planner-orchestrator path remains the turn execution engine
   - existing approval modes and history persistence remain in force
6. Make session memory visible and controllable:
   - render which memory sources are loaded for the current session
   - expose the current session id, model, cwd, and approval mode in the interactive UI
   - allow inspecting and editing memory files from the chat surface
7. Add persistence and recovery rules that keep conversations coherent:
   - interrupted turns are marked explicitly
   - failed tool or shell turns are still attached to the session record
   - resumed sessions load compacted memory plus the most recent turn window instead of replaying the entire raw transcript every time

## Deliverables
- A persistent terminal chat experience on top of the current v1 runtime
- A layered memory model with user, project, and session memory
- Session resume, checkpoint, and compaction behavior
- A richer slash-command surface for session control
- Visible memory loading and session state in the interactive UI

## Exit Criteria
- A user can hold a multi-turn conversation with Foundation across restarts and keep useful continuity.
- Existing v1 tools, shell execution, approvals, and history still work inside the persistent chat session.
- Session memory is loaded deterministically from the defined layers and can be inspected by the user.
- Transcript compaction keeps context bounded without losing critical instructions or recent task state.
- Session resume is reliable enough to recover from normal interruption without starting from scratch.

## Test Focus
- Memory layer loading and precedence
- Resume behavior for latest and explicit session ids
- Transcript compaction thresholds and summary correctness
- Slash-command routing for memory, session, and model controls
- Recovery from interrupted, failed, or partially completed turns
- Consistency between interactive session state and persisted history records

## Handoff to Stage 1
Once Foundation behaves like a persistent terminal agent shell on top of the current v1 runtime, replace the fixed built-in tool surface with the v2 capability registry without changing the user’s conversational mental model.
