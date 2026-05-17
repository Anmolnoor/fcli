# Stage 7: Interactive Chat and REPL

## Goal
Build the interactive command loop that makes Foundation CLI feel like a real assistant rather than a collection of subcommands.

## Entry Criteria
- Stage 6 exit criteria are met.
- The runtime, tool layer, orchestrator, and approvals already work outside a REPL context.

## Locked Decisions
- `prompt_toolkit` powers the interactive shell.
- `Rich` handles rendering of markdown, code, tables, and summaries.
- The MVP remains a terminal REPL, not a full-screen Textual application.
- Approval and execution state must stay visible and interruptible from the session.

## Public Interfaces Introduced
- Interactive session manager
- Prompt renderer and key bindings
- Slash-command surface for session-local controls
- REPL history integration with persisted memory

## Step-by-Step Plan
1. Implement the interactive input loop with:
   - multiline editing
   - history
   - inline suggestions
   - completion hooks
2. Add session-local commands such as:
   - help
   - history lookup
   - config inspection
   - session reset or clear
   - approval controls where appropriate
3. Render structured plans and execution summaries clearly:
   - what the assistant wants to do
   - what needs approval
   - what already executed
   - what failed
4. Wire streaming execution output into the session so long-running commands remain readable.
5. Distinguish shell-style commands from natural-language requests in a predictable way.
6. Persist session history so prior context is available when reopening the CLI.

## Deliverables
- `foundation chat` interactive experience
- Session history and completion behavior
- Readable plan and approval UX
- Streaming output integration

## Exit Criteria
- Users can hold a practical interactive session from the terminal.
- Approval prompts are readable and do not hide execution consequences.
- Streaming output does not corrupt the prompt state.
- Session history persists and can be revisited.

## Test Focus
- REPL parsing and command routing
- History persistence
- Completion hooks and multiline behavior where unit-testable
- Rendering logic for plans, approvals, and execution summaries

## Handoff to Stage 8
After the full user loop exists, strengthen logs and developer workflow so the integrated system is diagnosable and maintainable.
