# Architecture

One daemon (`<package>/server.py`) hosts one character. Everything durable lives in a
SQLite ledger (`<package>/ledger.py`): an append-only event log plus state tables
(inputs, wakes, runs, operations, context rows, notes...).

- Harness (`harness.py`): admission and the single serialized worker. Only one wake runs
  at a time; human input first, then recovery, then other wakes (cron, heartbeat).
- Run loop (`run.py`): one wake = provider requests until you call turn_finish. Every
  fact the next step depends on is committed first. Tool calls are recorded as intent →
  dispatched → result.
- Context (`context.py`): what the model sees: a system prompt (instructions + your
  files as of the context epoch), then stored messages verbatim. Changed character files
  are shown as host blocks rather than rewriting the prompt.
- Tools (`tools/registry.py`, `tools/builtin.py`, `tools/host.py`, `shell.py`);
  plugins (`plugins.py`) add more under bubblewrap (`sandbox.py`).
- Scheduling (`scheduler.py`, `heartbeat.py`, `cron.py`), journal (`journal.py`),
  questions (`questions.py`), permissions (`authz.py`, `access.py`).
- Settings (`config.py`, `settings.py`), restarts (`restart.py`, `service.py`).
- Client protocol: `docs/client_protocol.md` in the implementation folder.
