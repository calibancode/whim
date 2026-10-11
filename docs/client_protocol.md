# whim client protocol (v1)

This is the wire interface a client uses to talk to a running whim daemon, as implemented.
It is enough to write a client without importing any whim Python module;
`tests/test_protocol_consumer.py` is such a client, using only the Python standard library.

## 1. Transport and authorization

- **Socket.** HTTP/1.1 over a Unix domain socket at `<home>/run/whim.sock`. The `run/`
  directory is mode `0700` and the socket `0600`. The host name in requests is ignored
  (the CLI uses `http://whim`). There is no TCP listener.
- **Authentication.** For every request the daemon reads the connecting process's uid
  from the kernel (`SO_PEERCRED`). It must equal the configured designated human's uid
  (`allowed_uid`, by default the uid that ran `whim init`). Otherwise the response is
  `403 {"error": "forbidden"}`. There are no tokens or cookies.
- **Principal.** The daemon attaches the designated human's opaque `principal_id` itself.
  Request bodies cannot claim an identity: `POST /v1/input` ignores unknown fields such as
  `origin`, `principal_id` or `sender`. The control, answer and approval bodies reject
  unknown fields with `400`.
- **Entitlements.** Every event has an `audience`: `human` (the designated human's own
  conversation) or `operator` (everything else: private authored text, tools, external
  senders, host state). In v1 the one authenticated principal holds both entitlements.
  Streams filter by audience before anything is sent, both on replay and live. The
  operator-only endpoints check the `operator` entitlement, and the question endpoints
  check `human`. `GET /v1/snapshot`, `POST /v1/input` and `POST /v1/wake` need only an
  authenticated caller.
- **External senders** (adapters, such as a chat platform) never use this socket. They
  enter through host-installed adapter code. Their messages appear as attributed
  `input.received` events (§4.3) and carry no principal, so they can never approve,
  answer or control anything.

## 2. Errors

