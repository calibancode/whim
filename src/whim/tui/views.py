"""Secondary views: journal, memory files, schedule, activity and administration.

Workspace files are read locally (read-only, never following links); everything else
comes from the daemon's API and the event-derived model.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import date

from rich.console import RenderableType
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, Checkbox, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from whim import safefs
from whim.config import CHARACTER_FILES
from whim.tui import describe
from whim.tui.app import VIEW_KEYS, VIEWS, Chrome, ConfirmScreen, ResponsiveScreen, status_panel
from whim.tui.markup import to_content
from whim.tui.state import Run, parse_at
from whim.tui.transcript import stamp

MAX_SHOWN_BYTES = 512 * 1024


class ViewHeader(Chrome):
    DEFAULT_CSS = "ViewHeader { height: 1; padding: 0 1; background: $panel; }"

    def __init__(self, current: str):
        super().__init__()
        self.current = current

    def render(self) -> RenderableType:
        t = Text(no_wrap=True, overflow="ellipsis")
        narrow = self.size.width < 66
        for name, _ in VIEWS:
            key = "esc" if name == "chat" else VIEW_KEYS[name]
            short = SHORT_NAMES[name]
            if name == self.current:
                t.append(f" {short} ", style="bold reverse")
            else:
                t.append(f" {key}" + ("" if narrow else f" {short}") + " ", style="dim")
        return t


SHORT_NAMES = {"chat": "Chat", "journal": "Journal", "memory": "Memory", "schedule": "Schedule",
               "activity": "Activity", "admin": "Admin"}


class ViewScreen(ResponsiveScreen):
    VIEW = ""
    DEFAULT_CSS = """
    ViewScreen { layout: vertical; }
    ViewScreen .split { height: 1fr; }
    ViewScreen .split > OptionList { width: 34; height: 1fr; border: none; border-right: vkey $panel-lighten-1; }
    ViewScreen .reader { width: 1fr; height: 1fr; padding: 0 2; }
    ViewScreen.narrow .split { layout: vertical; }
    ViewScreen.narrow .split > OptionList { width: 1fr; height: 8; border-right: none; border-bottom: hkey $panel-lighten-1; }
    ViewScreen .hint { height: 1; padding: 0 1; color: $text-muted; }
    ViewScreen .section { text-style: bold; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "app.view('chat')", "Back", show=False)]

    def compose_header(self) -> ComposeResult:
        yield ViewHeader(self.VIEW)

    def on_screen_resume(self) -> None:
        self.reload()
        if (lists := self.query(OptionList)) and self.focused is None:
            lists.first().focus()

    def reload(self) -> None:
        pass

    def data_changed(self) -> None:
        pass


