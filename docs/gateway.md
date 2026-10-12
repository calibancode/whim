# Remote access: `whim-gateway`

The control socket ([client protocol](client_protocol.md)) only trusts local processes of the
designated human's uid. `whim-gateway` is a small, separate process that lets one more client
in: the whim Android app, over Tailscale. It does not change the daemon or its protocol.

```
phone ──Tailscale (WireGuard)──▶ tailscaled ──Serve (HTTPS)──▶ gateway.sock ──▶ whim.sock
 app + device token         identity header set by Serve   whim-gateway      the daemon
```

## The security boundary

A request is relayed only if all of these hold, checked in this order; anything missing,
unexpected or malformed is refused (fail closed):

1. **The connecting process is tailscaled.** The gateway listens on a Unix socket in an
   owner-only directory (`$XDG_RUNTIME_DIR/whim-gateway/`), never on TCP, and reads the
   peer's uid from the kernel (`SO_PEERCRED`). Only uid 0 (tailscaled) is accepted.
   `SO_PEERCRED` proves only that the process that connected the socket ran as uid 0 at
   the time; not which program it is, nor who is behind it. That is enough to stop forged
   headers: other local users can't reach the socket at all, and processes running as you
   (including the character's sandboxed shell commands) can reach it but are refused before
   any header is read. Only root could impersonate tailscaled, and root controls everything.
   It does **not** keep local processes out entirely: anything on this host that can open a
   connection to `https://<host>.<tailnet>.ts.net` goes through Serve, and Serve vouches for
   the host's own owner, i.e. you. For those, check 3 is the barrier (the character's shell
   has no network unless you grant it, and a code or token never appears on the host).
2. **The tailnet identity is yours.** Serve strips any `Tailscale-User-Login` header a client
   sends and sets its own. The gateway requires exactly one, equal (case-insensitively) to the
   configured `allowed_login`. Other tailnet users, devices shared in from other tailnets,
   tagged devices and Funnel traffic carry no matching identity and are refused.
   Tailnet membership alone is not enough.
3. **The device is paired.** A bearer token issued once, by pairing. `whim-gateway pair`
   shows a one-time code (60 bits, valid 10 minutes, withdrawn after 5 wrong guesses) that
   is itself only accepted through checks 1 and 2. The phone stores the token encrypted with
   a non-exportable Android Keystore key. The host stores only SHA-256 hashes, in
   `$XDG_STATE_HOME/whim-gateway/devices.json` (0600): outside the whim home, so never in the
   character's workspace, model context, tool arguments or ledger. Tokens are never logged.

Then **only these routes are relayed**, each re-validated:

| Route | Body accepted (unknown fields → 400) |
| --- | --- |
| `GET /v1/snapshot`, `GET /v1/questions`, `GET /v1/approvals`, `GET /v1/control` | — |
| `GET /v1/events?after=<n>&follow=0\|1` (and `Last-Event-ID`) | — (native stream, passed through as is) |
| `POST /v1/input` | `text`, `client_message_id`, `when` |
| `POST /v1/questions/{id}/answer` | `choice`, `text`, `client_message_id` |
| `POST /v1/approvals/{id}` | `decision`, `standing` |
| `POST /v1/control` | `action` (`stop` / `interrupt` / `steer`), `text`, `client_message_id` |

Bodies are rebuilt from those fields alone before they reach the daemon, so a remote client
can't even attempt to send `principal_id`, `origin` or `sender`; the daemon attaches the
principal itself, exactly as for the TUI. Path ids must match `[A-Za-z0-9_-]{1,80}`. Bodies
are capped at 2 MiB, at most 8 event streams are open at once, and a non-following backlog
is gzip-compressed. Settings, access grants, grant revocation, restarts, wakes and the AG-UI
projection are **not** reachable through the gateway; use the TUI or CLI on the host.

The gateway has a few routes of its own: `POST /gateway/v1/pair`, `POST /gateway/v1/unpair`
(this device), `GET /gateway/v1/info` (the character's name), and read-only
`GET /gateway/v1/documents` / `GET /gateway/v1/document?path=…` serving only `IDENTITY.md`,
`SOUL.md`, `USER.md`, `MEMORY.md` and `journal/*.md`, read without following links.

Errors: `401` this device isn't paired; `403` the peer or identity was refused; `404` not a
relayed route; `400`/`413` a bad or oversized body; `503 {"reason": "daemon_unreachable"}`
whim isn't running (the request certainly wasn't received); `502 {"reason": "outcome_unknown"}`
the connection to whim failed mid-request (it may have acted). Everything else is whim's own
answer, passed through.

What this does not protect against: root on the host; someone holding your unlocked phone
(the app is as trusted as your terminal); and approvals remain approvals, so read them on
the phone as carefully as in the TUI. Nothing said in conversation approves anything.

## Activating it

Run as yourself (not root), on the host where whim runs:

```sh
cd /path/to/whim && .venv/bin/pip install -e .        # puts whim-gateway in the venv
whim-gateway init --login you@example.com              # your Tailscale login; see `tailscale status`
whim-gateway check                                     # before starting: expect the socket problem only
```

Run it as a user service next to `whim.service`
(`~/.config/systemd/user/whim-gateway.service`):

```ini
[Unit]
Description=whim remote gateway (for Tailscale Serve)
After=whim.service

[Service]
ExecStart=/path/to/whim/.venv/bin/python -P -m whim.gateway serve
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now whim-gateway
whim-gateway check
```

The socket lives under `/run/user/<uid>`, which exists while you're logged in or when
lingering is on (`loginctl enable-linger`), as it already must be for `whim.service`.

Then point Tailscale Serve at it (HTTPS certificates must be enabled for the tailnet; the
CLI offers to). Serve, **never Funnel**:

```sh
sudo tailscale serve --bg unix:$XDG_RUNTIME_DIR/whim-gateway/gateway.sock
tailscale serve status        # https://<host>.<tailnet>.ts.net → unix:…/gateway.sock
```

(`sudo` because changing Serve needs root unless you've run `sudo tailscale set --operator=$USER`.)
This serves on port 443 of the host's tailnet name only. `tailscale serve reset`, or
`tailscale serve --https=443 off`, removes it again.

Check the boundary before pairing anything (expected answers in comments):

```sh
curl -s --unix-socket $XDG_RUNTIME_DIR/whim-gateway/gateway.sock \
  -H 'Tailscale-User-Login: you@example.com' http://x/v1/snapshot       # 403: not tailscaled
curl -s https://<host>.<tailnet>.ts.net/v1/snapshot                      # 401: you, but unpaired
journalctl --user -u whim-gateway -n 5                                    # no 403 for the second
```

The second request proves Serve reaches the gateway as uid 0 and passes your identity; from
another tailnet user's device the same request gets `403`.

Pair the phone: install the app, connect the Tailscale app, run `whim-gateway pair`, and enter
`https://<host>.<tailnet>.ts.net` and the code. `whim-gateway devices` lists paired devices;
`whim-gateway revoke <id>` unpairs one at once.

## Development

`devtools/dev-stack.sh` in the Android repository runs a throwaway whim home with the offline
script provider, this gateway with a development config, and a loopback-only stand-in for
Serve. Its config sets `trusted_proxy_uids` to your own uid, which is exactly what a real
configuration must never do; `whim-gateway check` reports it as a problem.