| Status | Meaning |
| --- | --- |
| `400` | malformed JSON, a body that fails validation, a bad cursor, an unknown AG-UI view, or an approval/grant that is unknown or already decided/revoked |
| `403` | wrong peer uid, or the endpoint needs an entitlement the caller lacks |
| `404` | unknown question id (other unknown paths get aiohttp's plain-text `404`) |
| `409` | the request conflicts with current state: nothing is running (stop/interrupt), or the question is already answered/cancelled/expired |

**Sizes.** Text submitted in an input, steer, answer or wake is limited to 1 MiB of UTF-8.
A larger one gets `400`, and nothing is accepted. Request bodies may be up to 8 MiB,
to allow for JSON escaping. No event's SSE `data:` line exceeds 8 MiB, so clients should
accept lines at least that long; the default line limit of many HTTP libraries is far
smaller. If a client meets a frame it can't read, it should say so and move its cursor
past it rather than stop: replaying would meet the same event again.

Error bodies are JSON: `{"error": "<human-readable message>"}`. The message is meant for
people; clients should branch on the status code.

## 3. Endpoints

### 3.1 Sending input

`POST /v1/input`

```json
{"text": "hello", "client_message_id": "uuid-optional", "when": "next_step"}
```

- `when` is `next_step` (default) or `next_run`.
  - `next_step`: the message enters at the active run's next safe point, or starts a
    new run if the character is idle.
  - `next_run`: the message is held until the current run ends.
- `202` returns `{"id": "in_…", "status": "pending", "seq": <seq of its input.received>, "duplicate": false}`.
- **Idempotency.** `client_message_id` is unique per source and is checked against the
  ledger, so the check survives daemon restarts. Repeating it returns `200` with the
  original record and `"duplicate": true`. It never creates a second message. A client
  that didn't receive a response should retry with the same id.
- **Unknown outcomes.** A client that could not connect knows the request was not
  received. A failure after connecting (a dropped connection, a timeout) leaves the
  outcome unknown. For input, retry with the same `client_message_id` until there is an
  answer. Never resend with a new id: that is how duplicates happen. `whim chat` keeps
  each message and its id until the daemon answers, and reports `rejected` only for an
  HTTP refusal. Controls, answers, wakes and approval decisions act on current state.
  Don't repeat them blindly after an unknown outcome: check `GET /v1/control`,
  `/v1/questions` or the event stream first. That is what `whim chat` and the one-shot
  commands tell the user to do.
- Accepted input is durable before the response is sent. This holds even while the
  character is busy, during compaction, or while the context is full (§6).

`POST /v1/wake` with `{"reason": "manual", "text": ""}` (both optional) queues a host
wake. It returns `202 {"wake_id": "wake_…"}`. This call is **not** idempotent.

### 3.2 Reading state

`GET /v1/snapshot` returns current state plus the cursor it corresponds to:

```json
{"cursor": 812, "character_id": "…", "thread_id": "…", "active_run": "run_…" | null,
 "pending_inputs": 0, "queued_wakes": 0,
 "recent_runs": [{"id", "status", "reason", "started_at", "ended_at", "requests", "tool_calls"}],
 "heartbeat": {"enabled": true, "next_due_at": "…", "cue_revision": 3, "active_cues": 2, …},
 "jobs": [...], "context": {"estimated_tokens", "trigger_tokens", "force_tokens", "hard_limit_tokens",
                            "suppressed_until", "blocked", "in_progress"?, "last"?, …},
 "messages_to_human": [{"id": "dl_…", "run_id", "content", "status", "question_id": "q_…" | null}]}
```

The snapshot reflects every event up to and including `cursor`, and nothing after it.
To follow on from it, stream with `after=<cursor>` (§5). `messages_to_human` holds the
last 20 deliveries to the human, oldest first. `question_id` is set on the delivery that
carried a question.

### 3.3 Event streams

- `GET /v1/events?after=<seq>&follow=0|1` streams native ledger events (§4).
- `GET /v1/agui?view=conversation|activity|inspect&after=<seq>&follow=0|1` streams the
  AG-UI 1.0 projection (§7).

Both default to `after=0&follow=1`. A `Last-Event-ID` header, if present, overrides
`after`. A non-integer cursor gets `400`.

### 3.4 Questions (needs `human`)

- `GET /v1/questions` returns
  `{"pending": [{"id", "text", "choices": [...], "status", "created_at", "expires_at"}]}`.
- `POST /v1/questions/{id}/answer` takes
  `{"choice": 2 | "coffee", "text": "words", "client_message_id": "…"}`. Give at least
  one of `choice` or `text`.
  - `choice` is either a 1-based index or the exact text of a choice (case-insensitive).
  - It returns `200 {"question_id", "input_id", "choice": "<picked>" | null}`.
  - The answer becomes an ordinary message from the human, correlated by
    `question_id` (§4.3).
  - A second answer gets `409`. A client retrying after a lost response should treat
    `409 … already answered` as success.
  - An unknown id gets `404`, and an expired question gets `409`.

### 3.5 Controls (needs `operator`)

- `GET /v1/control` returns `{"active_run": null}` when idle. While a run is active it
  returns `{"active_run", "origin", "started_at", "requests", "tool_calls", "step",
  "stop_requested"}`.
- `POST /v1/control` takes `{"action": "stop" | "interrupt" | "steer", "text"?, "client_message_id"?}`:
  - **`stop`** ends the run at its next safe point. Calls that haven't started are recorded
    as not run. It returns the same body as `GET /v1/control`.
  - **`interrupt`** cancels the run now. Its in-flight work is settled conservatively and
    never retried. It returns `{"interrupting": "run_…"}`.
  - **`steer`** admits `text` right away. A model response still being written is discarded
    and requested again with the message included. It returns the input record. Its
    `client_message_id` deduplicates the message the same way as in §3.1.
  - With nothing running, `stop` and `interrupt` get `409`. A `steer` is still accepted
    and waits as an ordinary message, which starts a run.
  - Stopping or interrupting a memory-maintenance run cancels that compaction
    (`compaction.cancelled`). It is not retried; a new compaction may start after a hold.

### 3.6 Approvals (needs `operator`)

- `GET /v1/approvals` returns `{"pending": [...], "grants": [...], "plugins": [...]}`.
- `POST /v1/approvals/{id}` takes `{"decision": "approve" | "deny", "standing": false}`
  and returns the approval row. A second decision gets `400`.
- `POST /v1/grants/{id}/revoke` returns the grant row. A grant that is unknown or already
  revoked gets `400`.

Decisions are made only through these endpoints. Nothing said in a conversation, and
no AG-UI event, grants anything.

## 4. Native events

### 4.1 Envelope

Every SSE frame with data carries one JSON event:

```json
{"v": 1, "seq": 812, "id": "ev_…", "at": "2026-10-10T19:00:00.123Z", "type": "tool.finished",
 "character_id": "…", "thread_id": "…", "run_id": "run_…", "cause_id": "ev_…",
 "actor": "host" | "character" | "human" | "external", "origin": {…},
 "audience": "operator" | "human", "data": {…}}
```

- `seq` is the only ordering key. It is assigned at commit and strictly increasing. An
  event is published only after its transaction commits, so a rolled-back fact never
  appears.
- `id` is a stable unique event id. `at` is UTC with milliseconds.
- Fields that would be null are omitted: `run_id` for host-level facts, `cause_id`,
  and `origin`.
- `origin` (when present) gives provenance. `kind` is one of `input`, `control`, `host`,
  `heartbeat`, `cron`, `maintenance` or `recovery`, plus `source_kind`, `principal_id`,
  `wake_id`, `input_id`, `job_id`, `compaction_id` and `resumes_run_id` as they apply.
  `principal_id` appears only on the designated human's own actions.
- New event types and new `data` fields may be added. Clients must ignore what they
  don't know.

### 4.2 Event catalog

`data` fields marked `?` are present only when they apply.
**Audience** is `h` (human) or `o` (operator). The **Run** column says whether `run_id` is set.

| Type | Aud. | Run | `data` |
| --- | --- | --- | --- |
| `input.received` | h (direct) / o (external) | no | `input_id, text, source_message_id, via, sender, reply_destination, mode?, question_id?` |
| `input.pending` | o | yes | `input_id`: arrived while this run was active |
| `input.admitted` | h / o, as received | yes | `input_id`: entered this run's model context, exactly once |
| `wake.queued` / `wake.cancelled` | o | no | `wake_id, kind, reason?, compaction_id?` / `wake_id, reason, job_id?, by_run?` |
| `run.started` | o | yes | `resumes_run_id?`; the run's `origin` describes what woke it |
| `run.finished` | o | yes | `reason: "turn_finish"` (only after an accepted `turn_finish`) |
| `run.paused` | o | yes | `reason`: `request_limit`, `time_limit`, `tool_limit`, `stalled`, `context_limit`, `context_full` |
| `run.failed` | o | yes | `reason: provider_error \| host_error, detail` |
| `run.interrupted` | o | yes | `reason`: `operator_stop`, `operator_interrupt`, `unclean_shutdown`; `unresolved?` |
| `provider.request.finished` | o | yes | `provider, model, finish_reason, request_index, tool_calls, catalog_generation, usage` |
| `message.completed` | o | yes | `message_id, content`: text the character wrote. **Not a delivery**; nobody received it |
| `context.note` | o | yes | `kind`: `continuation`, `file_changed`, `host_notes`, `missed_reply_check`, `steer_discarded_request`, …; `count?, note_ids?` |
| `tool.started` | o | yes | `operation_id, call_id, tool, args, effect?, recovered?` |
| `tool.finished` | o | yes | `operation_id, call_id, tool, status, result, reason?, recovered?` |
| `delivery.requested` | o | yes | `delivery_id, operation_id, destination, requested_as, content, reply_to, question_id?` |
| `delivery.settled` | h (to the human) / o | yes | `delivery_id, destination, status`, plus for the human `content, reply_to, question_id?`, otherwise `receipt`; `reason?` |
| `question.created` | h | yes | `question_id, text, choices, expires_at` |
| `question.answered` | h | no | `question_id, input_id, choice` |
| `question.cancelled` / `question.expired` | h | yes / no | `question_id` |
| `approval.requested` | h | yes or no | `approval_id, subject, scopes, summary` |
| `approval.decided` | h | no | `approval_id, decision, subject, scopes, standing` |
| `grant.revoked` | h | no | `grant_id, subject, scope` |
| `control.requested` | o | yes | `action, step?, input_id?, cancelled_model_request?` |
| `schedule.armed` / `schedule.due` / `schedule.coalesced` | o | no | `schedule: heartbeat \| cron, wake_id, due_at, missed, job_id?, …` |
| `schedule.fired` | o | yes | `schedule, wake_id, occurrence_id, due_at, missed, delay_seconds, …` |
| `cues.changed` / `jobs.changed` | o | either | `revision, actor, operations, …` |
| `compaction.started` / `.summarized` / `.failed` / `.cancelled` | o | `.cancelled` only | `compaction_id, …`; `.failed` and `.cancelled` give `next_attempt_not_before` |
| `memory.checkpointed` | o | yes | `compaction_id, run_ids, skipped?` |
| `context.compacted` | o | no | `compaction_id, epoch_from, epoch_to, version, covered_seq, tokens_before, tokens_after, …` |
| `catalog.changed`, `plugin.*` | o | no | tool catalog generations and plugin lifecycle |
| `bootstrap.started` / `bootstrap.completed` | o | yes | first meeting |
| `recovery.pending` | o | no | `note_id, run_ids, unresolved` |
| `host.started` | o | no | `interrupted_runs` |
| `host.warning` | o | either | `kind, message?, …` (kinds below) |

`host.warning` kinds:

- `compaction_failed`, `compaction_summary_failed` and `compaction_insufficient`
- `context_full` and `context_full_cleared` (§6)
- `job_invalid`
- `recovery_suspended`
- `bootstrap_file_missing` and `bootstrap_file_unsafe`

### 4.3 Semantics a client relies on

- **Replies vs private text.** Only `delivery.settled` with `destination: "human"` and
  `status: "sent"` is a message to the human. `message.completed` is the character's
  own text and stays private: show it, if at all, marked as unsent. The character may
  also say nothing at all.
- **Sender attribution.** `input.received.data.sender` is filled in by the host and never
  taken from the message:
  - The designated human appears as
    `{"role": "designated_human", "verified_by": "direct", "label": "human"}`.
  - Someone an adapter vouches for appears as
    `{"role": "external", "verified_by": "adapter:<name>", "label", "id", "name", "channel"}`.
    `id` is that platform's sender id. External input is operator-audience.
  - `reply_destination` is where `reply_to_source` would deliver.
  - `delivery.requested.data` gives the resolved `destination` (e.g. `chat:room`) and
    what the character asked for (`requested_as`).
- **Questions.** `question.created` → `delivery.*` with the same `question_id` (the text
  the human sees) → `question.answered` → `input.received` with that `question_id`.
- **Tool calls.** `tool.started` and `tool.finished` correlate by `operation_id`.
  `call_id` is the provider's id. Every call the model requested gets exactly one
  `tool.started` and one `tool.finished`, including calls that never ran. So the sum of
  `provider.request.finished.data.tool_calls` equals the number of `tool.started`
  events.
  - **Statuses.** `ok`, `error`, `not_dispatched` (did not run), `interrupted` (a
    read-only call cut off; safe to repeat) and `uncertain` (may or may not have taken
    effect; never retried automatically).
  - **The result.** `result` is the outcome the character was shown. Long strings are
    abridged.
- **Recovery.** After a crash, startup reconciliation settles the interrupted run's calls
  and publishes them like live outcomes with `recovered: true` and a `reason`. A call the
  crash stopped before it was recorded gets a `tool.started` (`recovered: true`) too.
  Then `run.interrupted` (with `unresolved`), `recovery.pending` and usually one
  `wake.queued` (`kind: "recovery"`) follow. Everything a client needs is in these events.
- **Run lifecycle.** Each run has exactly one `run.started` and one terminal event:
  `finished`, `paused`, `failed` or `interrupted`. A run is never resumed. A later run
  may name it in `resumes_run_id`.

## 5. Cursors, replay and reconnection

- The cursor is `seq`. Each native frame is `id: <seq>`, `event: <type>`,
  `data: <json>`, then a blank line.
- An event the viewer isn't entitled to is sent as an **id-only frame** (`id: <seq>` and
  a blank line). This advances the cursor without revealing anything. Clients must accept
  frames with no data.