class FileReader(ViewScreen):
    """A list of workspace files and a reader for the highlighted one."""

    HINT = ""
    EMPTY = ""

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(classes="split"):
            yield OptionList(id="files")
            with VerticalScroll(classes="reader", id="reader"):
                yield Markdown(id="doc")
                yield Static(id="plain")
        yield Chrome(self.HINT, classes="hint")

    @property
    def workspace(self):
        return self.app.cfg.workspace  # type: ignore[attr-defined]

    def files(self) -> list[tuple[str, str]]:
        """[(relative path, label)]"""
        raise NotImplementedError

    def reload(self) -> None:
        ol = self.query_one(OptionList)
        current = ol.get_option_at_index(ol.highlighted).id if ol.highlighted is not None and ol.option_count else None
        entries = self.files()
        ol.clear_options()
        ol.add_options([Option(label, id=rel) for rel, label in entries])
        if not entries:
            self.query_one("#plain", Static).update(Text(self.EMPTY, style="dim"))
            self.query_one("#doc", Markdown).display = False
            return
        ids = [rel for rel, _ in entries]
        ol.highlighted = ids.index(current) if current in ids else 0
        self.show(ids[ol.highlighted])

    @on(OptionList.OptionHighlighted)
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.show(event.option.id)

    @work(exclusive=True, group="show")
    async def show(self, rel: str) -> None:
        self.shown = rel
        doc, plain = self.query_one("#doc", Markdown), self.query_one("#plain", Static)
        try:
            data = safefs.read_bytes(self.workspace, rel, MAX_SHOWN_BYTES)
        except (OSError, safefs.UnsafePath) as e:
            doc.display, plain.display = False, True
            plain.update(Text(f"can't read {rel}: {e}", style="red"))
            return
        text = data[:MAX_SHOWN_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_SHOWN_BYTES:
            text += "\n\n… (longer than shown)"
        if rel.endswith(".md"):
            doc.display, plain.display = True, False
            await doc.update(text)
        else:
            doc.display, plain.display = False, True
            plain.update(Content(text))
        self.query_one("#reader", VerticalScroll).scroll_home(animate=False)


class JournalScreen(FileReader):
    VIEW = "journal"
    EMPTY = "No journal entries yet."
    HINT = "↑↓ choose a day · r reload · esc back"
    BINDINGS = [Binding("r", "reload", "Reload", show=False)]

    def files(self) -> list[tuple[str, str]]:
        try:
            names = [n for n, k in safefs.listdir(self.workspace, "journal") if k == "file" and n.endswith(".md")]
        except (OSError, safefs.UnsafePath):
            return []
        out = []
        for n in sorted(names, reverse=True):
            try:
                label = f"{date.fromisoformat(n[:-3]):%a %d %b %Y}"
            except ValueError:
                label = n
            out.append((f"journal/{n}", label))
        return out

    def action_reload(self) -> None:
        self.reload()


class MemoryScreen(FileReader):
    """The character's own documents: the character files first, then any other notes it
    keeps as text. The journal has its own view; a plugin's folder (it has plugin.json)
    is code, and anything else that isn't a text document is the workspace's business."""

    VIEW = "memory"
    EMPTY = "No memory documents yet."
    HINT = "↑↓ choose a file · e open in your editor · r reload · esc back"
    BINDINGS = [Binding("r", "reload", "Reload", show=False), Binding("e", "edit", "Edit", show=False)]
    DOCUMENTS = (".md", ".markdown", ".txt")

    def files(self) -> list[tuple[str, str]]:
        found: list[str] = []

        def walk(rel: str, depth: int) -> None:
            try:
                entries = safefs.listdir(self.workspace, rel)
            except (OSError, safefs.UnsafePath):
                return
            if rel and ("plugin.json", "file") in entries:
                return
            for name, kind in entries:
                path = f"{rel}/{name}" if rel else name
                if name.startswith(".") or path == "journal" or len(found) >= 300:
                    continue
                if kind == "file" and name.lower().endswith(self.DOCUMENTS):
                    found.append(path)
                elif kind == "dir" and depth < 3:
                    walk(path, depth + 1)

        walk("", 0)
        first = [f for f in CHARACTER_FILES if f in found]
        rest = sorted((f for f in found if f not in first), key=lambda p: (p.count("/"), p.lower()))
        return [(f, f) for f in first + rest]

    def action_reload(self) -> None:
        self.reload()

    def action_edit(self) -> None:
        rel = getattr(self, "shown", None)
        if not rel:
            return
        path = self.workspace / rel
        if path.is_symlink() or not path.is_file():
            self.notify("Only regular files in the workspace can be opened.", severity="warning")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with self.app.suspend():
            try:
                subprocess.run([*shlex.split(editor), str(path)], check=False)
            except OSError as e:
                print(f"couldn't start {editor}: {e}")
        self.reload()


class ScheduleScreen(ViewScreen):
    VIEW = "schedule"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with VerticalScroll(classes="reader"):
            yield Static(id="schedule-status")
            yield Static("Recent wakes of its own", classes="section")
            yield Static(id="schedule-recent")
        yield Chrome("esc back · quiet heartbeats are listed here and in Activity, not in the conversation",
                     classes="hint")

    def reload(self) -> None:
        self.data_changed()

    def data_changed(self) -> None:
        app = self.app
        self.query_one("#schedule-status", Static).update(status_panel(app, wide=True))  # type: ignore[arg-type]
        runs = [r for r in reversed(app.model.runs.values()) if r.autonomous and r.origin][:25]  # type: ignore[attr-defined]
        lines: list[RenderableType] = []
        for r in runs:
            lines.append(run_line(r))
        if not lines:
            lines.append(Text("none yet", style="dim"))
        self.query_one("#schedule-recent", Static).update(to_content(lines))


def run_line(r: Run, width: int | None = None) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f"{stamp(r.started)}  ", style="dim")
    t.append(describe.origin_label(r.origin) if r.origin else "run", style="italic" if r.autonomous else "")
    if r.ended is None:
        t.append(" · awake now", style="yellow")
    shown = [c for c in r.calls.values() if c.tool != "turn.finish" and not c.delivered]
    bits = []
    if r.deliveries:
        bits.append(f"{r.deliveries} message{'s' * (r.deliveries != 1)}")
    if shown:
        bits.append(f"{len(shown)} action{'s' * (len(shown) != 1)}")
    if r.ended is not None and r.autonomous and r.quiet:
        bits.append("quiet")
    if bits:
        t.append(" · " + ", ".join(bits), style="dim")
    if r.outcome not in (None, "finished"):
        t.append(f" · {r.outcome} ({r.reason})", style="red" if r.outcome == "failed" else "yellow")
    if r.troubles:
        t.append(f" ✗{r.troubles}", style="bold red")
    if width:
        t.truncate(width, overflow="ellipsis")
    return t


