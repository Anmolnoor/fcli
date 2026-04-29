# Foundation CLI Monitor Protocol

Foundation CLI publishes a redacted event stream describing every agent turn.
The same event format is used for two surfaces:

- **Persistent NDJSON event log** — written to disk for every session by
  default. A future GUI or analyzer can open past session files without
  attaching during the run.
- **Live transports** *(opt-in)* — Unix domain socket and localhost
  HTTP/SSE. Push-style consumers can subscribe to a running fcli instead of
  tailing the on-disk file.

Both surfaces are **read-only**. Subscribers observe; they cannot steer the
agent.

## On-disk layout

```
${XDG_STATE_HOME:-~/.local/state}/foundation/events/
├── sessions.jsonl                 # append-only index, one session per line
├── <session_id>.ndjson            # one redacted event per line
├── <session_id>.ndjson
└── ...
```

- The events directory is created with mode `0700` and per-file mode `0600`.
  Ownership is the running user; no other user on the machine can read it.
- Override the directory with `--events-dir <path>` or
  `monitor.events_dir = "<path>"` in `config.toml`.
- Disable persistence entirely with `--no-monitor` or
  `FOUNDATION_MONITOR=0`.

### `sessions.jsonl` schema (one JSON object per line)

```json
{
  "schema_version": "1",
  "session_id": "sess-7d3ccb33",
  "request_id": "req-d793b24a",
  "started_at": "2026-04-29T02:21:36Z",
  "ended_at": "2026-04-29T02:22:11Z",
  "request_summary": "fix the failing test",
  "status": "completed",
  "file_path": "/abs/path/to/sess-7d3ccb33.ndjson"
}
```

`status` is one of:

| Value | Meaning |
|---|---|
| `completed` | session_end fired with an OK status |
| `completed_inconclusive` | session_end fired but verification didn't pass |
| `failed` | session_end fired with a fatal status |
| `interrupted` | process exited before session_end (atexit / SIGINT / SIGTERM) |
| `write_truncated` | disk-full or IOError mid-session; the file is incomplete |

### Per-session NDJSON file

One envelope per line, UTF-8, `\n` terminated:

```json
{"event_schema_version":"1","event":"session_start","ts":"2026-04-29T02:21:36Z","request_id":"r","session_id":"s","payload":{}}
{"event_schema_version":"1","event":"iteration_started","ts":"...","request_id":"r","session_id":"s","payload":{"iteration":1}}
{"event_schema_version":"1","event":"tool_call_started","ts":"...","request_id":"r","session_id":"s","payload":{"action_id":"a1","tool":"foundation.file.read"}}
{"event_schema_version":"1","event":"tool_call_finished","ts":"...","request_id":"r","session_id":"s","payload":{"action_id":"a1","tool":"foundation.file.read"}}
{"event_schema_version":"1","event":"session_end","ts":"...","request_id":"r","session_id":"s","payload":{"status":"completed"}}
```

`request_id` and `session_id` are always promoted to top-level fields so
consumers can index without parsing `payload`. The originating event-name
constants are exported from `foundation.observability` and listed in the
section below.

