# Foundation CLI v0.1 Roadmap

## Purpose
This planning set is the stage-zero baseline for Foundation CLI. It turns the MVP direction into an execution sequence with explicit gates so implementation can move from an empty repository to a usable v0.1 without scope drift.

Foundation CLI v0.1 should ship as a local-first, real-shell-backed command-line assistant with:
- a clear `plan -> approve -> execute -> observe` lifecycle,
- a practical interactive shell,
- a thin model adapter,
- a reliable shell runtime,
- useful local tools,
- simple memory and approvals,
- strong logging and guardrails.

## Planning Artifacts
Stage zero creates these canonical planning documents:
- `plans/00-roadmap.md`
- `plans/01-foundation-and-scaffolding.md`
- `plans/02-cli-surface-and-config.md`
- `plans/03-shell-runtime.md`
- `plans/04-tooling-and-local-context.md`
- `plans/05-model-adapter-and-orchestrator.md`
- `plans/06-memory-approvals-and-guardrails.md`
- `plans/07-interactive-chat-and-repl.md`
- `plans/08-observability-and-devx.md`
- `plans/09-hardening-and-v0.1-release.md`

Implementation should begin from `00-roadmap.md`, then advance numerically through the stage files.

## Locked Defaults
- Python target: `3.12`
- Initial platform target: macOS only
- Packaging style: installable CLI named `foundation`
- Runtime design: standard-library-first, thin abstractions, explicit control flow
- Model path: single provider for v0.1, adapter shaped for future expansion
- UI stack: `Typer` + `prompt_toolkit` + `Rich`
- Storage: SQLite via stdlib `sqlite3`
- Config format: TOML
- Secrets: system keychain via `keyring`

## MVP Boundaries
### In scope
- Interactive CLI and chat entrypoints
- Real subprocess execution with streaming
- PTY support on macOS where needed
- Typed module boundaries with Pydantic
- Local context tools (`git`, `rg`, `fd`, `man`, `tldr`)
- Approvals, history, and audit trail
- Structured logs and developer tooling

### Out of scope for v0.1
- Windows support
- Full-screen TUI
- ORM adoption
- Live file watching
- OpenTelemetry export
- Multi-provider routing
- Multi-agent frameworks
- Vector databases

## Architecture Map
Foundation CLI should be built around these modules:
- CLI surface: command tree, help, flags, config inspection
- Interactive shell: prompt loop, history, completion, approval UX
- Orchestrator: request intake, planning, policy checks, execution sequencing
- Model adapter: provider-specific request/response boundary
- Shell runtime: subprocess and PTY execution
- Tool executor: local context tools and normalized tool results
- Memory/config: TOML settings, secrets, SQLite history
- Guardrails: workspace confinement, approval policies, destructive-action checks
- Observability: logs, metrics-ready event schema, diagnostics

## Stage Sequence
| Stage | Outcome | Blocks Next Stage Until | Primary Artifact |
| --- | --- | --- | --- |
| 0 | Planning baseline is written and sequenced | Roadmap and all stage plans exist | `plans/` |
| 1 | Repo scaffolding and quality tooling exist | Package installs, checks run, CLI stub works | Project skeleton |
| 2 | CLI surface and config system are usable | Commands parse, config validates, secrets resolve | Command surface |
| 3 | Real shell runtime works | Shell commands stream, timeout, cancel, and clean up correctly | Executor |
| 4 | Local tools and workspace context work | Tool wrappers are typed and degrade cleanly | Tool layer |
| 5 | Orchestrator and model adapter work | Structured plans can be validated and executed safely | AI runtime |
| 6 | Memory, approvals, and guardrails are enforced | Risky actions are gated and persisted | Safety layer |
| 7 | Interactive chat and REPL are usable | Users can inspect and approve actions in-session | UX loop |
| 8 | Observability and dev workflow are solid | Logs and quality gates make issues diagnosable | Ops baseline |
| 9 | MVP is hardened and releasable | End-to-end scenarios pass and docs are complete | v0.1 release |

## Stage Gate Rules
Every stage must satisfy these rules before the next stage begins:
1. The previous stage's exit criteria are fully met.
2. New behavior added in the stage has automated tests.
3. Public interfaces added in the stage are documented in the relevant plan and code docs.
4. Failure modes are handled explicitly instead of being left implicit.
5. Logging exists for the main request and failure paths introduced by the stage.
6. Known limitations are recorded if they are intentionally deferred.

## Cross-Stage Standards
- Keep module contracts typed with Pydantic models.
- Prefer `shell=False` and argument lists for process execution.
- Keep policy checks separate from UI code and execution code.
- Avoid introducing dependencies unless they materially improve UX, correctness, or safety.
- Treat the workspace root as the default trust boundary.
- Keep hidden autonomy out of the MVP; the user should always be able to inspect planned actions.

## Delivery Order
1. Build the baseline repo and toolchain.
2. Establish a stable CLI and config story.
3. Make execution reliable before adding AI planning.
4. Add local context tools before model orchestration so the model has grounded inputs.
5. Add the orchestrator only once execution and tool primitives are stable.
6. Add approvals and persistence before optimizing the REPL experience.
7. Improve observability before final hardening so debugging data is available.
8. Harden only after the full request loop exists.

## Definition of Done for v0.1
Foundation CLI v0.1 is done when a user can:
- install the package locally,
- configure one provider,
- open `foundation chat`,
- request a task,
- inspect the proposed actions,
- approve or reject risky steps,
- execute real shell commands,
- review history and prior approvals,
- diagnose failures from logs and clear CLI output.
