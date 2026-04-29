# Stage 2: External Event Stream (persistent NDJSON log + optional live transports)

## Goal

Make every fcli session observable to external tooling — a future GUI window,
terminal pane, or scripted log analyzer — without requiring that tooling to be
attached *during* the run.

The primary surface is a **persistent, redacted NDJSON event log** written to
disk by default for every session. A third-party GUI can open past session
files later and render graphs / tables / timelines from them, no live
connection required.

The secondary surface, opt-in, is **live transports** — Unix domain socket
(default) with localhost HTTP/SSE fallback for tools that can't open AF_UNIX —
for monitors that prefer push to tailing the file. Live transports read from
the same in-memory fan-out as the file writer.

Both surfaces are **read-only**: subscribers (file readers or live clients)
observe; they do not steer the agent. The existing SQLite trace store is
untouched — it remains the source of truth for internal trace inspection; the
NDJSON log is the GUI-friendly surface for external tooling.

## Entry Criteria

- Stage 01 shipped. `ObserverService.event_sink` is in place and exercised by
  `LiveTurnRenderer`.
- All 22 `EVENT_*` payloads already pass through the existing redaction
  pipeline before reaching `event_sink`.
- The orchestrator's hot path is unchanged from v3 — sinks are passive
  callbacks called after redaction + history persistence.

## Locked Decisions

- **Persistence is on by default.** Every session writes a redacted NDJSON
  event log under
  `${XDG_STATE_HOME:-~/.local/state}/foundation/events/<session_id>.ndjson`,
  mode `0600`. Created on `session_start`, closed on `session_end`. An
  append-only `sessions.jsonl` index in the same directory lists
  `{session_id, started_at, ended_at, request_summary, status, file_path}`
  so a GUI can enumerate sessions without scanning every file.
- **Persistence runs in piped/CI runs too.** TTY auto-disable applies to the
  Stage 01 live UX only. The file writer keeps writing whenever `fcli` is
  invoked, so post-hoc tooling never misses a session. Users opt out with
  `--no-monitor` or `FOUNDATION_MONITOR=0`.
- **Retention.** Configurable cap on the events directory (default: keep last
  200 sessions / 500 MB, whichever is hit first). Oldest sessions are pruned
  on `session_end`; the index is rewritten in place. Configured under
  `AppSettings.monitor.retention`.
- **Live transports are opt-in.** Unix domain socket via `--monitor-socket`
  (default path `${XDG_RUNTIME_DIR:-$TMPDIR}/foundation/<pid>.sock`, mode
  `0700`); localhost HTTP/SSE via `--monitor-http=<port>` bound to
  `127.0.0.1` only — never `0.0.0.0`. Both env-var equivalents:
  `FOUNDATION_MONITOR_SOCKET=1`, `FOUNDATION_MONITOR_HTTP=<port>`. Sockets
  are removed on process exit (`atexit` + signal handlers).
- **Wire format: NDJSON.** One redacted event per line, UTF-8, `\n` terminated.
  Identical bytes go to the file writer and to live transports. SSE wraps the
  same NDJSON line in `data:` framing.
- **Schema:** the existing `ObserverEnvelope` payload, minus internal fields,
  versioned by an `event_schema_version` field on every line. Bumped on
  breaking change. Same version applies to file format and live wire format.
- **Auth (live only):** Unix socket relies on filesystem permissions (`0700` +
  owner-only dir). HTTP transport requires a per-process token printed on
  stdout at startup and passed via `Authorization: Bearer <token>`. No token,
  no stream. The on-disk log relies on file-system permissions (`0600`).
- **Read-only.** No control verbs on either surface. Live subscribers may only
  `subscribe` and `disconnect`; anything else is a 400. File readers obviously
  can't steer the agent.
- **Backpressure:** per-live-subscriber bounded queue (default 1024 events).
  On overflow, the slowest subscriber is dropped with a final
  `subscriber_overflow` line and a connection close. The file writer uses a
  small in-process buffer with `fsync` policy `per-batch` (default flush
  interval 200ms); on disk-full it logs WARNING and drops further writes for
  that session without ever blocking the agent.
