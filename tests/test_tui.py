"""The terminal UI's promises: only deliveries reach the conversation, a message is sent
exactly once, and the reader keeps their place while the terminal is resized."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from conftest import Clock, complete_bootstrap, events

from whim.provider.fake import ScriptedProvider, call, finish
from whim.tui.protocol import Outbox, Store
from whim.tui.state import Message, Model, Segment


async def test_conversation_holds_deliveries_not_private_text_or_quiet_heartbeats(rig):
    clock = Clock()
    h = await rig.start(ScriptedProvider([
        call("message_send", authored="thinking out loud", destination="reply_to_source", content="hi there"),
        finish(),
        call("heartbeat_manage", operations=[]),  # a heartbeat that only looks around
        finish(),
        call("journal_write", entry="a quiet hour"),  # one that does something and speaks
        call("message_send", destination="human", content="thought of you"),
        finish(),
    ]), clock=clock)
    complete_bootstrap(h)
    h.scheduler.tick()
    model = Model()
    model.add_local("cmid-1", "hello")  # shown at once, before whim has confirmed it
    h.submit_input("hello", source_message_id="cmid-1")
    await h.wait_idle()
    for _ in range(2):
        clock.advance(60)
        h.scheduler.tick()
        await h.wait_idle()
    for ev in events(h):
        model.apply(ev)

    shown = [i for i in model.items if i.visible]
    assert [(m.role, m.text, m.origin, m.local) for m in shown if isinstance(m, Message)] == [
        ("human", "hello", None, None),  # the local message, confirmed in place: not a second copy
        ("character", "hi there", None, None),
        ("character", "thought of you", "during a heartbeat", None),
    ]
    segments = [s for s in shown if isinstance(s, Segment)]
    assert [s.notes for s in segments] == [["thinking out loud"], []]  # private text: a note, never a message
    assert [c.tool for c in segments[1].shown_calls()] == ["journal.write"]
    quiet = [r for r in model.runs.values() if r.kind == "heartbeat" and r.quiet]
    assert len(quiet) == 1 and quiet[0].segment is not None and not quiet[0].segment.visible


class FlakyDaemon:
    """Answers POSTs from a script: an exception (the connection dropped after the
    request went out) or a (status, body) reply. Records every body it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent: list[dict] = []

    async def try_post(self, path, body):
        self.sent.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


async def _drain(outbox: Outbox, client, until) -> None:
    task = asyncio.create_task(outbox.run(client))
    try:
        for _ in range(200):
            if until():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("outbox didn't settle")
    finally:
        task.cancel()


async def test_an_uncertain_send_is_retried_with_the_same_id_even_after_quitting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    updates: list[str] = []
    first = Outbox(Store(tmp_path / "home"), lambda item, state, note: updates.append(state))
    first.add("/v1/input", {"text": "hi"}, text="hi")
    dropped = FlakyDaemon(aiohttp.ServerDisconnectedError())
    await _drain(first, dropped, lambda: updates == ["unconfirmed"])
    # The TUI quits here. The next session finds the message and sends it again.
    later = Outbox(Store(tmp_path / "home"), lambda item, state, note: updates.append(state))
    answers = FlakyDaemon((200, {"id": "in_1", "duplicate": True}))
    await _drain(later, answers, lambda: not later.items)
    assert updates == ["unconfirmed", "accepted"]
    assert dropped.sent[0]["client_message_id"] == answers.sent[0]["client_message_id"]
    assert Store(tmp_path / "home").data["outbox"] == []