- A stream sends every committed event after `after`, in order and without gaps, then
  (with `follow=1`) live events as they commit. The daemon subscribes before reading the
  backlog, so nothing committed during the switch is lost or repeated. `follow=0` ends the
  response at the end of the backlog.
- **Process before you advance.** Record an event's `seq` as your cursor only after you
  have handled it. Count a frame as received only when its terminating blank line has
  arrived. To reconnect, send the last such seq as `Last-Event-ID` (or `after`).
- **Keepalive.** On a quiet stream a `: keepalive` comment arrives every 15 s. Treat 45 s
  of silence as a dropped connection.
- **Slow clients.** If a client falls 2,000 events behind the live stream, the daemon
  closes its connection rather than buffering without limit. The client reconnects from
  its cursor and receives everything it missed from the ledger. The same applies to
  daemon restarts, since the ledger is durable and the cursor stays valid across restarts.
- **Suggested backoff:** 0.5 s doubling to 5 s. `whim chat` does this, says
  `(lost the connection to whim; reconnecting...)` once per outage and
  `(reconnected; catching up)` when it gets back.
- **Snapshot + stream.** For a quick start, `GET /v1/snapshot` and then stream from its
  `cursor`. Alternatively, replay from `after=0` to build state from events alone.

## 6. When the context is full