- **Replay window for live transports: none.** New live subscribers receive
  only events emitted after their `subscribe` time. They can read the on-disk
  file for history. The SQLite trace store remains the source of truth for
  internal trace inspection; the NDJSON log is the external-tool surface.
- **Untouched: SQLite trace store.** Stage 02 does **not** modify the trace
  store schema or write path. It only adds a new sink alongside it.

## Public Interfaces Introduced

- `foundation.monitor.EventLogWriter` — context-manager, owns the per-session
  NDJSON file handle and the `sessions.jsonl` index. Always-on by default.
  Constructed from `AppSettings.monitor`.
- `foundation.monitor.MonitorServer` — context-manager, owns the live listener
  and per-subscriber queues. Optional, started only when a live transport flag
  is set. Constructed from `AppSettings.monitor`.
- `foundation.monitor.MonitorTransport` — protocol with two impls:
  `UnixSocketTransport` and `LocalHttpSseTransport`.
- `foundation.monitor.compose_event_sink(writer, server=None, live=None)` —
  returns a `Callable[[str, Mapping[str, Any]], None]` ready to pass to
  `ObserverService(event_sink=...)`. Internally fans the call out to the file
  writer and (when present) live subscribers' queues. Per-sink exceptions are
  swallowed.
- `AppSettings.monitor` — new Pydantic section with `enabled` (default
  `true`), `events_dir`, `retention` (sessions, bytes), `flush_interval_ms`,
  `live_transports: list["unix" | "http"]` (default empty), `socket_path`,
  `http_port`, `auth_token` (auto-generated when empty),
  `subscriber_queue_size`.
- CLI flags:
  - `--no-monitor` / `FOUNDATION_MONITOR=0` — force-off, beats env var. No
    NDJSON, no live transports.
  - `--monitor-socket[=<path>]` / `FOUNDATION_MONITOR_SOCKET=1` — enable Unix
    live transport at the given path (or default).
  - `--monitor-http=<port>` / `FOUNDATION_MONITOR_HTTP=<port>` — enable
    localhost HTTP/SSE live transport on the given port.
  - `--events-dir=<path>` — override the persistence directory.

## Step-by-Step Plan

1. **Settings + token plumbing.** Add `AppSettings.monitor` (default
   `enabled=true`). Generate a per-process auth token via
   `secrets.token_urlsafe(24)` only when a live HTTP transport is enabled.
   Surface the token in startup output (stdout once, never via the event
   stream). The on-disk log relies on `0600` perms, no token.
2. **`EventLogWriter` core.** Context-manager that opens
   `<events_dir>/<session_id>.ndjson` on `session_start` (mode `0600`,
   `O_APPEND`), buffers redacted envelopes, flushes per `flush_interval_ms`
   (or on `session_end`), and closes the file on `session_end`. Appends a
   row to `sessions.jsonl` on close. On disk-full / IOError, logs WARNING
   and drops further writes for that session — never blocks the agent.
3. **Retention sweep.** On `session_end`, after appending the index row,
   prune oldest sessions until the configured caps (count + bytes) are
   satisfied. Rewrite `sessions.jsonl` in place atomically (`.tmp` + rename).
4. **`MonitorServer` core (live).** A small server with a thread pool of size
   1 + N per subscriber. Maintains a `dict[subscriber_id, queue.Queue]`.
   Provides `register(transport_handle) -> subscriber_id`,
   `unregister(subscriber_id)`, `publish(envelope)`. Uses one background
   fan-out thread per subscriber to drain its queue and write to the
   transport. Started only when a live transport flag is set.
5. **`UnixSocketTransport`.** `socket.AF_UNIX` listener bound to the
   configured path with `0700` perms. `accept()` loop in a daemon thread.
   Each accepted connection becomes a subscriber. On disconnect (peer close,
   EPIPE), the subscriber is unregistered. Path cleaned up on shutdown.
6. **`LocalHttpSseTransport`.** Stdlib `http.server.ThreadingHTTPServer`
   bound to `127.0.0.1:<port>`. Single endpoint: `GET /events`. Validates
   `Authorization: Bearer <token>`. On valid auth, holds the connection
   open and writes SSE frames. `OPTIONS /events` returns the schema +
   version. Anything else returns 404 / 405 / 401.
