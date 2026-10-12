"""The conversation transcript: one widget per visible model item, in a scroll container.

Each message is its own widget holding Textual `Content`, so Textual reflows it to the
current width on every resize (there is no pre-wrapped log) and its text can be selected
with the mouse and copied. Scrolling has two states:

- **following**: the view sits at the bottom and stays there as items arrive;
- **reading**: the user moved away. New items never move the view; a counter of unseen
  messages is shown instead.

The reading position is remembered as (widget, fraction of its height) for the item at
the top of the view, and restored after any resize, so the same message stays in view
however the text rewraps. Only the most recent items are mounted at first; older ones
are mounted when the user scrolls to the top. While text in the transcript is selected,
the view stops following, so the selection isn't moved out from under the pointer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.message import Message as TMessage
from textual.widget import Widget
from textual.widgets import Static

from whim.tui import describe
from whim.tui.markup import chat_content, to_content
from whim.tui.state import Day, Item, Message, Model, Notice, Segment

WINDOW = 150  # visible items mounted at first
PAGE = 100  # more mounted each time the top is reached


def stamp(at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    if at.date() == now.date():
        return f"{at:%H:%M}"
    if now - at < timedelta(days=6):
        return f"{at:%a %H:%M}"
    return f"{at:%d %b %H:%M}"


LOCAL_STATE = {
    "sending": ("sending…", "dim"),
    "accepted": ("sent", "dim"),
    "unsent": ("waiting for whim; will send when it's reachable", "$warning"),
    "unconfirmed": ("not confirmed yet; retrying with the same id, so it can't arrive twice", "$warning"),
    "rejected": ("not sent", "$error"),
}


class ItemView(Widget):
    """Base for transcript rows; `item` is the model object it shows."""

    DEFAULT_CSS = "ItemView { height: auto; }"

    def __init__(self, item: Item, ctx: "Transcript"):
        super().__init__()
        self.item = item
        self.ctx = ctx
        self.shown_version = -1

    def update_item(self) -> None:
        if self.item.version != self.shown_version:
            self.shown_version = self.item.version
            self.refresh_content()

    def refresh_content(self) -> None:
        self.refresh(layout=True)


class MessageView(ItemView):
    DEFAULT_CSS = """
    MessageView { layout: vertical; height: auto; margin: 1 0 0 0; }
    MessageView.human { align-horizontal: right; }
    MessageView > .bubble { height: auto; width: auto; }
    MessageView.human > .bubble {
        max-width: 80%; padding: 0 1; background: $primary 18%; border-left: tall $primary 60%;
    }
    MessageView.character > .bubble { width: 1fr; max-width: 92; padding: 0 1; border-left: tall $accent 70%; }
    MessageView.character.autonomous > .bubble { border-left: tall $secondary 70%; }
    MessageView.pending > .bubble { border-left: tall $warning 60%; }
    MessageView.rejected > .bubble { border-left: tall $error 70%; }
    """

    def compose(self):
        yield Static(classes="bubble")

    def on_mount(self) -> None:
        self.update_item()

    def refresh_content(self) -> None:
        m: Message = self.item  # type: ignore[assignment]
        self.set_class(m.role == "human", "human")
        self.set_class(m.role == "character", "character")
        self.set_class(bool(m.origin), "autonomous")
        self.set_class(m.local in ("sending", "unsent", "unconfirmed"), "pending")
        self.set_class(m.local == "rejected", "rejected")
        self.query_one(".bubble", Static).update(self.renderable())

    def renderable(self) -> Content:
        m: Message = self.item  # type: ignore[assignment]
        if m.role == "character":
            parts: list[Content | tuple[str, str] | str] = [(self.ctx.name_of(), "bold"), (f"  {stamp(m.at)}", "dim")]
            if m.origin:
                parts.append((f"  · {m.origin}", "dim italic"))
            parts += ["\n", chat_content(m.text)]
            if self.ctx.model.questions.get(m.question_id or "") is not None:
                parts.append(("\n↳ waiting for your answer · ctrl+o to answer", "italic $warning"))
            return Content.assemble(*parts)
        parts = [("you", "bold"), (f"  {stamp(m.at)}", "dim")]
        if m.mode == "steer":
            parts.append(("  · steer", "dim italic"))
        elif m.mode == "next_run":
            parts.append(("  · for later", "dim italic"))
        if m.question_id:
            parts.append(("  · answer", "dim italic"))
        parts += ["\n", Content(m.text)]
        if m.local in LOCAL_STATE:
            words, style = LOCAL_STATE[m.local]
            if m.local == "rejected" and m.note:
                words += f": {m.note}"
            parts.append((f"\n{words}", f"italic {style}"))
        return Content.assemble(*parts)


class SegmentView(ItemView, can_focus=True):
    """A run's tool activity: one compact line, expandable to arguments and results."""

    DEFAULT_CSS = """
    SegmentView { height: auto; margin: 1 0 0 0; padding: 0 1; color: $text-muted; }
    SegmentView:hover { background: $boost; }
    SegmentView:focus { background: $boost; color: $text; }
    SegmentView.expanded { color: $text; border-left: tall $panel-lighten-2; }
    """

    def on_mount(self) -> None:
        self.set_class(self.expanded, "expanded")
        self.update_item()

    @property
    def expanded(self) -> bool:
        return self.item.key in self.ctx.expanded

    def toggle(self) -> None:
        views = [c for c in self.ctx.children if isinstance(c, ItemView)]
        if self.ctx.following and self not in views[-2:]:
            # Opening an entry higher up: keep it where it is rather than follow the end.
            self.ctx.following = False
            self.ctx._capture(force=True)
        self.ctx.expanded.symmetric_difference_update({self.item.key})
        self.set_class(self.expanded, "expanded")
        self.refresh(layout=True)

    def focus_on_click(self) -> bool:
        return False  # the composer keeps focus; ctrl+↑/↓ focus entries from the keyboard

    def on_click(self, event: events.Click) -> None:
        # A drag that selected text, or a double/triple click, is selection, not a toggle.
        if event.chain == 1 and not self.screen.selections:
            self.toggle()

    def key_enter(self) -> None:
        self.toggle()

    def render(self) -> Content:
        seg: Segment = self.item  # type: ignore[assignment]
        run = seg.run
        calls = seg.shown_calls()
        running = run is not None and run.ended is None and any(c.status is None for c in calls)
        trouble = sum(c.status in describe.TROUBLE for c in calls)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("▾ " if self.expanded else "▸ ", style="dim")
        if run is not None and run.autonomous:
            line.append(f"◇ {describe.origin_label(run.origin)}", style="italic")
            line.append(" · ")
        if running:
            line.append("◌ ", style="yellow")
        words = [c.description for c in calls]
        if not words and seg.notes:
            words = ["wrote a note it didn't send"]
        shown = ", ".join(words[:3]) + (f" (+{len(words) - 3} more)" if len(words) > 3 else "")
        line.append(shown)
        if seg.notes and calls:
            line.append(" · ✎ unsent note", style="dim italic")
        if trouble:
            line.append(f"  ✗ {trouble}", style="bold red")
        line.append(f"  {stamp(seg.at)}", style="dim")
        line.truncate(max(10, self.size.width - 2), overflow="ellipsis")
        if not self.expanded:
            return to_content(line)
        out: list[Text] = [line]
        for c in calls:
            icon = describe.STATUS_ICON.get(c.status, "·")
            style = "red" if c.status in describe.TROUBLE else "green" if c.status == "ok" else "yellow"
            row = Text("  ")
            row.append(icon, style=style)
            row.append(f" {c.description}")
            row.append(f"  {c.tool}", style="dim")
            if c.status not in (None, "ok"):
                row.append(f"  {describe.STATUS_WORD.get(c.status, c.status)}", style=style)
            if c.recovered:
                row.append("  (settled after a restart)", style="dim italic")
            out.append(row)
            if c.args:
                out.append(describe.fields(c.args, indent=4))
            if c.reason:
                out.append(Text(f"    reason: {c.reason}", style=style))
            if c.status is not None and c.result is not None:
                out.append(Text("    → " + ("ok" if c.status == "ok" else describe.STATUS_WORD.get(c.status, "")),
                                style="dim" if c.status == "ok" else style))
                out.append(describe.fields(c.result, indent=6, style="dim" if c.status == "ok" else style))
        for note in seg.notes:
            out.append(Text("  ✎ written, not sent:", style="italic"))
            out.append(Text("    " + note.replace("\n", "\n    "), style="dim italic"))
        return to_content(out)


