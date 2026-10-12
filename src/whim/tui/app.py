"""`whim tui`: a full-screen terminal client of the whim daemon.

The app wires three layers together: `protocol` (daemon connection, event stream,
outbox), `state` (the view model built from events) and the widgets here and in
`transcript` and `views`. It never writes character state except through the API.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Offset
from textual.message import Message as TMessage
from textual.screen import ModalScreen, Screen
from textual.selection import Selection
from textual.widgets import Button, Checkbox, Input, Label, Markdown, Static, TextArea

from whim import safefs
from whim.config import Config
from whim.tui import clipboard, describe
from whim.tui.markup import to_content
from whim.tui.protocol import Link, Outbox, Store
from whim.tui.state import Model, parse_at
from whim.tui.transcript import Transcript, stamp

VIEWS = [("chat", "Conversation"), ("journal", "Journal"), ("memory", "Memory"), ("schedule", "Schedule"),
         ("activity", "Activity"), ("admin", "Administration")]
VIEW_KEYS = {"journal": "f2", "memory": "f3", "schedule": "f4", "activity": "f5", "admin": "f6"}
NARROW, WIDE, SHORT = 70, 120, 22


def character_name(cfg: Config) -> str:
    """The name the character gave itself in IDENTITY.md, if it reads like one."""
    text = safefs.read_text(cfg.workspace, "IDENTITY.md") or ""
    m = re.search(r"(?im)^[\W_]*name[\W_]*?[:：]\s*\**\s*([^\s*—,.;:()\[\]]+)", text)
    return m.group(1) if m else (cfg.seed_name or "whim")


def until(at: datetime, now: datetime | None = None) -> str:
    secs = int((at - (now or datetime.now().astimezone())).total_seconds())
    if secs <= 0:
        return "due"
    if secs < 3600:
        return f"in {max(1, secs // 60)}m"
    if secs < 86400:
        return f"in {secs // 3600}h {secs % 3600 // 60:02d}m"
    return f"in {secs // 86400}d"


WORD = re.compile(r"\w+|[^\w\s]+|\s+")


def select_unit(screen: Screen, event: events.Click) -> None:
    """Double-click selects a word and triple-click a line (a logical line: however it is
    wrapped on screen). Textual's own default selects the whole widget, then the whole
    scrolling container, which in the conversation would be everything."""
    widget = event.widget
    if widget is None or not widget.allow_select or isinstance(widget, (TextArea, Input)):
        return
    hit, offset = screen.get_widget_and_offset_at(*event.screen_offset)
    whole = widget.get_selection(Selection(None, None)) if hit is widget and offset is not None else None
    if not whole:
        return
    lines = whole[0].splitlines()
    if offset.y >= len(lines):
        return
    line = lines[offset.y]
    start, end = 0, len(line)
    if event.chain == 2:
        for m in WORD.finditer(line):
            if m.start() <= offset.x < m.end() or m.end() == len(line):
                start, end = m.span()
                break
    screen.selections = {widget: Selection(Offset(start, offset.y), Offset(end, offset.y))}
    screen.app.publish_primary(line[start:end])  # type: ignore[attr-defined]


class Chrome(Static):
    """Labels and bars: not part of what a drag selects."""

    ALLOW_SELECT = False


class ResponsiveScreen(Screen):
    """Screens get `narrow`, `wide` and `short` classes from their size, and the app's
    selection gestures on top of Textual's drag-to-select."""

    def on_resize(self, event: events.Resize) -> None:
        w, h = event.size
        self.set_class(w < NARROW, "narrow")
        self.set_class(w >= WIDE, "wide")
        self.set_class(h < SHORT, "short")

    def on_click(self, event: events.Click) -> None:
        if event.chain in (2, 3):
            select_unit(self, event)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3 and self.selections:  # right-click copies, as in many terminals
            self.action_copy_text()

    def on_text_selected(self, event: events.TextSelected) -> None:
        if text := self.get_selected_text():
            self.app.publish_primary(text)  # type: ignore[attr-defined]


# -- chat screen widgets ---------------------------------------------------------------