async def test_only_a_refusal_drops_a_message(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    updates: list[tuple[str, str]] = []
    outbox = Outbox(Store(tmp_path / "home"), lambda item, state, note: updates.append((state, note)))
    outbox.add("/v1/input", {"text": "x" * 10}, text="x")
    daemon = FlakyDaemon(aiohttp.ClientConnectorError(None, OSError("no socket")),  # never reached whim
                         (400, {"error": "text is too long"}))
    await _drain(outbox, daemon, lambda: not outbox.items)
    assert updates == [("unsent", ""), ("rejected", "text is too long")]


# -- the transcript widget ---------------------------------------------------------------

textual = pytest.importorskip("textual")


def _model(n: int) -> Model:
    model = Model()
    for i in range(n):
        role = "human" if i % 2 else "character"
        ev = ({"type": "input.received", "data": {"input_id": f"in_{i}", "text": f"note {i} " + "words " * (i % 9 * 6),
                                                   "sender": {"role": "designated_human"}}}
              if role == "human" else
              {"type": "delivery.settled", "run_id": "run_1",
               "data": {"delivery_id": f"dl_{i}", "destination": "human", "status": "sent",
                        "content": f"reply {i}\n\n" + "lorem ipsum dolor " * (i % 7 * 8)}})
        model.apply({"at": "2026-10-11T10:00:00Z", "seq": i + 1, **ev})
    model.take_changes()
    return model


async def _until(pilot, condition) -> bool:
    """The view settles a layout pass or two after a change: wait for it (bounded)."""
    for _ in range(50):
        if condition():
            return True
        await pilot.pause(0.02)
    return condition()


def _top(transcript):
    """(item key, rows of it above the view) for the item at the top of the view."""
    from whim.tui.transcript import ItemView

    for v in transcript.children:
        if isinstance(v, ItemView) and v.virtual_region.bottom > transcript.scroll_y:
            return v.item.key, round(transcript.scroll_y - v.virtual_region.y)


async def test_resizing_keeps_the_reader_on_the_same_message_and_follows_only_at_the_end():
    from textual.app import App

    from whim.tui.transcript import Transcript

    model = _model(60)

    class Host(App):
        def compose(self):
            yield Transcript(model, lambda: "whim")

    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        t = app.query_one(Transcript)
        t.load()
        assert await _until(pilot, lambda: t.following and t.scroll_y == t.max_scroll_y > 0)

        t.scroll_to(y=t.max_scroll_y // 2, animate=False, immediate=True)  # reading back
        await pilot.pause()
        reading = _top(t)
        for width in (44, 140, 61, 80, 100):
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            assert _top(t)[0] == reading[0]  # the same message stays at the top, however it rewraps
        assert _top(t) == reading  # and back at the first size, the very same line
        assert not t.following

        model.apply({"type": "delivery.settled", "run_id": "run_1", "at": "2026-10-11T10:05:00Z",
                     "data": {"delivery_id": "dl_new", "destination": "human", "status": "sent", "content": "new"}})
        t.sync(model.take_changes()[0])
        await pilot.pause()
        assert _top(t) == reading and t.unseen == 1  # a new message never pulls the reader away

        t.jump_to_end()
        await pilot.pause()
        model.apply({"type": "delivery.settled", "run_id": "run_1", "at": "2026-10-11T10:06:00Z",
                     "data": {"delivery_id": "dl_newer", "destination": "human", "status": "sent", "content": "x"}})
        before = t.max_scroll_y
        t.sync(model.take_changes()[0])
        assert await _until(pilot, lambda: t.max_scroll_y > before and t.scroll_y == t.max_scroll_y)  # it follows


async def test_messages_can_be_selected_and_copy_as_written():
    from textual.app import App
    from textual.selection import SELECT_ALL

    from whim.tui.transcript import MessageView, Transcript

    paragraph = "a long first paragraph that has to wrap several times in a narrow terminal window"
    model = Model()
    model.apply({"type": "delivery.settled", "run_id": "run_1", "at": "2026-10-11T10:00:00Z", "seq": 1,
                 "data": {"delivery_id": "dl_1", "destination": "human", "status": "sent",
                          "content": f"{paragraph}\n\n- **one**\n- two"}})
    model.take_changes()

    class Host(App):
        def compose(self):
            yield Transcript(model, lambda: "whim")

    app = Host()
    async with app.run_test(size=(40, 20)) as pilot:
        app.query_one(Transcript).load()
        await pilot.pause()
        bubble = app.query_one(MessageView).query_one(".bubble")
        hit, offset = app.screen.get_widget_and_offset_at(bubble.region.x + 4, bubble.region.y + 2)
        assert hit is bubble and offset is not None  # the mouse can select inside the text
        app.screen.selections = {bubble: SELECT_ALL}
        copied = app.screen.get_selected_text()
        assert paragraph in copied  # wrapped on screen, one line when copied
        assert copied.endswith("• one\n• two")
