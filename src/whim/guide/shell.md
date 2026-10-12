# Shell

Source: `<package>/shell.py`. Needs your human's "shell" permission (host_access).

shell_exec runs a command in a bubblewrap sandbox that sees /usr, a few /etc files, a
private /tmp ($HOME), and the folders host_access lists, at their real paths (read-only
ones stay read-only). PATH is /usr's plus any tool folders your human granted (host_access
request with tool true, e.g. ~/.cargo/bin); a tool whose program is a link into an
ungranted folder, or that looks for its data under $HOME, won't work until that is
granted or set up. No network unless granted. It waits up to wait_seconds: a quick
command returns its output and exit code; a longer one returns a session id.

- shell_write session_id [input]: read new output; with tty sessions, also type input
  ("\n" Enter, "\u0003" Ctrl-C, "\u0004" Ctrl-D).
- shell_kill session_id [signal]; shell_list.
Sessions outlive a wake but not the harness: a restart ends them (you're told).
Limits (exec.* settings): timeouts, retained output, memory, file size, concurrent
sessions. Output beyond what one call returns is elided in the middle.