class StatusBar(Chrome):
    DEFAULT_CSS = "StatusBar { height: 1; padding: 0 1; background: $panel; }"

    def render(self) -> RenderableType:
        app: WhimApp = self.app  # type: ignore[assignment]
        link, model, snap = app.link, app.model, app.link.snapshot
        width = self.size.width
        left = Text(no_wrap=True, overflow="ellipsis")
        dot = {"connected": ("●", "green"), "connecting": ("●", "yellow"), "down": ("●", "yellow"),
               "absent": ("●", "red")}[link.state]
        left.append(dot[0] + " ", style=dot[1])
        left.append(app.char_name, style="bold")
        if link.state != "connected":
            left.append("  " + {"connecting": "connecting…", "down": "reconnecting…",
                                "absent": "whim isn't running"}[link.state], style=dot[1])
        elif not link.loaded:
            left.append("  loading…", style="dim")
        elif model.active_run:
            run = model.runs.get(model.active_run)
            left.append("  awake", style="")
            if run is not None and run.autonomous:
                left.append(f" ({describe.origin_label(run.origin)})", style="dim")
            left.append(f" · {model.activity or 'thinking'}", style="dim italic")
        else:
            left.append("  resting", style="dim")
            if snap.get("draining_for_restart"):
                left.append(" · restarting soon", style="yellow")
        right = Text(no_wrap=True)
        waiting = len(model.approvals) + len(model.questions)
        if waiting:
            right.append(f" ⚑ {waiting} waiting ", style="bold black on yellow")
            right.append(" ")
        hb = (snap.get("heartbeat") or {}) if link.state == "connected" else {}  # stale while disconnected
        if hb.get("enabled") and hb.get("next_due_at") and width >= 60:
            due = parse_at(hb["next_due_at"])
            right.append("heartbeat " if width >= 90 else "♡ ", style="dim")
            right.append(f"{due:%H:%M}", style="")
            if width >= 90:
                right.append(f" ({until(due)})", style="dim")
        elif hb and not hb.get("enabled") and width >= 60:
            right.append("heartbeats off", style="dim")
        left.truncate(max(10, width - right.cell_len - 3), overflow="ellipsis")
        pad = max(1, width - 2 - left.cell_len - right.cell_len)
        return Text.assemble(left, " " * pad, right)


class AttentionBar(Chrome):
    """What the character is waiting on from the human: approvals and questions."""

    DEFAULT_CSS = """
    AttentionBar { height: auto; max-height: 3; padding: 0 1; background: $warning 20%; display: none; }
    AttentionBar:hover { background: $warning 30%; }
    """

    def show(self, approvals: dict, questions: dict) -> None:
        self.display = bool(approvals or questions)
        if not self.display:
            return
        line = Text(overflow="ellipsis", no_wrap=True)
        if approvals:
            first = next(iter(approvals.values()))
            summary = (first.get("summary") or "").splitlines()[0] if first.get("summary") else first.get("subject", "")
            line.append("⚑ approval needed", style="bold")
            if any("WARNING" in (a.get("summary") or "") for a in approvals.values()):
                line.append(" (⚠ with a warning)", style="bold red")
            line.append(": " + summary)
        elif questions:
            first = next(iter(questions.values()))
            line.append("? asked you: ", style="bold")
            line.append(" ".join(first.get("text", "").split()))
        more = len(approvals) + len(questions) - 1
        tail = Text((f"  +{more} more" if more > 0 else "") + "  · ctrl+o to review", style="dim")
        line.truncate(max(10, self.size.width - tail.cell_len - 2), overflow="ellipsis")
        self.update(Text.assemble(line, tail))

    def on_click(self) -> None:
        self.app.action_attention()  # type: ignore[attr-defined]