7. **Event sink wiring.** `RequestOrchestrator` already accepts `event_sink`
   on its `ObserverService`. CLI startup composes the sink chain via
   `compose_event_sink(EventLogWriter, server=MonitorServer?, live=LiveTurnRenderer?)`.
   `EventLogWriter` is always present unless `--no-monitor` is set. The
   composer swallows per-sink exceptions so a misbehaving sink never breaks
   orchestration.
8. **Redaction guarantee (file + live).** Both `EventLogWriter.write` and
   `MonitorServer.publish` accept only *post-redaction* envelopes from
   `ObserverService.emit`. Unit tests assert that any payload containing the
   canary token is dropped before reaching the file *and* before reaching
   the live transport.
9. **Backpressure + overflow (live only).** Per-subscriber queue is bounded;
   full-queue `put_nowait` raises, the subscriber is marked overflowed, a
   final `subscriber_overflow` event is enqueued (force), and the connection
   is closed. The file writer uses its own small buffer; the agent thread
   continues without ever blocking on either path.
10. **Lifecycle.** `EventLogWriter.__exit__` flushes, fsyncs, closes the
    handle, appends/rewrites the index, runs retention. `MonitorServer.__exit__`
    closes the listener, drains queues, joins fan-out threads with a 1s
    timeout each, and removes the socket file. `atexit` + SIGTERM/SIGINT
    handlers also clean up to handle hard shutdowns (partial NDJSON files
    keep their content; the index entry is written with `status=interrupted`).
11. **Doctor surface.** `fcli doctor` reports the events directory, current
    session count and total bytes, retention caps, and whether persistence
    is currently enabled / disabled. Same surface notes whether live
    transports are listening and on which path/port.
12. **Privacy disclosure.** README and a one-time first-run notice document
    that fcli writes a redacted event log to `<events_dir>` for local
    tooling, and how to disable (`--no-monitor` / `FOUNDATION_MONITOR=0`).
13. **Doc + protocol spec.** `docs/monitor-protocol.md` (new): on-disk file
    layout, sessions index schema, NDJSON wire format, schema versioning
    policy, auth (live only), backpressure semantics, transport URIs,
    example client snippets in Python and Node for both file-tail and
    live-subscribe consumers.
14. **Tests.**
    - Unit: `EventLogWriter` round-trip — write N envelopes, close, reopen
      file, parse N lines, assert ordering and `event_schema_version`.
    - Unit: redaction-on-disk — canary token in payload never appears in
      the file.
    - Unit: retention prunes oldest by count and by bytes; index is
      rewritten atomically.
    - Unit: `EventLogWriter` keeps writing in piped/CI runs (no TTY).
    - Unit: `--no-monitor` / `FOUNDATION_MONITOR=0` produces no file and no
      index row.
    - Unit: `MonitorServer` registers/unregisters subscribers; `publish`
      delivers to all live subscribers; overflow drops the slow one only.
    - Unit: HTTP transport rejects missing/invalid token; accepts the right
      one; emits SSE-framed lines.
    - Unit: Unix transport sets `0700` mode; cleans up the path on exit.
    - Unit: `compose_event_sink` continues delivering to surviving sinks
      when one sink throws.
    - Integration: full-stack test driving a fake orchestrator turn and
      asserting both the on-disk NDJSON file *and* a connected Unix-socket
      subscriber receive the expected event sequence.
    - Integration: doctor reports the events directory and current usage.
    - Smoke: `fcli "<request>"` (no flags) produces a complete NDJSON file
      with `session_start` → `session_end`. `fcli --monitor-socket "..."`
      streams the same events live and the file is identical to the live
      stream byte-for-byte (modulo `subscriber_overflow`).

## Wire format

```
{"event_schema_version":"1","event":"iteration_started","ts":"2026-04-29T02:21:36Z","request_id":"d793b24a","session_id":"7d3ccb33","payload":{"iteration":1}}
{"event_schema_version":"1","event":"tool_call_started","ts":"...","request_id":"...","session_id":"...","payload":{"action_id":"1","capability_id":"foundation.file.read","summary":"Probe file existence"}}
...
{"event_schema_version":"1","event":"session_end","ts":"...","request_id":"...","session_id":"...","payload":{"status":"completed_inconclusive","stop_reason":"no_progress"}}
```

