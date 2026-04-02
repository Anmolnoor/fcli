# Stage 3: Shell Runtime

## Goal
Build the execution engine that runs real shell commands safely and predictably. This is the runtime core of Foundation CLI and must be stable before model-driven orchestration starts.

## Entry Criteria
- Stage 2 exit criteria are met.
- Config models exist for workspace root, timeouts, and execution defaults.

## Locked Decisions
- Use `subprocess` for buffered execution.
- Use `asyncio.subprocess` for streaming and concurrent execution.
- Use `pty` on macOS when a command needs terminal semantics.
- Default to argument-list execution with `shell=False`.

## Public Interfaces Introduced
- `ShellCommandRequest`
- `ShellCommandResult`
- `ExecutionMode` for buffered, streamed, and PTY-backed execution
- Normalized execution error types for timeout, cancellation, and spawn failure

## Step-by-Step Plan
1. Define the execution request/result models:
   - command and args
   - cwd
   - env overlay
   - timeout
   - stream mode
   - PTY requirement
   - approval metadata hook
2. Implement buffered execution:
   - capture stdout/stderr
   - return exit code and duration
   - normalize failure conditions
3. Implement streaming execution:
   - asynchronously consume output
   - emit incremental events for the REPL and logs
   - preserve final result summary
4. Implement PTY execution for commands that behave poorly over pipes.
5. Add timeout and cancellation handling:
   - terminate the process cleanly
   - escalate to kill when needed
   - ensure child cleanup is reliable
6. Add cwd/env isolation and workspace-path validation.
7. Add execution summaries that can be rendered safely in the CLI.

## Deliverables
- Reusable shell execution service
- Buffered, streaming, and PTY-backed command paths
- Reliable cleanup behavior
- Normalized results suitable for logging and orchestration

## Exit Criteria
- Simple commands execute correctly.
- Failing commands preserve stderr and exit codes correctly.
- Long-running commands stream output without freezing the caller.
- Timed-out and cancelled commands are cleaned up reliably.
- PTY-backed execution works on macOS for targeted command classes.

## Test Focus
- Success and non-zero exit paths
- Timeout and cancellation handling
- Environment and cwd overrides
- Streaming output ordering
- PTY-backed command behavior where testable

## Handoff to Stage 4
Do not add model orchestration yet. First make local tools consume this runtime so the execution layer proves itself under realistic usage.