class Composer(TextArea):
    """Multiline message editor: Enter is a newline; ctrl+enter (where the terminal can
    report it) or ctrl+g sends."""

    DEFAULT_CSS = """
    Composer { height: auto; min-height: 3; max-height: 12; border: round $panel-lighten-2; padding: 0 1; }
    Composer:focus { border: round $accent 70%; }
    Composer .text-area--cursor-line { background: transparent; }
    """
    BINDINGS = [Binding("ctrl+enter,ctrl+g,ctrl+j", "send", "Send", priority=True)]

    class Submitted(TMessage):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def action_send(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_copy(self) -> None:
        # Text selected elsewhere on the screen is newer than any selection in here:
        # changing this one clears that one.
        if self.screen.selections:
            self.screen.action_copy_text()
        else:
            super().action_copy()

    def on_resize(self, event: events.Resize) -> None:
        # TextArea rewraps on resize but repaints only if its line count changed, which
        # can leave lines wrapped for the old width on screen.
        self.refresh()


class Inspector(VerticalScroll):
    DEFAULT_CSS = """
    Inspector { width: 40; height: 1fr; border-left: vkey $panel-lighten-1; padding: 0 1; display: none; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="inspector-body")

    def update_from(self, app: "WhimApp") -> None:
        self.query_one("#inspector-body", Static).update(status_panel(app))

    def on_show(self) -> None:
        self.update_from(self.app)  # type: ignore[arg-type]


def status_panel(app: "WhimApp", wide: bool = False) -> Content:
    """Now / heartbeat / jobs / memory, as shown in the inspector and the Schedule view."""
    model, snap = app.model, app.link.snapshot
    out: list[RenderableType] = []

    def head(s: str) -> None:
        out.append(Text(("\n" if out else "") + s, style="bold"))

    head("Now")
    if model.active_run and (run := model.runs.get(model.active_run)):
        out.append(Text(f"awake since {run.started:%H:%M} ({describe.origin_label(run.origin)})"))
        out.append(Text(f"{run.requests} model requests, {len(run.calls)} actions", style="dim"))
        if model.activity:
            out.append(Text(f"last: {model.activity}", style="dim italic"))
    else:
        out.append(Text("resting"))
    if snap.get("pending_inputs"):
        out.append(Text(f"{snap['pending_inputs']} message(s) waiting to be read", style="yellow"))
    hb = snap.get("heartbeat") or {}
    head("Heartbeat")
    if not hb:
        out.append(Text("unknown", style="dim"))
    elif not hb.get("enabled"):
        out.append(Text("off", style="dim"))
    elif hb.get("next_due_at"):
        due = parse_at(hb["next_due_at"])
        out.append(Text(f"next {stamp(due)} ({until(due)})"))
    else:
        out.append(Text("not armed yet (after the first meeting)", style="dim"))
    if model.cues is not None:
        active = [c for c in model.cues if c.get("status") == "active"]
        out.append(Text(f"{len(active)} active cue(s)", style="dim"))
        for c in active[: (8 if wide else 4)]:
            t = Text("  • ", style="dim")
            t.append(" ".join(c.get("instruction", "").split()), style="")
            if c.get("priority") not in (None, "normal"):
                t.append(f" ({c['priority']})", style="dim")
            out.append(t)
    elif hb.get("active_cues"):
        out.append(Text(f"{hb['active_cues']} active cue(s)", style="dim"))
    jobs = [j for j in snap.get("jobs") or [] if j.get("status") in ("active", "paused", "failed")]
    head("Scheduled jobs")
    if not jobs:
        out.append(Text("none", style="dim"))
    for j in jobs[: (10 if wide else 5)]:
        t = Text("• ")
        t.append(" ".join(j.get("instruction", "").split())[:120])
        meta = j.get("next") or j.get("schedule", "")
        t.append(f"\n  {meta}" + (f" · {j['status']}" if j.get("status") != "active" else ""), style="dim")
        if j.get("error"):
            t.append(f"\n  {j['error']}", style="red")
        out.append(t)
    ctx = snap.get("context") or {}
    if ctx:
        head("Working memory")
        est, trig = ctx.get("estimated_tokens"), ctx.get("trigger_tokens")
        if est is not None and trig:
            out.append(Text(f"~{est:,} tokens; compacts at {trig:,}"))
        if ctx.get("in_progress"):
            out.append(Text("compacting now", style="yellow"))
        if ctx.get("blocked"):
            out.append(Text("full: new messages wait", style="red"))
    if snap.get("queued_wakes"):
        out.append(Text(f"\n{snap['queued_wakes']} wake(s) queued", style="dim"))
    return to_content(out)


class ChatScreen(ResponsiveScreen):
    DEFAULT_CSS = """
    ChatScreen { layout: vertical; }
    ChatScreen #body { height: 1fr; }
    ChatScreen #convo { width: 1fr; height: 1fr; }
    ChatScreen #unseen { height: 1; display: none; text-align: center; background: $accent 30%; }
    ChatScreen #hints { height: 1; padding: 0 1; color: $text-muted; }
    ChatScreen.wide.inspector #inspector { display: block; }
    ChatScreen.short #hints { display: none; }
    ChatScreen.short Composer { max-height: 5; }
    ChatScreen.narrow Transcript { padding: 0 1 1 1; }
    """
    BINDINGS = [
        Binding("pageup", "page(-1)", "Scroll up", show=False, priority=True),
        Binding("pagedown", "page(1)", "Scroll down", show=False, priority=True),
        Binding("ctrl+end,ctrl+l", "latest", "Latest", show=False, priority=True),
        Binding("ctrl+up", "segment(-1)", "Previous activity", show=False, priority=True),
        Binding("ctrl+down", "segment(1)", "Next activity", show=False, priority=True),
        Binding("escape", "composer", "Back to the composer", show=False, priority=True),
        Binding("ctrl+g", "send", "Send", show=False, priority=True),
        Binding("ctrl+b", "app.inspector", "Inspector", show=False),
        Binding("ctrl+o", "app.attention", "Review", show=False),
    ]

    def compose(self) -> ComposeResult:
        app: WhimApp = self.app  # type: ignore[assignment]
        yield StatusBar(id="status")
        with Horizontal(id="body"):
            with Vertical(id="convo"):
                yield Transcript(app.model, lambda: app.char_name, id="transcript")
                yield Chrome(id="unseen")
                yield AttentionBar(id="attention")
                draft = app.store.data.get("draft") or {}
                composer = Composer(draft.get("text", ""), id="composer", soft_wrap=True, show_line_numbers=False,
                                    tab_behavior="focus", placeholder="Write a message…")
                yield composer
                yield Chrome(id="hints")
            yield Inspector(id="inspector")

    def on_mount(self) -> None:
        composer = self.query_one(Composer)
        cursor = (self.app.store.data.get("draft") or {}).get("cursor")  # type: ignore[attr-defined]
        if cursor:
            try:
                composer.cursor_location = tuple(cursor)
            except (ValueError, IndexError):
                pass
        else:
            composer.move_cursor(composer.document.end)
        composer.border_subtitle = "ctrl+g send · ⏎ newline"
        composer.focus()
        self.set_class(True, "inspector")
        self.update_hints()

    def on_resize(self, event: events.Resize) -> None:
        super().on_resize(event)
        self.update_hints()

    def update_hints(self) -> None:
        w = self.size.width
        if w >= 110:
            s = ("ctrl+g send  ·  pgup/pgdn scroll  ·  ctrl+↑↓ activity  ·  f2–f6 views  ·  ctrl+p commands  ·  "
                 "f1 help  ·  ctrl+q quit")
        elif w >= 70:
            s = "ctrl+g send  ·  f2–f6 views  ·  ctrl+p commands  ·  f1 help  ·  ctrl+q quit"
        else:
            s = "ctrl+g send · ctrl+p · f1 help"
        self.query_one("#hints", Chrome).update(s)

    @property
    def transcript(self) -> Transcript:
        return self.query_one(Transcript)

    def action_page(self, step: int) -> None:
        t = self.transcript
        (t.scroll_page_down if step > 0 else t.scroll_page_up)(animate=False)

    def action_latest(self) -> None:
        self.transcript.jump_to_end()

    def action_segment(self, step: int) -> None:
        self.transcript.focus_segments(step)

    def action_composer(self) -> None:
        self.clear_selection()
        self.query_one(Composer).focus()

    def action_send(self) -> None:
        composer = self.query_one(Composer)
        composer.focus()
        composer.action_send()

    def on_key(self, event: events.Key) -> None:
        """Typing while the transcript has focus goes to the composer."""
        composer = self.query_one(Composer)
        if self.focused is not composer and event.is_printable and event.character and event.key != "space":
            composer.focus()
            composer.insert(event.character)
            event.stop()

    @on(Transcript.Unseen)
    def unseen(self, event: Transcript.Unseen) -> None:
        bar = self.query_one("#unseen", Chrome)
        bar.display = event.count > 0
        bar.update(f"↓ {event.count} new message{'s' * (event.count != 1)} · ctrl+end to jump")

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id == "unseen":
            self.transcript.jump_to_end()

    @on(Composer.Submitted)
    def submitted(self, event: Composer.Submitted) -> None:
        self.app.send(event.text)  # type: ignore[attr-defined]

    @on(TextArea.Changed, "#composer")
    def draft_changed(self) -> None:
        self.app.save_draft_soon()  # type: ignore[attr-defined]


# -- modals ----------------------------------------------------------------------------


class Modal(ModalScreen):
    DEFAULT_CSS = """
    Modal { align: center middle; }
    Modal > #dialog {
        width: 90; max-width: 100%; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    Modal #dialog-body { height: auto; max-height: 1fr; }
    Modal .buttons { dock: bottom; height: auto; margin-top: 1; align-horizontal: right; }
    Modal .buttons Button { margin-left: 1; min-width: 10; }
    Modal .choice { height: auto; border: none !important; min-width: 0; width: auto; max-width: 100%; text-wrap: wrap; text-align: left; content-align-horizontal: left;
                    margin: 0 0 1 0; padding: 0 2; }
    Modal .warning { color: $error; text-style: bold; }
    """
    BINDINGS = [Binding("escape", "dismiss_modal", "Close", show=False)]

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class ConfirmScreen(Modal):
    def __init__(self, title: str, body: str, confirm: str = "Confirm", danger: bool = False):
        super().__init__()
        self.title_text, self.body, self.confirm, self.danger = title, body, confirm, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm, id="ok", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


def standing_note(scopes: list[str]) -> str:
    """What the standing choice adds, and what it doesn't (see whim.authz.STANDING)."""
    extra = [s for s in scopes if s != "plugin.run"]
    this = f"{', '.join(extra)} {'is' if len(extra) == 1 else 'are'} approved for this version only, and " if extra else ""
    return ("Also adds a standing plugin.run grant: from now on any plugin, or new version of one, runs in "
            f"the sandbox without asking. It covers nothing more: {this}every plugin still has to ask for net, "
            "workspace or any other permission. Revocable in Admin (f6).")


class ApprovalScreen(Modal):
    """Exactly what the character asked for, with its warnings. Deciding takes a button
    press (no single-key approve); focus starts on "Later"."""

    def __init__(self, approval: dict):
        super().__init__()
        self.approval = approval

    def compose(self) -> ComposeResult:
        a = self.approval
        scopes = a.get("scopes")
        if isinstance(scopes, str):
            try:
                scopes = json.loads(scopes)
            except ValueError:
                scopes = [scopes]
        self.scopes = list(scopes or [])
        lines = (a.get("summary") or "").splitlines()
        with Vertical(id="dialog"):
            yield Label(Text(f"{self.app.char_name} asks for your approval", style="bold"))  # type: ignore[attr-defined]
            with VerticalScroll(id="dialog-body"):
                for line in lines:
                    if line.startswith("WARNING:"):
                        yield Static(Text("⚠ " + line[len("WARNING:"):].strip()), classes="warning")
                    else:
                        yield Static(Text(line))
                meta = Text(f"\nscopes: {', '.join(self.scopes) or '—'}\nfor: {a.get('subject', '?')}", style="dim")
                if a.get("created_at"):
                    meta.append(f"\nasked {stamp(parse_at(a['created_at']))} · {a.get('id', '')}", style="dim")
                yield Static(meta)
                if "plugin.run" in self.scopes:
                    yield Checkbox("Let any plugin run sandboxed", id="standing")
                    yield Static(Text(standing_note(self.scopes), style="dim"), classes="note")
            with Horizontal(classes="buttons"):
                yield Button("Later", id="later")
                yield Button("Deny", id="deny", variant="error")
                yield Button("Approve", id="approve", variant="success")

    def on_mount(self) -> None:
        self.query_one("#later", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "later":
            self.dismiss(None)
            return
        standing = bool(self.query("#standing") and self.query_one("#standing", Checkbox).value)
        self.dismiss({"decision": event.button.id, "standing": standing})


class QuestionScreen(Modal):
    """Answer a question: pick a choice, write words, or both."""

    def __init__(self, question: dict):
        super().__init__()
        self.question = question
        self.choice: int | None = None

    def compose(self) -> ComposeResult:
        q = self.question
        with Vertical(id="dialog"):
            yield Label(Text(f"{self.app.char_name} asked you", style="bold"))  # type: ignore[attr-defined]
            with VerticalScroll(id="dialog-body"):
                yield Markdown(q.get("text", ""))
                if q.get("expires_at"):
                    yield Static(Text(f"open until {stamp(parse_at(q['expires_at']))}", style="dim"))
                for i, ch in enumerate(q.get("choices") or [], 1):
                    yield Button(f"{i}. {ch}", id=f"choice-{i}", classes="choice")
                yield Input(placeholder="your words (optional with a choice)", id="words")
            with Horizontal(classes="buttons"):
                yield Button("Later", id="later")
                yield Button("Send answer", id="send", variant="primary")

    def on_mount(self) -> None:
        first = self.query(".choice")
        (first.first() if first else self.query_one("#words", Input)).focus()

    @on(Button.Pressed, ".choice")
    def chose(self, event: Button.Pressed) -> None:
        self.choice = int(event.button.id.split("-")[1])
        for b in self.query(".choice").results(Button):
            b.variant = "primary" if b is event.button else "default"
        self.query_one("#words", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#send")
    def send(self) -> None:
        words = self.query_one("#words", Input).value.strip()
        if self.choice is None and not words:
            self.notify("Pick a choice or write something.", severity="warning")
            return
        self.dismiss({"choice": self.choice, "text": words or None})

    @on(Button.Pressed, "#later")
    def later(self) -> None:
        self.dismiss(None)


HELP_KEYS = [
    ("Conversation", None),
    ("ctrl+g", "send (also ctrl+enter, where the terminal reports it)"),
    ("enter", "new line"),
    ("ctrl+z / ctrl+y", "undo / redo"),
    ("shift+arrows", "select; ctrl+x / ctrl+c cut and copy, paste with the terminal's paste"),
    ("pgup / pgdn", "scroll the conversation"),
    ("ctrl+end", "jump to the latest message and follow it again"),
    ("ctrl+↑ / ctrl+↓", "move between activity entries; enter expands one"),
    ("escape", "back to the composer"),
    ("ctrl+o", "review what the character is waiting on: approvals, questions"),
    ("ctrl+b", "inspector (wide terminals; otherwise the schedule view)"),
    ("Selecting text", None),
    ("drag", "select, across messages; drag past the edge to scroll"),
    ("double / triple click", "a word / a whole line"),
    ("ctrl+c · right-click", "copy the selection; escape clears it"),
    ("shift+drag", "your terminal's own selection, if you prefer it"),
    ("Views", None),
    ("f2 · f3 · f4", "journal · memory · schedule"),
    ("f5 · f6", "activity · administration"),
    ("escape", "back to the conversation"),
    ("Everywhere", None),
    ("ctrl+p", "commands: steer, send for later, stop, interrupt, wake, views…"),
    ("ctrl+q", "quit (whim keeps running; your draft and unconfirmed messages are kept)"),
]
HELP_NOTE = ("A message counts as sent once whim confirms it. If the connection drops mid-send it is retried "
             "with the same id, so it can't arrive twice.")


def help_text() -> RenderableType:
    from rich.table import Table

    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    for key, desc in HELP_KEYS:
        if desc is None:
            table.add_row(Text(key, style="bold underline"), "")
        else:
            table.add_row(key, desc)
    return Group(table, Text(""), Text(HELP_NOTE, style="dim"))


class HelpScreen(Modal):
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            with VerticalScroll(id="dialog-body"):
                yield Static(help_text())
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    @on(Button.Pressed)
    def close(self) -> None:
        self.dismiss(None)


# -- the app ---------------------------------------------------------------------------


class WhimApp(App):
    TITLE = "whim"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "help", "Help"),
        # F-keys decode reliably in every terminal; alt+digit only where the terminal reports it.
        *[Binding(",".join(filter(None, (VIEW_KEYS.get(name), f"alt+{i}"))), f"view('{name}')", label, show=False,
                  priority=True)
          for i, (name, label) in enumerate(VIEWS, 1)],
    ]

    def __init__(self, cfg: Config, name: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.model = Model()
        self.store = Store(cfg.home)
        self.link = Link(cfg, self.model, on_events=self.schedule_flush, on_state=self.state_changed,
                         on_loaded=self.history_loaded)
        self.outbox = Outbox(self.store, self.outbox_update)
        self.name_override = name
        self.char_name = name or character_name(cfg)
        self._flush_timer = None
        self._draft_timer = None
        self._snapshot_timer = None

    # -- lifecycle -------------------------------------------------------------------

    async def on_mount(self) -> None:
        from whim.tui import views

        self.chat = ChatScreen()
        self.install_screen(self.chat, "chat")
        for name, cls in (("journal", views.JournalScreen), ("memory", views.MemoryScreen),
                          ("schedule", views.ScheduleScreen), ("activity", views.ActivityScreen),
                          ("admin", views.AdminScreen)):
            self.install_screen(cls(), name)
        await self.push_screen("chat")
        await self.link.start()
        self.run_worker(self.link.follow(), group="link", exit_on_error=False)
        self.run_worker(self.outbox.run(self.link.client), group="outbox", exit_on_error=False)
        self.set_interval(30, self.tick)

    async def on_unmount(self) -> None:
        await self.link.close()

    async def action_quit(self) -> None:
        self.save_draft()
        left = len(self.outbox.items)
        await self.link.close()
        self.exit(("1 message isn't" if left == 1 else f"{left} messages aren't") + " confirmed by whim yet; "
                  + ("it" if left == 1 else "they") + "'ll be retried (with the same id, so never twice) the next "
                  "time you open whim tui." if left else None)

    def tick(self) -> None:
        self.refresh_status()

    # -- events from the link ------------------------------------------------------------

    def history_loaded(self) -> None:
        for item in self.outbox.items:  # unconfirmed messages from a previous session
            state = item["state"] if item["state"] in ("unsent", "unconfirmed") else "sending"
            self.model.add_local(item["cmid"], item["text"], mode=item.get("mode"),
                                 question_id=item.get("question_id"), state=state)
        self.model.take_changes()  # the transcript is built from the model as it is now
        self.chat.transcript.load()
        self.refresh_status()
        self.reconcile_attention()

    def schedule_flush(self) -> None:
        if self._flush_timer is None:
            self._flush_timer = self.set_timer(0.04, self.flush)

    def flush(self) -> None:
        self._flush_timer = None
        if not self.link.loaded:
            return
        items, flags = self.model.take_changes()
        self.chat.transcript.sync(items)
        if flags & {"status", "attention", "snapshot"}:
            self.refresh_status()
        if "attention" in flags:
            self.reconcile_attention()
        if "snapshot" in flags and self._snapshot_timer is None:
            self._snapshot_timer = self.set_timer(0.5, self._refresh_snapshot)
        if "activity" in flags and hasattr(self.screen, "data_changed"):
            self.screen.data_changed()

    def _refresh_snapshot(self) -> None:
        self._snapshot_timer = None
        self.run_worker(self.link.refresh_snapshot(), group="snapshot")

    def state_changed(self) -> None:
        if self.name_override is None:
            self.char_name = character_name(self.cfg)
        self.refresh_status()
        if hasattr(self.screen, "data_changed"):
            self.screen.data_changed()

    def refresh_status(self) -> None:
        if not self.is_mounted or not hasattr(self, "chat") or not self.chat.is_mounted:
            return
        self.chat.query_one(StatusBar).refresh()
        self.chat.query_one(AttentionBar).show(self.model.approvals, self.model.questions)
        self.chat.query_one(Inspector).update_from(self)

    @work(group="attention", exclusive=True)
    async def reconcile_attention(self) -> None:
        """Pending approvals and questions from the API, which is authoritative (some
        endings, such as a cancelled approval, have no event of their own)."""
        ok, _, data = await self.link.request("GET", "/v1/approvals")
        if ok:
            self.model.approvals = {a["id"]: a for a in data.get("pending", [])}
        ok, _, data = await self.link.request("GET", "/v1/questions")
        if ok:
            self.model.questions = {q["id"]: q for q in data.get("pending", [])}
        self.refresh_status()

    # -- sending -------------------------------------------------------------------------

    def send(self, text: str, mode: str | None = None) -> None:
        if not text.strip():
            return
        if mode == "steer":
            path, body = "/v1/control", {"action": "steer", "text": text}
        else:
            path, body = "/v1/input", {"text": text, **({"when": "next_run"} if mode == "next_run" else {})}
        item = self.outbox.add(path, body, text=text, mode=mode)  # saved before the composer is cleared
        composer = self.chat.query_one(Composer)
        composer.clear()
        self.save_draft()
        self.model.add_local(item["cmid"], text, mode=mode)
        self.chat.transcript.jump_to_end()
        self.flush()

    def outbox_update(self, item: dict, state: str, note: str) -> None:
        self.model.local_state(item["cmid"], state, note)
        if state == "rejected":
            composer = self.chat.query_one(Composer)
            if not composer.text.strip():
                composer.text = item["text"]
                composer.move_cursor(composer.document.end)
                self.notify(f"whim refused that message: {note}. It's back in the composer.", severity="error")
            else:
                self.notify(f"whim refused a message: {note}", severity="error")
        self.schedule_flush()

    def copy_to_clipboard(self, text: str) -> None:
        """Everything copied in the app (selections, the composer's ctrl+c) goes through
        here: the system clipboard, tmux, or the terminal (see `clipboard`)."""
        self._clipboard = text  # so ctrl+v in the composer pastes it whatever happens

        def osc52(payload: str) -> None:
            self.call_from_thread(super(WhimApp, self).copy_to_clipboard, payload)

        def go() -> None:
            how = clipboard.copy(text, osc52)
            n = len(text)
            message = {"copied": f"copied {n} character{'s' * (n != 1)}",
                       "requested": f"sent {n} character{'s' * (n != 1)} to the terminal's clipboard",
                       "failed": "couldn't reach a clipboard (ctrl+v in the composer still pastes it)"}[how]
            self.call_from_thread(self.notify, message, severity="warning" if how == "failed" else "information",
                                  timeout=2.5)

        self.run_worker(go, thread=True, group="clipboard")

    def publish_primary(self, text: str) -> None:
        self.run_worker(lambda: clipboard.publish_primary(text), thread=True, group="primary", exclusive=True)

    def save_draft_soon(self) -> None:
        if self._draft_timer is not None:
            self._draft_timer.stop()
        self._draft_timer = self.set_timer(0.5, self.save_draft)

    def save_draft(self) -> None:
        self._draft_timer = None
        if not hasattr(self, "chat") or not self.chat.is_mounted:
            return
        composer = self.chat.query_one(Composer)
        self.store.data["draft"] = {"text": composer.text, "cursor": list(composer.cursor_location)}
        self.store.save()

    # -- actions -------------------------------------------------------------------------

    def action_view(self, name: str) -> None:
        if isinstance(self.screen, ModalScreen):  # finish with the dialog first
            return
        if self.get_screen(name) is not self.screen:
            self.switch_screen(name)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_inspector(self) -> None:
        if self.screen is not self.chat:
            return
        if self.chat.size.width < WIDE:
            self.action_view("schedule")
            return
        self.chat.toggle_class("inspector")
        self.refresh_status()

    def action_attention(self) -> None:
        if self.model.approvals:
            self.review_approval(next(iter(self.model.approvals.values())))
        elif self.model.questions:
            self.answer_question(next(iter(self.model.questions.values())))
        else:
            self.notify("Nothing is waiting on you.")

    def review_approval(self, approval: dict) -> None:
        async def decided(result: dict | None) -> None:
            if not result:
                return
            ok, msg, _ = await self.link.request("POST", f"/v1/approvals/{approval['id']}", result)
            verb = "approved" if result["decision"] == "approve" else "denied"
            self.report(ok, msg, verb, f"not {verb}")
            self.reconcile_attention()

        self.push_screen(ApprovalScreen(approval), decided)

    def answer_question(self, question: dict) -> None:
        def answered(result: dict | None) -> None:
            if not result:
                return
            choices = question.get("choices") or []
            picked = choices[result["choice"] - 1] if result["choice"] and result["choice"] <= len(choices) else None
            shown = (picked + (f" — {result['text']}" if result["text"] else "")) if picked else result["text"] or ""
            body = {k: v for k, v in result.items() if v is not None}
            item = self.outbox.add(f"/v1/questions/{question['id']}/answer", body, text=shown,
                                   question_id=question["id"])
            self.model.add_local(item["cmid"], shown, question_id=question["id"])
            self.chat.transcript.jump_to_end()
            self.flush()

        self.push_screen(QuestionScreen(question), answered)

    def control(self, action: str) -> None:
        async def go() -> None:
            ok, msg, _ = await self.link.request("POST", "/v1/control", {"action": action})
            self.report(ok, msg, f"{action} requested", action)

        self.run_worker(go(), group="control")

    def wake(self) -> None:
        async def go() -> None:
            ok, msg, _ = await self.link.request("POST", "/v1/wake", {"reason": "manual"})
            self.report(ok, msg, "wake queued", "wake")

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.run_worker(go(), group="control")

        self.push_screen(ConfirmScreen("Wake the character?", "This starts a wake without a message (a model call).",
                                       "Wake"), confirmed)

    def report(self, ok: bool, msg: str, done: str, failed: str) -> None:
        """Say how a one-off request went. An unknown outcome is said as such: it may
        have taken effect, so it is never reported as a failure (or retried)."""
        if ok:
            self.notify(done)
        elif msg.startswith("outcome unknown"):
            self.notify(msg, severity="warning", timeout=10)
        else:
            self.notify(f"{failed}: {msg}", severity="error")

    def send_composer(self, mode: str) -> None:
        text = self.chat.query_one(Composer).text
        if not text.strip():
            self.notify("Write the message in the composer first.", severity="warning")
            return
        self.send(text, mode)

    def get_system_commands(self, screen: Screen):
        yield from super().get_system_commands(screen)
        for name, label in VIEWS:
            key = "escape" if name == "chat" else VIEW_KEYS[name]
            yield SystemCommand(f"View: {label}", key, lambda n=name: self.action_view(n))
        yield SystemCommand("Steer with the composer text", "Get it to the character right away, even mid-wake",
                            lambda: self.send_composer("steer"))
        yield SystemCommand("Send the composer text for later", "Held until the current wake ends",
                            lambda: self.send_composer("next_run"))
        yield SystemCommand("Stop the current wake", "At its next safe point", lambda: self.control("stop"))
        yield SystemCommand("Interrupt the current wake", "Cancel it now", lambda: self.control("interrupt"))
        yield SystemCommand("Wake the character", "A wake without a message", self.wake)
        yield SystemCommand("Review approvals and questions", "ctrl+o", self.action_attention)
        yield SystemCommand("Toggle the inspector", "ctrl+b", self.action_inspector)
        yield SystemCommand("Jump to the latest message", "ctrl+end", lambda: self.chat.transcript.jump_to_end())


def run(cfg: Config, name: str | None = None) -> None:
    message = WhimApp(cfg, name).run()
    if message:
        print(message)
