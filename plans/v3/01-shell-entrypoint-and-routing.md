# Stage 1: Shell Entrypoint and Routing

## Goal
Make `foundation` the primary agent command without creating a second chat implementation. A bare `foundation` invocation should open or resume the interactive agent shell, and `foundation <request...>` should run a one-shot agent turn. Existing admin subcommands must keep precedence so operational commands still behave like administrative CLI surfaces instead of being misrouted into the agent.

## Entry Criteria
- The current v2 Stage 4 interactive shell and one-shot chat entrypoints work.
- The current CLI command tree and session-resume behavior are understood.
- The current admin subcommands and their flag parsing are covered by tests.

## Locked Decisions
- `foundation` and `foundation chat` must share one implementation path for interactive and one-shot agent behavior.
- Bare `foundation` starts or resumes the interactive shell.
- `foundation <request...>` runs a one-shot agent turn.
- Existing admin subcommands keep precedence over agent routing.
- Global render, approval, cwd, and session controls keep working for both bare and `chat` entrypoints.
- v3 does not add a second interactive shell surface and does not deprecate `foundation chat`.

## Public Interfaces Introduced or Changed
- `foundation`
- `foundation <request...>`
- `foundation chat`
- `AgentInvocationMode`
- `CLIRequestRoute`

## Step-by-Step Plan
1. Introduce one shared agent entrypoint that can run in interactive mode or one-shot mode.
2. Refactor top-level CLI routing so it decides between:
   - known admin subcommand,
   - bare agent shell invocation,
   - one-shot agent request.
3. Preserve the current session-management behavior for the interactive path:
   - resume latest compatible session by default,
   - still support explicit new or resume controls,
   - keep render and approval mode state available.
4. Make bare one-shot parsing consume the remaining CLI tokens as the request text instead of forcing the `chat` subcommand.
5. Keep `foundation chat ...` as a strict alias over the same code path so interactive and one-shot behavior remain identical.
6. Update help text and examples so the documented primary workflow is:
   - `foundation`
   - `foundation "fix the failing test"`
7. Preserve admin-path behavior for `run`, `tools`, `history`, `trace`, `config`, and `doctor`.

## Edge Cases and Failure Modes
- `foundation run` must route to the admin `run` subcommand, not to a one-shot request whose text is `run`.
- `foundation "run tests"` must still route to the one-shot agent path because the request text is quoted user input, not an admin subcommand.
- `foundation chat run tests` must behave exactly like `foundation run tests` would have behaved through the old chat alias path.
- `foundation --help` and subcommand-specific help must keep standard Typer help behavior.
- Empty quoted input such as `foundation ""` should fail with a clear usage error instead of opening the shell silently.
- Session-resume failures, missing prior sessions, or invalid explicit session ids must produce clear chat-path errors without breaking admin commands.
- Global flags must not be swallowed into request text accidentally.

## Deliverables
- A shared agent entrypoint used by both `foundation` and `foundation chat`
- Top-level CLI routing that preserves admin subcommand precedence
- One-shot bare-invocation support without duplicated shell code
- Updated help output and examples that document `foundation` as the primary entrypoint

## Exit Criteria
- `foundation` opens or resumes the interactive shell.
- `foundation <request...>` runs a one-shot agent turn.
- `foundation chat ...` remains fully supported and behaviorally identical.
- Admin subcommands still parse and execute exactly as admin commands.
- CLI help and error messages stay deterministic.

## Test Focus
- Bare `foundation` interactive routing
- Bare `foundation <request...>` one-shot routing
- Admin subcommand precedence for `run`, `tools`, `history`, `trace`, `config`, and `doctor`
- Alias parity between `foundation` and `foundation chat`
- Flag parsing, empty-input handling, and invalid-resume paths

## Handoff to Stage 2
Once the user can reliably enter the agent through `foundation` without breaking the rest of the CLI surface, replace shell-based code-reading and editing hacks with first-class file capabilities.