The `user_request` event fires before `session_start` (the orchestrator
emits the user's prompt before opening a session). When the writer flushes
that buffered event into the per-session file it back-fills `session_id` so
the file is consistent; the live transport sees the same envelope with
`session_id=null` at publish time.

## Wire format

`event_schema_version` is bumped only on a breaking change. Additive fields
keep the version unchanged.

```json
{
  "event_schema_version": "1",
  "event": "<name>",
  "ts": "<RFC 3339 UTC>",
  "request_id": "<string|null>",
  "session_id": "<string|null>",
  "payload": { /* event-specific */ }
}
```

Live HTTP/SSE wraps each envelope in a `data:` frame:

```
data: {"event_schema_version":"1","event":"session_start", ... }

```

(One blank line terminates the frame, per the SSE spec.)

## Event vocabulary

The 22 `EVENT_*` constants exported from `foundation.observability` define
the alphabet. Stable as of schema version `1`:

| Event | When |
|---|---|
| `session_start` | Once per turn, after request normalization. |
| `session_end` | Once per turn, with terminal `status`. |
| `user_request` | The user prompt (text already redacted). |
| `iteration_started` / `iteration_completed` | Each replan-loop iteration. |
| `plan_generation_started` / `plan_generation_finished` / `plan_generation_failed` | Planner round-trip. |
| `provider_call_started` / `provider_call_finished` / `provider_call_retry` / `provider_call_failed` | Underlying LLM I/O. |
| `tool_call_started` / `tool_call_finished` / `tool_call_failed` | Per-action capability dispatch. |
| `tool_execution_started` / `tool_execution_finished` / `tool_execution_failed` | Per-action runtime span. |
| `shell_execution_started` / `shell_execution_finished` / `shell_execution_failed` | Shell-runtime spans. |
| `approval_requested` / `approval_resolved` | Approval prompts. |
| `exception` | Caught failure with redacted detail. |
| `retry` | Transient retry loop ticks. |

Stage 02 also emits one synthetic event over live transports only:

| Event | When |
|---|---|
| `subscriber_overflow` | A subscriber's bounded queue overflowed; the subscriber is being evicted. |

## Live transports (opt-in)

| Transport | Flag | Default path / port | Auth |
|---|---|---|---|
| Unix socket | `--monitor-socket[=<path>]` | `${XDG_RUNTIME_DIR:-$TMPDIR}/foundation/<pid>.sock`, mode `0600`, parent dir `0700` | filesystem permissions only |
| Local HTTP/SSE | `--monitor-http=<port>` | `127.0.0.1:<port>` | `Authorization: Bearer <token>` |

Environment-variable equivalents: `FOUNDATION_MONITOR_SOCKET=1`,
`FOUNDATION_MONITOR_HTTP=<port>`.

The HTTP transport refuses to bind anywhere other than `127.0.0.1` /
`localhost`. There is no remote-exposure path; operators who want remote
access put their own proxy in front.

When `--monitor-http` is set, fcli prints the bearer token to stdout once
on startup. The token rotates per-process unless `monitor.auth_token` is
configured explicitly. Tokens are never written to the event log.

### Endpoints

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/events` | Open SSE stream (requires `Authorization: Bearer <token>`). |
| `OPTIONS` | `/events` | Return `{"event_schema_version":"1","endpoint":"/events","method":"GET","auth":"Bearer","content_type":"text/event-stream"}`. |
| any | other | `404` / `405`. |

## Backpressure

Each live subscriber owns a bounded queue (default 1024 events). On
overflow:

1. A `subscriber_overflow` envelope is enqueued (force).
2. The subscriber's connection is closed.
3. Other subscribers and the file writer are unaffected.

The file writer uses unbuffered writes to disk; an `OSError` (disk full,
EIO, etc.) marks the session `write_truncated` in the index and drops
further writes for that session without ever blocking the agent.

## Auth model

| Surface | Auth |
|---|---|
| On-disk NDJSON | filesystem (`0600` file, `0700` dir, owner-only). No in-band token. |
| Unix socket (live) | filesystem (`0600` socket, `0700` parent dir, owner-only). |
| Local HTTP/SSE (live) | `Authorization: Bearer <token>`. Token printed on stdout at startup; rotates per-process; never logged. |

## Lifecycle

- File created on `session_start`, closed on `session_end`. Index row
  appended on close.
- `atexit` + `SIGTERM` / `SIGINT` handlers close any in-progress session
  with `status=interrupted` so partial NDJSON files always have a matching
  index row.
- Retention sweep runs after every `session_end`. The configured caps
  (`monitor.retention.max_sessions`, `monitor.retention.max_bytes`) prune
  oldest sessions first; the index is rewritten atomically (`.tmp` +
  `rename`). The newest session is always preserved even if it exceeds the
  byte cap on its own.

## Example clients

### Python — tail the latest session

```python
import json
import os
import time
from pathlib import Path

events_dir = Path(
    os.environ.get("XDG_STATE_HOME") or "~/.local/state"
).expanduser() / "foundation" / "events"

with (events_dir / "sessions.jsonl").open() as index:
    sessions = [json.loads(line) for line in index if line.strip()]
latest = Path(sessions[-1]["file_path"])

with latest.open() as f:
    f.seek(0, os.SEEK_END)
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.1)
            continue
        envelope = json.loads(line)
        print(envelope["event"], envelope["payload"])
```

### Python — live Unix-socket subscriber

```python
import json
import socket

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/run/user/1000/foundation/12345.sock")
buf = b""
while True:
    chunk = sock.recv(4096)
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, _, buf = buf.partition(b"\n")
        envelope = json.loads(line)
        print(envelope["event"], envelope["payload"])
```

### Node — live HTTP/SSE subscriber

```js
import { EventSource } from "eventsource";

const es = new EventSource("http://127.0.0.1:8765/events", {
  headers: { Authorization: `Bearer ${process.env.FCLI_MONITOR_TOKEN}` },
});

es.onmessage = (msg) => {
  const env = JSON.parse(msg.data);
  console.log(env.event, env.payload);
};
```

### Node — file-tail consumer

```js
import fs from "node:fs";
import readline from "node:readline";

const stream = fs.createReadStream(process.argv[2], { encoding: "utf-8" });
const rl = readline.createInterface({ input: stream });
for await (const line of rl) {
  if (!line.trim()) continue;
  const env = JSON.parse(line);
  console.log(env.event, env.payload);
}
```

## Schema versioning policy

| Change | Bump | Compat |
|---|---|---|
| Add a new event name | no | additive |
| Add a new field inside `payload` | no | additive |
| Rename / drop / re-type any existing field | yes (`"2"`, `"3"`, …) | breaking |
| Change envelope shape | yes | breaking |

Consumers should ignore unknown event names and unknown payload keys when
the major schema version matches.
