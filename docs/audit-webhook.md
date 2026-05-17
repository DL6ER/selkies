# Audit Webhook

Selkies can post structured JSON events to an operator-configured webhook
for two classes of session traffic that may carry sensitive content:

* clipboard transfers in either direction,
* file uploads from the client browser to the server-side desktop.

The audit channel is **opt-in** and disabled by default. When the URL is
empty - the default - the audit emitter is a no-op and adds no overhead.

## Goals

* Give deployments that need a tamper-evident I/O log a way to capture
  it without forking Selkies or patching its internals.
* Keep payload contents out of the audit channel - we only ship metadata
  (event type, byte size, mime type, timestamp).
* Stay best-effort: a slow or down collector must never stall the
  streaming pipeline. Failures are logged and discarded.

## Configuration

Three settings, all parsed via the standard Selkies settings layer (CLI
flag, `SELKIES_*` env var, legacy env var or default):

| Setting                  | Default | Description                                  |
|--------------------------|---------|----------------------------------------------|
| `audit_webhook_url`      | `""`    | HTTPS URL the audit events are POSTed to. Empty disables. |
| `audit_webhook_token`    | `""`    | Optional Bearer token. Empty omits the `Authorization` header. |
| `audit_webhook_timeout`  | `"2.0"` | Per-request timeout in seconds (float-as-string). Lower bound 0.1s. |

Example:

```
SELKIES_AUDIT_WEBHOOK_URL=https://audit.example.org/selkies
SELKIES_AUDIT_WEBHOOK_TOKEN=eyJhbGciOi...
SELKIES_AUDIT_WEBHOOK_TIMEOUT=2.5
```

## Event Schema

Every event is a flat JSON object posted with `Content-Type: application/json`.

Common fields on all events:

| Field      | Type     | Description                                                    |
|------------|----------|----------------------------------------------------------------|
| `event`    | string   | Event identifier (see table below). Stable across releases.    |
| `ts`       | string   | RFC 3339 UTC timestamp of when the event was emitted.          |

### Clipboard events

| Event                | Extra fields                            | Direction (implicit)        |
|----------------------|-----------------------------------------|-----------------------------|
| `clipboard.send`     | `mime_type`, `size_bytes`               | server desktop -> client    |
| `clipboard.receive`  | `mime_type`, `size_bytes`, `multipart`  | client -> server desktop    |

`multipart=true` indicates that the inbound transfer used the chunked
clipboard protocol; `false` is a single-frame paste.

### File upload events (client -> server desktop)

| Event                  | Extra fields                                  |
|------------------------|-----------------------------------------------|
| `file.upload.start`    | `filename`, `announced_size_bytes`            |
| `file.upload.end`      | `filename`, `size_bytes`                      |
| `file.upload.error`    | `filename`, `error`                           |

`announced_size_bytes` is the size the client claimed at the start of
the upload. `size_bytes` on `file.upload.end` is the actual size as
written to disk (read via `os.path.getsize`) so the operator can detect
truncation. `-1` signals a size that could not be determined.

`filename` on `upload.start` is the client-supplied relative path as
received. On `upload.end` and `upload.error` it is the basename only.
Treat both as untrusted user input on the collector side.

Selkies has two streaming-mode upload code paths (WebRTC datachannel
in `input_handler.py` and WebSocket in `selkies.py`). Both emit the
same events with the same schema, so collectors do not need to care
which mode the session is using.

Note: this revision of the patch does not yet emit a `file.download`
event. The current Selkies static-file path for server-to-client
downloads does not exist in this code base; once it is reintroduced
upstream a download event can be added on the same audit channel.

### Example payload

```json
{
  "event": "clipboard.send",
  "ts": "2026-05-18T14:32:11.482000+00:00",
  "mime_type": "text/plain",
  "size_bytes": 423
}
```

## Delivery Semantics

* Fire-and-forget. The originating coroutine returns immediately; the
  POST runs on the same event loop as a background task.
* Best-effort. A single `aiohttp.ClientSession` is reused across events.
  Timeouts, DNS errors, connection refusals and HTTP `>= 400` responses
  are logged at WARN level and discarded.
* No retry. The audit channel does not buffer or retry. If a collector
  goes down, events emitted during that window are lost. Build retention
  on the collector side, not in Selkies.
* No ordering guarantee. Events fire as tasks on the asyncio loop;
  collectors that need strict ordering should rely on the `ts` field.

## What is NOT logged

* Clipboard payload bytes (only their size and mime type).
* File upload payload bytes (only filename and size).
* User identity, session identity, source IP. Selkies does not know
  these; if you need them in the audit trail, terminate the POST at an
  authenticated reverse-proxy or inject identifying headers via the
  collector.

## Collector expectations

* Accept `POST application/json`.
* Respond fast - the timeout is short on purpose.
* HTTP `2xx` is success. `>= 400` is logged on the Selkies side.
* Idempotency is not required; Selkies does not retry.
