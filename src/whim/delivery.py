"""Explicit outbound delivery: intent is durable before dispatch; outcome is a receipt.

Destinations:
- `human` is the designated human's direct interface. It is ledger-backed: committing
  the delivery *is* delivering it (frontends read it from the event stream), so its
  intent and settlement are one transaction and can never be uncertain.
- External destinations (adapters; only a test fixture in Milestone 1) dispatch after
  the intent commits. A crash between dispatch and receipt leaves the delivery
  `uncertain`, and it is never automatically re-sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from whim.ledger import Ledger, dumps, new_id

if TYPE_CHECKING:
    from whim.run import RunState

HUMAN = "human"
REPLY_TO_SOURCE = "reply_to_source"


class DeliveryFailed(Exception):
    """The destination definitively rejected the delivery (nothing was sent)."""


class Destination(Protocol):
    name: str
    description: str

    async def deliver(self, delivery_id: str, content: str, reply_to: str | None, target: str | None = None) -> dict:
        """`target` is the part after "name:" (e.g. a room), when the character gave one."""
        ...


class Deliveries:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.external: dict[str, Destination] = {}

    def add_destination(self, dest: Destination) -> None:
        if dest.name in (HUMAN, REPLY_TO_SOURCE) or dest.name in self.external:
            raise ValueError(f"destination {dest.name!r} already exists")
        self.external[dest.name] = dest

    def names(self) -> list[str]:
        return [HUMAN, REPLY_TO_SOURCE, *sorted(self.external)]

    def resolve(self, state: "RunState", destination: str) -> str:
        if destination == REPLY_TO_SOURCE:
            if state.reply_destination is None:
                raise LookupError("this wake has no message source to reply to")
            return state.reply_destination
        if destination == HUMAN or destination in self.external:
            return destination
        name, _, target = destination.partition(":")
        if target and name in self.external:  # "<destination>:<target>", e.g. an adapter's room
            return destination
        raise LookupError(f"unknown destination {destination!r}; available: {', '.join(self.names())}")

    async def send(
        self, state: "RunState", operation_id: str, destination: str, content: str, reply_to: str | None,
        *, question_id: str | None = None,
    ) -> dict:
        """Returns a receipt dict with `delivery_id` and `status`. `question_id` marks the
        delivery that carries a human.ask question (published, for correlation)."""
        target = self.resolve(state, destination)
        delivery_id = new_id("dl")
        led = self.ledger
        requested = {
            "delivery_id": delivery_id,
            "operation_id": operation_id,
            "destination": target,
            "requested_as": destination,
            "content": content,
            "reply_to": reply_to,
        }
        if question_id:
            requested["question_id"] = question_id
        if target == HUMAN:
            with led.tx():
                led.x(
                    "INSERT INTO deliveries(id, run_id, operation_id, destination, content, reply_to, status, receipt) "
                    "VALUES(?, ?, ?, ?, ?, ?, 'sent', ?)",
                    delivery_id, state.run_id, operation_id, target, content, reply_to,
                    dumps({"ledger": True}),
                )
                led.append("delivery.requested", requested, run_id=state.run_id, actor="character")
                self._link_question(question_id, delivery_id)
                led.append(
                    "delivery.settled",
                    {"delivery_id": delivery_id, "destination": target, "status": "sent",
                     "content": content, "reply_to": reply_to,
                     **({"question_id": question_id} if question_id else {})},
                    run_id=state.run_id,
                    audience="human",
                )
            return {"delivery_id": delivery_id, "status": "sent", "destination": target}

        name, _, sub = target.partition(":")
        dest = self.external[name]
        with led.tx():
            led.x(
                "INSERT INTO deliveries(id, run_id, operation_id, destination, content, reply_to, status) "
                "VALUES(?, ?, ?, ?, ?, ?, 'requested')",
                delivery_id, state.run_id, operation_id, target, content, reply_to,
            )
            led.append("delivery.requested", requested, run_id=state.run_id, actor="character")
            self._link_question(question_id, delivery_id)
        await state.harness.fault("delivery.before_dispatch")
        try:
            receipt = await dest.deliver(delivery_id, content, reply_to, target=sub or None)
            status = "sent"
        except DeliveryFailed as e:
            receipt, status = {"error": str(e)}, "failed"
        except Exception as e:  # noqa: BLE001 - unknown outcome: never assume either way
            receipt, status = {"error": f"{type(e).__name__}: {e}"}, "uncertain"
        await state.harness.fault("delivery.before_receipt")
        self.settle(delivery_id, status, receipt, run_id=state.run_id)
        return {"delivery_id": delivery_id, "status": status, "destination": target, "receipt": receipt}

    def _link_question(self, question_id: str | None, delivery_id: str) -> None:
        """Record which delivery carries a question in the delivery's own transaction, so
        the question row and the published events can never disagree after a crash."""
        if question_id:
            self.ledger.x("UPDATE questions SET delivery_id = ? WHERE id = ?", delivery_id, question_id)

    def settle(self, delivery_id: str, status: str, receipt: dict, *, run_id: str | None) -> None:
        with self.ledger.tx():
            self.ledger.x(
                "UPDATE deliveries SET status = ?, receipt = ? WHERE id = ?", status, dumps(receipt), delivery_id
            )
            row = self.ledger.q1("SELECT destination FROM deliveries WHERE id = ?", delivery_id)
            self.ledger.append(
                "delivery.settled",
                {"delivery_id": delivery_id, "destination": row["destination"], "status": status, "receipt": receipt},
                run_id=run_id,
            )