`event_schema_version` is bumped (e.g. `"2"`) only on a breaking change.
Additive fields keep the version unchanged.

## Auth model

| Surface | Auth |
| --- | --- |
| On-disk NDJSON | filesystem (`0600` file, `0700` dir, owner-only). No in-band token. |
| Unix socket (live) | filesystem (`0700`, owner-only). No in-band token. |
| Local HTTP/SSE (live) | `Authorization: Bearer <token>`. Token printed on stdout at startup; rotates per-process; never logged. |

Bind addresses are hard-coded: AF_UNIX or `127.0.0.1`. There is no remote
exposure path in stage 02. Operators who want remote access put their own
proxy in front and accept the consequences.

## Backpressure & failure modes

- **Slow live subscriber.** Queue fills → `subscriber_overflow` final event +
  disconnect. Other subscribers, the file writer, and the agent are unaffected.
- **Live subscriber crashes.** Write raises; subscriber is unregistered.
- **Sink raises.** `compose_event_sink` logs at WARNING and moves on.
  Orchestration is unaffected.
- **Disk full / IOError on file writer.** Logs WARNING once per session,
  drops further writes for that session, marks the index row
  `status=write_truncated`. Live transports (if any) keep working.
- **Live server can't bind.** Stage 02 fails-fast on the live transport with
  a clear error at startup; the agent continues to run **with persistence
  intact** (so a stale socket file doesn't brick the CLI). Operator gets a
  one-line warning + remediation hint.
- **Long turn, many subscribers.** Per-subscriber thread + bounded queue keeps
  memory and CPU O(N_subscribers); each subscriber is independent of the file
  writer.

## Files to add / modify

- `src/foundation/monitor/__init__.py` — **new.** Exports `EventLogWriter`,
  `MonitorServer`, `compose_event_sink`, transport classes.
- `src/foundation/monitor/event_log.py` — **new.** `EventLogWriter`, sessions
  index, retention sweep.
- `src/foundation/monitor/server.py` — **new.** `MonitorServer`, queue/fan-out
  logic, `compose_event_sink` helper.
- `src/foundation/monitor/transports.py` — **new.** Unix + HTTP/SSE impls.
- `src/foundation/monitor/protocol.py` — **new.** Envelope serialization,
  schema-version constant, redaction-canary test hook.
- `src/foundation/settings.py` — `AppSettings.monitor` section + validator
  (default `enabled=true`, retention caps, events_dir, live_transports list).
- `src/foundation/cli.py` — `--no-monitor` / `--events-dir` /
  `--monitor-socket[=<path>]` / `--monitor-http=<port>` flags; compose sink
  chain at startup; print token line only when HTTP is on.
- `src/foundation/services/doctor.py` — events dir, usage, retention,
  live-transport status surface.
- `README.md` — privacy disclosure: where the event log lives, how to disable.
- `docs/monitor-protocol.md` — **new.** On-disk layout, wire format, client
  docs (file-tail and live-subscribe).
- `tests/test_event_log.py` — **new.** Round-trip, redaction-on-disk,
  retention, no-monitor opt-out, piped-stdout still writes.
- `tests/test_monitor_server.py` — **new.**
- `tests/test_monitor_transports.py` — **new.**
- `tests/test_monitor_integration.py` — **new.** Drive a fake turn end-to-end
  with both file and Unix-socket subscribers attached; assert byte parity.
- `tests/test_doctor_monitor.py` — **new.** Doctor reports events dir + usage.

## Edge Cases and Failure Modes

- **`XDG_RUNTIME_DIR` / `XDG_STATE_HOME` unset on macOS.** Live socket falls
  back to `$TMPDIR/foundation/`; persistence falls back to
  `~/Library/Application Support/foundation/events/` (or
  `~/.local/state/foundation/events/` on Linux when `XDG_STATE_HOME` unset).
  Both created with `0700`. Document the path in the protocol doc.
- **Stale socket file from a prior crash.** On startup, if the path exists,
  attempt `connect`; if it fails, `unlink` and rebind. If `connect` succeeds,
  abort with a clear message ("another fcli is already serving here").
- **Multiple fcli processes on the same machine.** Default socket path is
  pid-scoped (`<pid>.sock`), so they never collide. The events directory is
  shared; each session has its own `<session_id>.ndjson`, so concurrent
  writes don't conflict. The `sessions.jsonl` index uses an
  advisory file lock (`fcntl.flock`) for the append + retention rewrite.
- **`fcli "<one-shot>"` (no flags).** EventLogWriter writes a complete file;
  no live transport runs. Future GUI opens the file and renders the session.
- **`fcli "<one-shot>" --monitor-socket`.** Live server starts, runs the
  single turn, shuts down. Live subscribers see `session_start` → ... →
  `session_end`, then receive a normal close. The on-disk file is identical.
- **Ctrl-C / hard kill mid-turn.** Signal handler flushes the file, closes
  the index with `status=interrupted`, closes the live listener, and drains
  queues with a 1s grace per subscriber. Partial NDJSON is retained.
- **Live UX + persistence + live transport all on simultaneously.** All
  three sinks receive every event; none blocks the others.

## Deliverables

- `EventLogWriter` + per-session NDJSON files + `sessions.jsonl` index, **on
  by default**, with retention.
- `MonitorServer` + Unix and HTTP/SSE live transports, **opt-in**.
- `--no-monitor` / `--events-dir` / `--monitor-socket` / `--monitor-http`
  CLI flags + env-var equivalents + auth-token startup line (HTTP only).
- Doctor surface for events directory + usage + live-transport status.
- README + first-run privacy disclosure.
- Documented on-disk layout + NDJSON / SSE wire format, versioned schema.
- Per-live-subscriber backpressure with overflow eviction.
- Disk-full / IOError handling that never blocks the agent.
- Unit + integration tests covering: file round-trip, on-disk redaction,
  retention, opt-out, registration, live redaction, overflow, auth, doctor,
  and full-turn streaming with both file and live subscribers attached.

## Exit Criteria

- After any `fcli "<request>"` (no flags), a complete redacted NDJSON event
  log exists at `<events_dir>/<session_id>.ndjson` with a matching index row.
- A future GUI / third-party app can open past session files and render
  graphs / tables without ever having attached during the run.
- Retention caps prune oldest sessions automatically.
- A separate process can subscribe to `fcli --monitor-socket` over Unix
  socket and receive the full redacted event stream of a live turn; the
  bytes match the on-disk file.
- The HTTP/SSE transport enforces token auth and binds only to localhost.
- A slow live subscriber is evicted without stalling the agent or the file
  writer.
- Disk-full on the writer logs a warning, marks the session
  `status=write_truncated`, and never blocks the agent.
- `--no-monitor` / `FOUNDATION_MONITOR=0` cleanly disables both persistence
  and live transports.
- Stage 01 live UX still works identically regardless of persistence /
  live-transport state.
- v3 trace store (SQLite) and history DB are **untouched**; the NDJSON log
  is a separate, GUI-friendly surface.
- Suite green; ruff/mypy clean.

## Out of Scope

- Modifying the SQLite trace store schema or moving trace data into NDJSON.
  The trace store stays as-is.
- Live replay / catch-up for late subscribers (they read the on-disk file
  for history).
- Remote (non-localhost) network exposure.
- Mutating agent state from a subscriber or file reader.
- Discovery / mDNS / zeroconf — clients open a known directory or connect to
  a known path / port.
- Cross-machine sync of the events directory.

## Handoff

Once stage 02 ships, downstream consumers come in two flavors:

- **Historical / GUI tools.** Open `<events_dir>/<session_id>.ndjson` (or
  scan `sessions.jsonl` to enumerate) and render graphs / tables / timelines
  from past sessions. No live connection required.
- **Live tools.** Connect to a running fcli over Unix socket or HTTP/SSE
  (when enabled) for push-style updates. They can also tail the on-disk
  file for the same data.

If a future stage wants live replay / catch-up for late subscribers, it
plugs another transport into the same fan-out using either the on-disk
NDJSON file or the trace store as a source.