ACTIVITY_FILTERS = [("all", "everything"), ("conversation", "conversations"), ("autonomous", "its own wakes"),
                    ("trouble", "with problems")]


class ActivityScreen(ViewScreen):
    VIEW = "activity"
    BINDINGS = [Binding("f", "filter", "Filter", show=False)]
    MAX_RUNS = 400

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(classes="split"):
            yield OptionList(id="runs")
            with VerticalScroll(classes="reader", id="detail-scroll"):
                yield Static(id="detail")
        yield Chrome(id="activity-hint", classes="hint")

    def on_mount(self) -> None:
        self.filter = 0
        self.selected: str | None = None
        self._timer = None
        self._listed: list[str] = []

    def reload(self) -> None:
        self.rebuild()

    def data_changed(self) -> None:
        if self._timer is None:
            self._timer = self.set_timer(0.5, self._changed)

    def _changed(self) -> None:
        self._timer = None
        self.rebuild()

    def action_filter(self) -> None:
        self.filter = (self.filter + 1) % len(ACTIVITY_FILTERS)
        self.rebuild()

    def runs(self) -> list[Run]:
        key = ACTIVITY_FILTERS[self.filter][0]
        out = []
        for r in reversed(self.app.model.runs.values()):  # type: ignore[attr-defined]
            if not r.origin and not r.calls:
                continue
            if key == "conversation" and r.autonomous or key == "autonomous" and not r.autonomous:
                continue
            if key == "trouble" and not (r.troubles or r.outcome not in (None, "finished")):
                continue
            out.append(r)
            if len(out) >= self.MAX_RUNS:
                break
        return out

    def rebuild(self) -> None:
        ol = self.query_one(OptionList)
        runs = self.runs()
        ids = ["host"] + [r.id for r in runs]
        width = max(10, ol.size.width - 2) if ol.size.width else None
        options = [Option(Text("host events", style="italic"), id="host")]
        options += [Option(run_line(r, width), id=r.id) for r in runs]
        keep = self.selected if self.selected in ids else (ids[1] if len(ids) > 1 else "host")
        ol.clear_options()
        ol.add_options(options)
        ol.highlighted = ids.index(keep)
        self.selected = keep
        self.show_detail()
        label = ACTIVITY_FILTERS[self.filter][1]
        self.query_one("#activity-hint", Chrome).update(
            f"↑↓ choose · f filter (showing {label}) · esc back")

    @on(OptionList.OptionHighlighted)
    def picked(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self.selected:
            self.selected = event.option.id
            self.show_detail()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def show_detail(self) -> None:
        model = self.app.model  # type: ignore[attr-defined]
        if self.selected == "host":
            body = host_detail(list(model.host_events))
        else:
            run = model.runs.get(self.selected)
            body = run_detail(run) if run is not None else Text("")
        self.query_one("#detail", Static).update(body)


def _short(value: object, n: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def run_detail(run: Run) -> RenderableType:
    out: list[RenderableType] = []
    head = Text(f"{describe.origin_label(run.origin)}  ", style="bold")
    head.append(f"{run.started:%Y-%m-%d %H:%M:%S}", style="dim")
    if run.ended:
        secs = (run.ended - run.started).total_seconds()
        head.append(f" → {run.ended:%H:%M:%S} ({secs:.0f}s)", style="dim")
    out.append(head)
    o = run.origin
    if o.get("job_id"):
        out.append(Text(f"job {o['job_id']}", style="dim"))
    if o.get("resumes_run_id"):
        out.append(Text(f"continues {o['resumes_run_id']}", style="dim"))
    end = f"{run.outcome or 'running'}" + (f" ({run.reason})" if run.reason else "")
    out.append(Text(f"{end} · {run.requests} model requests · {len(run.calls)} actions · "
                    f"tokens {run.prompt_tokens:,} in ({run.cache_hit_tokens:,} cached) / {run.completion_tokens:,} out",
                    style="red" if run.outcome == "failed" else "dim"))
    if run.detail:
        out.append(Text(run.detail, style="red"))
    out.append(Text(run.id, style="dim"))
    out.append(Text(""))
    if run.events is None:
        out.append(Text("(older run: only its summary is kept in this session)", style="dim italic"))
        for c in run.calls.values():
            out.append(Text(f"{describe.STATUS_ICON.get(c.status, '·')} {c.description}"))
        return to_content(out)
    for ev in run.events:
        t, d = ev.get("type", ""), ev.get("data") or {}
        at = f"{parse_at(ev.get('at')):%H:%M:%S} "
        line = Text(at, style="dim")
        match t:
            case "tool.started" | "delivery.requested":
                continue
            case "tool.finished":
                c = run.calls.get(d.get("operation_id", ""))
                status = d.get("status")
                style = "red" if status in describe.TROUBLE else "green" if status == "ok" else "yellow"
                line.append(describe.STATUS_ICON.get(status, "·") + " ", style=style)
                line.append(c.description if c else d.get("tool", "?"))
                line.append(f"  {d.get('tool')}", style="dim")
                if status != "ok":
                    line.append(f"  {describe.STATUS_WORD.get(status, status)}", style=style)
                out.append(line)
                if c is not None and c.args:
                    out.append(describe.fields(c.args, indent=4, limit=2000))
                if d.get("reason"):
                    out.append(Text(f"    reason: {d['reason']}", style=style))
                out.append(Text("    →", style="dim"))
                out.append(describe.fields(d.get("result"), indent=6, limit=2000, style="dim" if status == "ok" else style))
                continue
            case "provider.request.finished":
                u = d.get("usage") or {}
                line.append(f"model request {d.get('request_index')}: {d.get('finish_reason')}, "
                            f"{u.get('prompt_tokens', 0):,} in / {u.get('completion_tokens', 0):,} out"
                            + (f", {u['cache_hit_tokens']:,} cached" if u.get("cache_hit_tokens") else "")
                            + (f", {u['latency_ms'] / 1000:.1f}s" if u.get("latency_ms") else ""), style="dim")
            case "message.completed":
                line.append("✎ wrote, not sent:", style="italic")
                out.append(line)
                out.append(Text("    " + (d.get("content") or "").strip().replace("\n", "\n    "), style="italic"))
                continue
            case "delivery.settled":
                line.append(f"→ {d.get('destination')}: {d.get('status')}  ", style="bold" if d.get("status") == "sent"
                            else "yellow")
                line.append(_short(d.get("content") or d.get("receipt") or "", 200))
            case "input.admitted":
                line.append(f"read message {d.get('input_id')}", style="dim")
            case "input.pending":
                line.append(f"message {d.get('input_id')} arrived while awake", style="dim")
            case "exec.started":
                line.append(f"$ {d.get('command')}", style="bold")
                line.append(f"  in {d.get('cwd')}" + ("  (network)" if d.get("network") else "")
                            + f"  · session {d.get('session_id')}", style="dim")
            case "context.note":
                line.append(f"host note: {d.get('kind')}", style="dim")
            case "run.started" | "run.finished" | "run.paused" | "run.failed" | "run.interrupted":
                line.append(t.split(".")[1] + (f" ({d['reason']})" if d.get("reason") else ""),
                            style="red" if t == "run.failed" else "dim")
            case _:
                line.append(t, style="")
                if d:
                    line.append("  " + _short(d), style="dim")
        out.append(line)
    return to_content(out)


def host_detail(events: list[dict]) -> RenderableType:
    out: list[RenderableType] = [Text("Host events, newest first", style="bold"), Text("")]
    for ev in reversed(events[-500:]):
        t, d = ev.get("type", ""), ev.get("data") or {}
        line = Text(f"{stamp(parse_at(ev.get('at'))):>12}  ", style="dim")
        style = "red" if t in ("host.warning", "compaction.failed") or d.get("kind", "").endswith("failed") else ""
        match t:
            case "context.compacted":
                line.append(f"memory compacted: {d.get('tokens_before', 0):,} → {d.get('tokens_after', 0):,} tokens")
            case "exec.exited":
                line.append(f"shell session {d.get('session_id')} {d.get('status')}"
                            + (f" (exit {d['exit_code']})" if d.get("exit_code") is not None else "")
                            + (f" after {d['seconds']:.0f}s" if d.get("seconds") else ""))
            case "input.received":
                s = d.get("sender") or {}
                line.append(f"message from {s.get('name') or s.get('label') or s.get('id')} "
                            f"({s.get('verified_by')}): ", style="bold")
                line.append(_short(d.get("text", ""), 200))
            case "host.warning":
                line.append(f"warning: {d.get('kind')}  {d.get('message', '')}", style="red")
            case "schedule.armed":
                line.append(f"{d.get('schedule')} armed, next {stamp(parse_at(d.get('next_due_at')))}", style="dim")
            case "schedule.due":
                n = (d.get("missed") or 1) - 1  # counts the due occurrence itself
                line.append(f"{d.get('schedule')} due" + (f" ({n} earlier one{'s' * (n != 1)} folded in)" if n else ""))
            case "grant.created" | "grant.revoked":
                line.append(f"{'granted' if t == 'grant.created' else 'revoked'}: {d.get('scope')} for {d.get('subject')}")
            case "approval.decided":
                line.append(f"approval {d.get('decision')}: {', '.join(d.get('scopes') or [])} for {d.get('subject')}")
            case "question.answered":
                line.append("question answered" + (f": {d['choice']}" if d.get("choice") else ""), style="dim")
            case "host.started":
                n = len(d.get("interrupted_runs") or [])
                line.append("whim started" + (f" ({n} interrupted run{'s' * (n != 1)} reconciled)" if n else ""))
            case "compaction.started" | "compaction.summarized" | "compaction.failed" | "compaction.cancelled":
                line.append(f"memory compaction {t.split('.')[1]}", style=style)
                if d.get("next_attempt_not_before"):
                    line.append(f" · next try after {stamp(parse_at(d['next_attempt_not_before']))}", style="dim")
            case "wake.queued" | "wake.cancelled":
                line.append(f"{t}: {d.get('kind') or ''} {d.get('reason') or ''}".strip(), style="dim")
            case "config.changed":
                changes = ", ".join(f"{k} = {v.get('new')!r}" for k, v in (d.get("changes") or {}).items())
                line.append(f"settings changed by {d.get('by')}: {changes}")
            case _:
                line.append(t, style=style)
                if d:
                    line.append("  " + _short(d), style="dim")
        out.append(line)
    if len(out) == 2:
        out.append(Text("none yet", style="dim"))
    return to_content(out)


class AdminScreen(ViewScreen):
    VIEW = "admin"
    DEFAULT_CSS = """
    AdminScreen .row { height: auto; margin-top: 1; }
    AdminScreen .row Button { margin-right: 2; }
    AdminScreen .item { height: auto; }
    AdminScreen .item Static { width: 1fr; height: auto; }
    AdminScreen .item Button { margin-left: 1; }
    AdminScreen .form { height: auto; margin-top: 1; }
    AdminScreen .form Input { width: 1fr; }
    AdminScreen .form Checkbox { width: auto; margin-left: 1; }
    AdminScreen .form Button { margin-left: 1; }
    """
    BINDINGS = [Binding("r", "reload", "Reload", show=False)]

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with VerticalScroll(classes="reader"):
            yield Static("Status", classes="section")
            yield Static(id="admin-status")
            with Horizontal(classes="row"):
                yield Button("Stop wake", id="stop", compact=True)
                yield Button("Interrupt", id="interrupt", compact=True)
                yield Button("Wake", id="wake", compact=True)
                yield Button("Restart whim…", id="restart", compact=True, variant="warning")
            yield Static("Waiting on you", classes="section")
            yield Vertical(id="admin-waiting", classes="item")
            yield Static("Access", classes="section")
            yield Static(id="admin-access")
            with Horizontal(classes="form"):
                yield Input(placeholder="folder to grant, or shell / network / self", id="grant-target", compact=True)
                yield Checkbox("write", id="grant-write", compact=True)
                yield Checkbox("tools on PATH", id="grant-tool", compact=True)
                yield Button("Grant", id="grant", compact=True, variant="primary")
            yield Static("Grants", classes="section")
            yield Vertical(id="admin-grants", classes="item")
            yield Static("Settings", classes="section")
            with Horizontal(classes="form"):
                yield Input(placeholder="key = value   (e.g. heartbeat.interval_minutes = 90)", id="setting", compact=True)
                yield Button("Apply", id="apply", compact=True, variant="primary")
            yield Static(id="admin-settings")
        yield Chrome("r reload · tab moves between controls · esc back", classes="hint")

    def action_reload(self) -> None:
        self.reload()

    def data_changed(self) -> None:
        self.render_status()

    def render_status(self) -> None:
        app = self.app
        link, snap = app.link, app.link.snapshot  # type: ignore[attr-defined]
        t = Text()
        t.append(f"connection: {link.state}" + (f" ({link.problem})" if link.problem else "") + "\n")
        t.append(f"home: {app.cfg.home}\n", style="dim")  # type: ignore[attr-defined]
        if snap:
            t.append(f"awake: {snap.get('active_run') or 'no'} · pending messages {snap.get('pending_inputs', 0)}"
                     f" · queued wakes {snap.get('queued_wakes', 0)}")
            if snap.get("draining_for_restart"):
                t.append("\nrestart requested: waiting for the current wake to end", style="yellow")
        if app.model.skipped:  # type: ignore[attr-defined]
            t.append(f"\n{len(app.model.skipped)} event(s) too large to show were skipped", style="yellow")
        self.query_one("#admin-status", Static).update(t)

    @work(exclusive=True, group="admin")
    async def reload(self) -> None:
        app = self.app
        self.render_status()
        req = app.link.request  # type: ignore[attr-defined]
        ok_a, msg_a, appr = await req("GET", "/v1/approvals")
        ok_q, _, qs = await req("GET", "/v1/questions")
        waiting = self.query_one("#admin-waiting", Vertical)
        await waiting.remove_children()
        self.actions: dict[str, tuple] = {}
        rows = []
        for a in appr.get("pending", []) if ok_a else []:
            rows.append((Text("⚑ " + (a.get("summary") or a.get("subject", "")).replace("WARNING:", "⚠")), "Review",
                         ("approval", a)))
        for q in qs.get("pending", []) if ok_q else []:
            rows.append((Text("? " + q.get("text", "")), "Answer", ("question", q)))
        if not ok_a:
            await waiting.mount(Static(Text(msg_a, style="red")))
        elif not rows:
            await waiting.mount(Static(Text("nothing", style="dim")))
        for i, (label, verb, action) in enumerate(rows):
            self.actions[f"w{i}"] = action
            await waiting.mount(Horizontal(Static(label), Button(verb, id=f"w{i}", compact=True), classes="item"))
        grants = self.query_one("#admin-grants", Vertical)
        await grants.remove_children()
        glist = appr.get("grants", []) if ok_a else []
        for i, g in enumerate(glist):
            self.actions[f"g{i}"] = ("revoke", g)
            when = stamp(parse_at(g.get("created_at"))) if g.get("created_at") else ""
            await grants.mount(Horizontal(Static(Text(f"{g.get('scope')}  for {g.get('subject')}  ") +
                                                 Text(when, style="dim")),
                                          Button("Revoke", id=f"g{i}", variant="error", compact=True), classes="item"))
        if ok_a and not glist:
            await grants.mount(Static(Text("none", style="dim")))
        ok, msg, acc = await req("GET", "/v1/access")
        t = Text()
        if ok:
            for r in acc.get("roots", []):
                t.append(f"{r['access']:<10} ", style="bold" if r["access"] == "read-write" else "")
                t.append(f"{r['path']}", style="")
                t.append(f"  ({r['kind']}{', on PATH' if r['path'] in acc.get('tools_on_path', []) else ''})\n",
                         style="dim")
            t.append(f"shell commands: {'allowed' if acc.get('shell') else 'not allowed'} · network for them: "
                     f"{'allowed' if acc.get('shell_network') else 'not allowed'}")
            for s in acc.get("sessions", []):
                t.append(f"\nshell session {s.get('session_id')} {s.get('status')}: {_short(s.get('command'), 80)}",
                         style="dim")
        else:
            t.append(msg, style="red")
        self.query_one("#admin-access", Static).update(t)
        ok, msg, conf = await req("GET", "/v1/config")
        t = Text()
        if ok:
            waiting_keys = set(conf.get("waiting_for_restart", []))
            for k, v in conf.get("settings", {}).items():
                t.append(f"{k} = ", style="dim")
                t.append(json.dumps(v) if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else str(v))
                if k in waiting_keys:
                    t.append("   ← saved, waits for a restart", style="yellow")
                t.append("\n")
        else:
            t.append(msg, style="red")
        self.query_one("#admin-settings", Static).update(t)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        app = self.app
        bid = event.button.id or ""
        if bid in ("stop", "interrupt"):
            app.control(bid)  # type: ignore[attr-defined]
        elif bid == "wake":
            app.wake()  # type: ignore[attr-defined]
        elif bid == "restart":
            self.confirm_restart()
        elif bid == "grant":
            self.grant()
        elif bid == "apply":
            self.apply_setting()
        elif bid in getattr(self, "actions", {}):
            kind, row = self.actions[bid]
            if kind == "approval":
                app.review_approval(row)  # type: ignore[attr-defined]
            elif kind == "question":
                app.answer_question(row)  # type: ignore[attr-defined]
            elif kind == "revoke":
                self.revoke(row)

    @on(Input.Submitted, "#setting")
    def setting_submitted(self) -> None:
        self.apply_setting()

    @on(Input.Submitted, "#grant-target")
    def grant_submitted(self) -> None:
        self.grant()

    def _post(self, path: str, body: dict, done: str) -> None:
        async def go() -> None:
            ok, msg, _ = await self.app.link.request("POST", path, body)  # type: ignore[attr-defined]
            self.app.report(ok, msg, done, "failed")  # type: ignore[attr-defined]
            self.reload()

        self.run_worker(go(), group="admin-post")

    def confirm_restart(self) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                self._post("/v1/restart", {"reason": "requested by the human (tui)"},
                           "restart requested: whim restarts after the current wake")

        self.app.push_screen(ConfirmScreen(
            "Restart whim?", "whim stops starting new work, lets the current wake finish, checks that a new process "
            "would start, then exits for its systemd service to restart it. Only available under the service.",
            "Restart", danger=True), answered)

    def revoke(self, grant: dict) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                self._post(f"/v1/grants/{grant['id']}/revoke", {}, "revoked")

        self.app.push_screen(ConfirmScreen("Revoke this grant?", f"{grant.get('scope')} for {grant.get('subject')}\n\n"
                                           "Shell sessions that rely on it are stopped.", "Revoke", danger=True),
                             answered)

    def grant(self) -> None:
        target = self.query_one("#grant-target", Input).value.strip()
        if not target:
            self.notify("Give a folder path, or shell, network or self.", severity="warning")
            return
        if target not in ("shell", "network", "self"):
            target = os.path.abspath(os.path.expanduser(target))
        body = {"action": "grant", "target": target, "write": self.query_one("#grant-write", Checkbox).value,
                "tool": self.query_one("#grant-tool", Checkbox).value}

        async def go(confirm: bool) -> None:
            status_ok, msg, data = await self.app.link.request("POST", "/v1/access",  # type: ignore[attr-defined]
                                                               {**body, "confirm": confirm})
            if not status_ok and data.get("warnings") and not confirm:
                warn = "\n".join(f"⚠ {w}" for w in data["warnings"])

                def answered(yes: bool | None) -> None:
                    if yes:
                        self.run_worker(go(True), group="admin-post")

                self.app.push_screen(ConfirmScreen(f"Grant {target}?", warn, "Grant anyway", danger=True), answered)
                return
            self.app.report(status_ok, msg, "granted", "not granted")  # type: ignore[attr-defined]
            if status_ok:
                self.query_one("#grant-target", Input).value = ""
            self.reload()

        self.run_worker(go(False), group="admin-post")

    def apply_setting(self) -> None:
        raw = self.query_one("#setting", Input).value
        key, eq, value = raw.partition("=")
        if not eq or not key.strip():
            self.notify("Write it as key = value.", severity="warning")
            return

        async def go() -> None:
            ok, msg, out = await self.app.link.request(  # type: ignore[attr-defined]
                "POST", "/v1/config", {"changes": {key.strip(): value.strip()}})
            if ok:
                note = ("in effect now" if out.get("applied_now") else
                        "saved; takes effect after a restart" if out.get("saved_waiting_for_restart") else
                        "already set" if not out.get("changed") else "saved")
                self.notify(f"{key.strip()}: {note}")
                self.query_one("#setting", Input).value = ""
            else:
                self.app.report(False, msg, "", "not changed")  # type: ignore[attr-defined]
            self.reload()

        self.run_worker(go(), group="admin-post")
