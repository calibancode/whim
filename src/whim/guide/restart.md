# Restart

Source: `<package>/restart.py`. host_restart(reason, note) restarts the harness after
your current wake ends, e.g. to apply settings marked `restart` or changed source.

1. It's recorded durably; new work waits (messages are still accepted).
2. After your wake ends, a fresh interpreter checks that the configuration loads and the
   code imports. If not, nothing restarts and you're told why.
3. The daemon exits; its systemd service starts it again. Shell sessions end.
4. You wake with a note: what changed and your note to yourself.

Only works when the harness runs as its systemd user service; otherwise your human has
to restart it.