class NoticeView(ItemView):
    DEFAULT_CSS = """
    NoticeView { height: auto; margin: 1 0 0 0; content-align: center middle; text-align: center; color: $text-muted; }
    NoticeView.warn { color: $warning; }
    NoticeView.error { color: $error; }
    """

    def on_mount(self) -> None:
        self.update_item()

    def refresh_content(self) -> None:
        self.set_class(self.item.level == "warn", "warn")  # type: ignore[attr-defined]
        self.set_class(self.item.level == "error", "error")  # type: ignore[attr-defined]
        self.refresh(layout=True)

    def render(self) -> Content:
        n: Notice = self.item  # type: ignore[assignment]
        text = Text(f"· {n.text}", no_wrap=True, overflow="ellipsis")
        tail = f" · {stamp(n.at)}"
        text.truncate(max(10, self.size.width - len(tail)), overflow="ellipsis")
        text.append(tail)
        return to_content(text)


class DayView(ItemView):
    DEFAULT_CSS = "DayView { height: auto; margin: 1 0 0 0; color: $text-muted; }"

    def render(self) -> Content:
        label = f" {self.item.at:%A, %d %B %Y} "
        width = max(len(label), self.size.width)
        side = max(0, (width - len(label)) // 2)
        return Content.styled("─" * side + label + "─" * max(0, width - side - len(label)), "dim")


VIEWS: dict[type, type[ItemView]] = {Message: MessageView, Segment: SegmentView, Notice: NoticeView, Day: DayView}


class Transcript(VerticalScroll, can_focus=True):
    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 0 2 1 2; scrollbar-size-vertical: 1; }
    Transcript > #transcript-top { height: auto; color: $text-muted; text-align: center; }
    """

    class Unseen(TMessage):
        """The count of messages that arrived while the user was reading history changed."""

        def __init__(self, count: int):
            super().__init__()
            self.count = count

    def __init__(self, model: Model, name_of: Callable[[], str], **kw):
        super().__init__(**kw)
        self.model = model
        self.name_of = name_of
        self.views: dict[str, ItemView] = {}
        self.expanded: set[str] = set()
        self.start = 0  # model.items index of the first item that may be mounted
        self.following = True
        self.unseen = 0
        self._anchor: tuple[Widget, float] | None = None
        self._anchor_size: Size | None = None
        self._loading_earlier = False
        self._settling = False
        self._held = False  # text in the transcript is selected: don't follow

    def compose(self):
        yield Static("loading history…", id="transcript-top")

    def on_mount(self) -> None:
        self.watch(self.screen, "selections", self._selections_changed, init=False)

    def focus_on_click(self) -> bool:
        return False  # selecting or clicking text leaves the composer focused

    def _selections_changed(self, selections: dict) -> None:
        held = any(w is self or self in w.ancestors for w in selections)
        if held == self._held:
            return
        self._held = held
        if held:
            self._capture(force=True)  # new messages arrive below; the selected text stays put
        elif self.following:
            self.jump_to_end()  # catch up with whatever arrived meanwhile

    # -- building ----------------------------------------------------------------

    def _view(self, item: Item) -> ItemView:
        view = VIEWS[type(item)](item, self)
        self.views[item.key] = view
        return view

    def load(self) -> None:
        """Mount the most recent items, once history has been replayed."""
        visible = 0
        start = len(self.model.items)
        while start > 0 and visible < WINDOW:
            start -= 1
            visible += self.model.items[start].visible
        self.start = start
        views = [self._view(it) for it in self.model.items[start:] if it.visible]
        self._set_top()
        if views:
            self.mount_all(views)
        self.following = True  # the content growing settles the view at the end

    def _set_top(self) -> None:
        top = self.query_one("#transcript-top", Static)
        if self.start > 0:
            top.update("↑ scroll up for earlier conversation")
        elif not self.model.items:
            top.update("Nothing here yet. Say hello when you like; replies come when the character chooses.")
        else:
            top.update("")
        top.display = self.start > 0 or not self.model.items

    def sync(self, changed: list[Item]) -> None:
        """Bring widgets up to date with model items that changed."""
        new_messages = 0
        for item in changed:
            view = self.views.get(item.key)
            if view is not None:
                if item.visible:
                    view.update_item()
                else:
                    del self.views[item.key]
                    view.remove()
                continue
            if not item.visible or self._index(item) < self.start:
                continue
            before = self._next_view(item)
            if before is not None:
                self.mount(self._view(item), before=before)
            else:
                self.mount(self._view(item))
            new_messages += isinstance(item, Message)
        if changed:
            self._set_top()
        if (not self.following or self._held) and new_messages:
            self.unseen += new_messages
            self.post_message(self.Unseen(self.unseen))

    def _index(self, item: Item) -> int:
        i = getattr(item, "_index", None)
        if i is None or i >= len(self.model.items) or self.model.items[i] is not item:
            i = self.model.items.index(item)
            item._index = i  # type: ignore[attr-defined]
        return i

    def _next_view(self, item: Item) -> ItemView | None:
        """The mounted view of the first visible item after `item`, if any (usually none)."""
        items = self.model.items
        for later in items[self._index(item) + 1:]:
            if (view := self.views.get(later.key)) is not None:
                return view
        return None

    def load_earlier(self) -> None:
        if self.start == 0 or self._loading_earlier:
            return
        visible, start = 0, self.start
        while start > 0 and visible < PAGE:
            start -= 1
            visible += self.model.items[start].visible
        views = [self._view(it) for it in self.model.items[start:self.start] if it.visible]
        self.start = start
        first = next((c for c in self.children if isinstance(c, ItemView)), None)
        self._capture(force=True)
        self._loading_earlier = True  # until the view settles on the same item again
        if views:
            if first is not None:
                self.mount_all(views, before=first)
            else:
                self.mount_all(views)
        self._set_top()

    # -- following and the reading position ------------------------------------------

    def jump_to_end(self) -> None:
        if self._held:
            self.screen.clear_selection()  # going to the end ends what was being selected
        self.following = True
        self.call_after_refresh(self._trim)
        self.scroll_end(animate=False, immediate=True, x_axis=False)
        self.call_after_refresh(self._settle)  # again once anything just mounted is laid out
        if self.unseen:
            self.unseen = 0
            self.post_message(self.Unseen(0))

    def _trim(self) -> None:
        """Back at the end after reading far back: unmount the oldest views again, so
        resizing stays quick. Nothing visible moves."""
        views = [c for c in self.children if isinstance(c, ItemView)]
        excess = len(views) - WINDOW
        if not self.following or self._held or excess < PAGE:
            return
        for v in views[:excess]:
            del self.views[v.item.key]
        self.start = self._index(views[excess].item)
        self.remove_children(views[:excess])
        self._set_top()

    def watch_virtual_size(self, old: Size, new: Size) -> None:
        # Called mid-layout, before the container size is final: settle just after.
        self.call_next(self._settle)

    def on_resize(self, event: events.Resize) -> None:
        self._settle()

    def _settle(self) -> None:
        """Re-establish the view after the content or the viewport changed size: at the
        end when following, otherwise on the same item as before."""
        self._anchor_size = self.size
        self._loading_earlier = False
        self._settling = True  # the anchor stays as the user left it: no rounding drift
        try:
            if self.following and not self._held:
                self.scroll_end(animate=False, immediate=True, x_axis=False)
            elif self._anchor is not None and self._anchor[0].is_attached:
                view, frac = self._anchor
                r = view.virtual_region
                self.scroll_to(y=round(r.y + frac * r.height), animate=False, immediate=True)
            else:
                self._capture()
        finally:
            self._settling = False

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if self._settling or self._loading_earlier or (
                self._anchor_size is not None and self.size != self._anchor_size):
            return  # a change is being settled: keep the position from before it
        at_end = new >= self.max_scroll_y - 0.5
        if at_end != self.following:
            self.following = at_end
            if at_end:
                self.call_after_refresh(self._trim)
            if at_end and self.unseen:
                self.unseen = 0
                self.post_message(self.Unseen(0))
        self._capture()
        if new <= 1 and old > new and self.start > 0:
            self.load_earlier()

    def _capture(self, force: bool = False) -> None:
        """Remember the item at the top of the view and how far into it the view starts."""
        if not force and self._anchor_size is not None and self.size != self._anchor_size:
            return
        top = self.scroll_y
        views = [c for c in self.children if isinstance(c, ItemView)]
        lo, hi = 0, len(views)
        while lo < hi:  # first view whose bottom is below the top of the viewport
            mid = (lo + hi) // 2
            r = views[mid].virtual_region
            if r.y + r.height <= top:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(views):
            r = views[lo].virtual_region
            self._anchor = (views[lo], (top - r.y) / max(1, r.height))
        self._anchor_size = self.size

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.scroll_y <= 1 and self.start > 0:
            self.load_earlier()

    def focus_segments(self, step: int) -> None:
        """Move focus to the next/previous visible activity entry."""
        segs = sorted((v for v in self.views.values() if isinstance(v, SegmentView)),
                      key=lambda v: v.virtual_region.y)
        if not segs:
            return
        cur = self.screen.focused
        if cur in segs:
            i = segs.index(cur) + step
        else:  # start from what's on screen
            bottom = self.scroll_y + self.size.height
            on_screen = [j for j, v in enumerate(segs) if v.virtual_region.y < bottom]
            i = on_screen[-1] if on_screen else 0
        if 0 <= i < len(segs):
            segs[i].focus()
            segs[i].scroll_visible(animate=False)
