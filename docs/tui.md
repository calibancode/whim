# The terminal interface: `whim tui`

A full-screen client for the running daemon: the conversation, what the character does
on its own, and the controls from [administration](administration.md), in one place.
`whim chat` remains as a plain line-based fallback.

```sh
pip install -e '.[tui]'      # Textual is an optional dependency
whim tui                     # --name NAME overrides what the character is called
```

The TUI is only a client of the [client protocol](client_protocol.md). It can start
before the daemon and waits for it. Quitting (ctrl+q) never stops the daemon. It writes
nothing into the whim home: its draft and any unconfirmed messages are kept in
`$XDG_STATE_HOME/whim/` (default `~/.local/state/whim/`), owner-only.

## The conversation

- **Messages.** Your messages are on the right. The character's are on the left,
  rendered as Markdown. Only messages actually delivered to you appear here. Text the
  character wrote but didn't send is never shown as a message. It appears, marked as
  unsent, inside the activity entry for that moment.
- **Activity entries.** Tool calls are grouped into one compact line per stretch of
  work, e.g. `▸ edited MEMORY.md, wrote in the journal`. Click one, or move to it with
  ctrl+↑/↓ and press enter, to see each call's arguments, result or error.
- **Its own time.** Work from a heartbeat or scheduled job appears as an entry marked
  `◇ heartbeat` (or `◇ scheduled job`), and messages from it are tagged `· during a
  heartbeat`. A heartbeat that only looked around and did nothing stays out of the
  conversation; it is listed under Schedule and Activity.
- **Scrolling.** The view follows new messages only while you're at the bottom. If you
  have scrolled back, nothing moves; a `↓ N new messages` bar appears, and ctrl+end
  returns you to the end and resumes following. On resize, text rewraps and the message
  you were reading stays at the top. Older history loads as you scroll up.
- **Status line.** Connection (green dot: connected), whether the character is
  resting or awake (and what it did last), anything waiting on you, and the next
  heartbeat.

## Selecting and copying

Text in the conversation, Journal, Memory and Activity can be selected with the mouse:

- **Drag** to select, across wrapped lines and from one message to the next. Dragging
  past the top or bottom edge scrolls; so does the wheel while dragging.
- **Double-click** selects a word, **triple-click** a whole line (as written, however
  it wraps on screen).
- **ctrl+c** (or a right-click) copies the selection; **escape** clears it. A wrapped
  paragraph copies as one line. Clicking text doesn't take focus from the composer, and
  ctrl+c copies the composer's own selection when that is the newer one.
- While text in the conversation is selected, the view stops following new messages, so
  the selection stays where it is; a `↓ N new` bar counts what arrived. Clearing the
  selection (escape, a click) catches up. Resizing keeps the selection on the same text.

Copying goes to the system clipboard with `wl-copy`, `xclip`, `xsel` or `pbcopy` when
one is available. Inside tmux it also goes through `tmux load-buffer -w`, which tmux
forwards to your terminal's clipboard when it can; otherwise, and over SSH, it asks the
terminal with OSC 52, which some terminals refuse or have to be allowed. The message
after a copy says which happened: `copied` is confirmed, `sent to the terminal's
clipboard` is not. Either way, ctrl+v in the composer pastes it. On Linux, selected
text also becomes the primary selection, for middle-click paste.

Holding **shift** while dragging uses your terminal's own selection instead (in most
terminals, and in tmux).

## Writing

The composer is a multiline editor. Enter starts a new line, and **ctrl+g** sends.
ctrl+enter also sends in terminals that report it (those with the kitty keyboard
protocol). Undo and redo are ctrl+z and ctrl+y; select with shift+arrows; cut and copy
are ctrl+x and ctrl+c; paste with your terminal's paste, or ctrl+v for what was copied
in the TUI. The composer grows up to a limit, then scrolls.

**Sending is exactly-once.** A message is shown at once with its state: `sending…`,
then confirmed. If whim isn't reachable it stays `waiting for whim` and is sent when it
is. If the connection fails mid-send, it is retried with the same message id, so it
can't arrive twice. Only a refusal from whim (such as a message over 1 MiB) drops it,
and then the text goes back into an empty composer. Unconfirmed messages and your draft
survive quitting and are picked up the next time you open the TUI.

The command palette (ctrl+p) can also **steer** with the composer text (reach the
character right away, even mid-wake) or send it **for later** (held until the current
wake ends). It also has stop, interrupt and wake.

## Approvals and questions

When the character asks for approval or asks you a question, a bar appears above the
composer and the status line shows `⚑ waiting`. **ctrl+o** opens the oldest one:

- **Approval:** exactly what was requested, with its warnings, the scopes and the
  subject. It takes a button press to decide; focus starts on "Later". Decisions go
  through whim's approval API; nothing typed in the conversation approves anything.
- **Question:** pick a choice, write an answer, or both.

## Other views

| key | view |
| --- | --- |
| f2 | **Journal**: the character's dated entries |
| f3 | **Memory**: IDENTITY, SOUL, USER, MEMORY and the character's other notes (`.md`, `.txt`; not the journal or plugin folders); `e` opens one in `$VISUAL`/`$EDITOR` |
| f4 | **Schedule**: next heartbeat, standing cues, scheduled jobs, recent wakes of its own |
| f5 | **Activity**: every run with its tool calls, results, errors, private text and token use, plus host events (wakes, compaction, shell sessions, warnings); `f` filters |
| f6 | **Administration**: status; stop, interrupt, wake and restart; pending approvals and questions; access, grants and settings |

Escape returns to the conversation. alt+1–6 also switch views in terminals that report
them. On terminals 120 columns or wider, ctrl+b shows an inspector beside the
conversation; on narrower ones it opens the Schedule view. f1 lists the keys.

Journal and Memory read the workspace directly (read-only, not following links); the
TUI must run on the same machine as the daemon, as the control socket already requires.

## Limits

- Standing cues are shown as of the character's last `heartbeat_manage` call in the
  ledger; the API has no endpoint that lists them.
- The TUI replays the whole event ledger when it starts (thousands of events take a
  fraction of a second), and keeps only the most recent runs' full details in memory.
- Messages are rendered as selectable text, which means wrapped list items have no
  hanging indent; lists with long items are spaced out instead. Copying copies the text
  as shown (bullets included), not the Markdown source.
