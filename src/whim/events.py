"""Canonical durable event catalog (Event Contract v0.1, reconciled for Milestone 1).

Envelope fields are produced by `Ledger.append`. `seq` is the only ordering key.
"""

DURABLE = frozenset(
    {
        # ingress and admission
        "input.received",  # attributed input accepted and persisted
        "input.pending",  # input arrived while a run was active; waits for a boundary
        "input.admitted",  # input incorporated into a run's model context, exactly once
        "wake.queued",  # host accepted a non-input wake (manual/host, recovery)
        "wake.cancelled",  # queued wake made redundant (e.g. recovery note carried by another run)
        # run lifecycle
        "run.started",
        "run.finished",  # only after an accepted turn.finish
        "run.interrupted",  # crash/unclean shutdown, found by startup reconciliation
        "run.paused",  # host limit reached (requests, tools, time, stalled)
        "run.failed",  # provider or host error
        # model activity
        "provider.request.finished",  # one provider request; carries usage incl. cache hit/miss
        "message.completed",  # character-authored text; NOT a delivery
        "context.note",  # host-attributed context added (continuation, file change, recovery)
        # tools and effects
        "tool.started",  # intent persisted before dispatch
        "tool.finished",  # terminal status: ok | error | not_dispatched | uncertain | interrupted
        "delivery.requested",
        "delivery.settled",  # sent | failed | uncertain
        # heartbeats
        "schedule.armed",  # next heartbeat due time set (first time, after onboarding)
        "schedule.due",  # a heartbeat became due; one wake queued (later misses coalesce into it)
        "schedule.fired",  # that wake was admitted as a run; the schedule re-anchors here
        "schedule.coalesced",  # a waiting scheduled wake absorbed further missed occurrences
        "cues.changed",  # standing cues edited (revisioned, audited)
        "jobs.changed",  # scheduled jobs edited (revisioned, audited)
        # compaction
        "compaction.started",  # working context passed the trigger; maintenance wake queued
        "memory.checkpointed",  # the character's maintenance run finished
        "compaction.summarized",  # validated checkpoint stored (not yet in use)
        "context.compacted",  # new context epoch committed: checkpoint + verbatim tail
        "compaction.failed",
        "compaction.cancelled",  # the designated human stopped its maintenance run; not retried
        # capabilities and permissions
        "catalog.changed",  # the set of tools offered to the model changed (new generation)
        "plugin.installed",
        "plugin.validated",
        "plugin.failed",
        "plugin.activated",
        "plugin.deactivated",
        "approval.requested",  # a concrete permission request for the designated human
        "approval.decided",  # their decision, via the authenticated control path
        "grant.revoked",
        # host controls and questions
        "control.requested",  # the designated human stopped, interrupted or steered a run
        "question.created",
        "question.answered",
        "question.cancelled",
        "question.expired",
        # first meeting
        "bootstrap.started",  # BOOTSTRAP.md shown for the first time
        "bootstrap.completed",  # durable completion; the file is then retired
        # recovery and host health
        "recovery.pending",
        "host.started",
        "host.warning",
    }
)

# Server-enforced visibility classes. Milestone 1 has exactly one authenticated
# principal, who may see both; the field exists so later subscribers can be filtered.
AUDIENCES = frozenset({"operator", "human"})
