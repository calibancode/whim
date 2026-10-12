# Compaction

Source: `<package>/compaction.py` (lifecycle in its opening docstring); size estimates
in `estimate()`; the checkpoint prompt and sections near `SECTIONS`.

1. When the working context passes `compaction.trigger_tokens`, you get a maintenance
   wake at the next idle moment, to journal and curate MEMORY.md / USER.md.
2. A smaller model (`compaction.model`) then writes a checkpoint of older context.
3. One transaction starts a new context epoch: a fresh system prompt with your current
   files, the checkpoint, and about `compaction.tail_tokens` of recent conversation kept
   verbatim. Nothing is deleted: history.search still reaches everything.

At `force_tokens` compaction goes ahead of waiting input. Failures back off; the hard
ceiling is `context_window_tokens - reserve_tokens`, above which nothing is sent until
compaction succeeds. Settings: host_config get compaction.