If the working context reaches its hard ceiling and compaction can't run, the daemon
admits no new run. Input is still accepted (`202`) and stays pending. The daemon emits
`host.warning` `context_full`, and `GET /v1/snapshot` shows it in `context.blocked`.
After compaction succeeds, `context_full_cleared` follows and the waiting messages are
handled.

## 7. AG-UI projection

`GET /v1/agui` serves an [AG-UI 1.0](https://github.com/ag-ui-protocol/ag-ui) event stream.
The schema is pinned in `src/whim/agui/schema-1.0.json`, and every event the daemon sends
validates against it.

- **Framing.** Frames are `data: <AG-UI event JSON>`. One ledger event may become several
  AG-UI events. Only the last frame of each group carries `id: <seq>`, so a reconnect
  never splits or repeats a group. A ledger event that projects to nothing sends an
  id-only frame.
- **Views:**
  - `conversation` has run lifecycle, the human's messages, and deliveries to the human,
    plus `whim.approval.requested`, `whim.question.created` and `whim.question.cancelled`
    as `CUSTOM` events.
  - `activity` adds private authored text, tool calls and results, other deliveries,
    schedule, cue and job changes, bootstrap, memory checkpoints and plugin activation
    (as `CUSTOM` `whim.<type>`).
  - `inspect` adds every other run-scoped event (usage per request, host notes) for
    operators.
- **Run-scoped only.** AG-UI forbids events outside a run. Host-level facts (wakes,
  schedule due, compaction start, host warnings between runs, question answered/expired)
  are therefore only in the native stream.
- **Run endings.** `run.paused` becomes `RUN_FINISHED` with
  `outcome: {"type": "cancelled"}`. `run.failed` and `run.interrupted` become
  `RUN_ERROR` with `code`.
- **Message metadata** sits under `metadata.whim` on `TEXT_MESSAGE_START`. It is kept
  identically in snapshots:
  - user: `{"kind": "input", "via", "sender", "reply_destination", "question_id"?, "mode"?}`.
    An external sender's label is also the AG-UI `name`.
  - delivered reply: `{"kind": "delivery", "destination": "human", "question_id"?}`
  - private text: `{"kind": "authored", "delivered": false}`. Never render this as a reply.
- **Tool calls.** `toolCallId` is the `operation_id`. Each call is its own parent
  assistant message `<operation_id>.call`, and the result message is
  `<operation_id>.result`. The result `content` is JSON
  `{"status", "result"}`, including for recovered calls.
- **Resuming inside a run.** A stream that resumes after a cursor inside an open run
  starts by restating that run's `RUN_STARTED`, identical to the live one plus
  `metadata.whim.restated: true`. If a tool call was open at the cursor, a complete
  `MESSAGES_SNAPSHOT` for the view follows. Snapshots reconcile by message id: the
  client's message state after snapshot + tail equals what a client that never
  disconnected has.
- **Read-only.** No REASONING events, ever. Connecting never wakes the character.
  Decisions go through §3.5–3.6, not through AG-UI.

## 8. Durability guarantees a client can assume

- Any event a client has seen is committed (SQLite WAL, `synchronous=FULL`) and will be
  there after any crash.
- **Workspace writes.** A `tool.finished` with status `ok` for a file write (or journal
  append, or plugin install) is committed only after the file's data, its directory entry
  and any folders created for it were fsynced. A crash can leave such a change made but
  not yet recorded. Recovery then reports it as `uncertain` (or, for journal entries,
  checks the file), never as done and never by repeating it.
- **Deliveries to the human** are committed in a single transaction; the commit *is*
  the delivery.
- **External deliveries** that crash between dispatch and receipt settle as `uncertain`
  and are not re-sent.
