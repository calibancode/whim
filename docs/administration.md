# Administering whim

Commands for the designated human. Each acts on the running daemon when there is one;
`whim config` also works offline. The character has matching tools (`host_access`,
`host_config`, `host_restart`) backed by the same code; whatever widens its reach or
changes cost or trust becomes an approval request (`whim approvals`, `whim approve`).

## Access: `whim access`

```sh
whim access                                  # what the character can use
whim access grant ~/code/project             # a folder, read-only
whim access grant ~/code/project --write     # read-write
whim access grant ~/.cargo/bin --tool        # also put its programs on PATH for commands
whim access grant shell                      # run sandboxed shell commands at all
whim access grant network                    # network for those commands
whim access grant self --write               # edit what whim itself runs (always readable)
whim access revoke TARGET [--write]          # --write takes back only write access
```

You can grant any existing folder, read-only or read-write, except `/`, `/proc`, `/sys`,
`/dev`, and anything inside the whim home or its credentials folder. Revoking access
kills shell sessions that relied on it.

**Always masked.** However broad a grant, these hold for the file tools and for shell
commands:

- Hidden: the whim home (its database and control socket; the workspace stays visible),
  the credentials folder, `/run`, and secret stores under your home folder (`~/.ssh`,
  `~/.gnupg`, `~/.aws`, keyrings, `~/.netrc`...). Granting one of these places itself
  makes it visible. Hidden means not mounted at all: a read-only mount would still let
  commands connect to sockets in it.
- Read-only without `self --write`: what the daemon loads when it starts. That is its
  source, the Python environments and import paths it runs from, and its systemd unit.

**Warnings.** These grants are allowed but ask for confirmation (`--yes` skips the
question). When the character asks for one, the warning appears in the approval request.

- **Writing where code runs outside the sandbox:** `~/.local/bin`, `~/.cargo/bin`,
  `~/.config`, systemd folders, and the daemon's `PATH` entries under your home folder.
- **Your whole home folder.** App tokens under `~/.config` are visible, for instance.
- **A secret store, named directly.**
- **Anything under `/run`.** `/run/user/<uid>` holds your systemd user manager, D-Bus
  and agent sockets. Even read-only, that lets commands act as you, unsandboxed.
- **`/tmp` itself.** It holds X11 and ssh-agent sockets.

The masks protect whim's own boundaries; they don't make other writable places safe.
Write access to any folder is write access to whatever code in it later runs outside
the sandbox: a project's scripts, `~/.bashrc`, other programs' units. The warnings name
common cases, not all of them.

**Tools.** Shell commands see only `/usr`, a few `/etc` files, a private `/tmp` (also
`$HOME`) and the granted folders. `--tool` puts a granted folder first on `PATH`. Grant a
tool's `bin` folder rather than its whole data folder. If its programs are links into
another folder (pipx, for example), the grant reply lists those folders under
`unreachable_link_targets`; grant them read-only as well. Tools that find their data
through `$HOME` (rustup's `~/.rustup`, for instance) don't find it there.

## Settings: `whim config`

```sh
whim config show [SECTION] [--json]
whim config get KEY                          # value, type, default, when it applies
whim config set KEY=VALUE [KEY=VALUE ...]    # validated together, saved atomically
whim config validate                         # does config.toml load and whim import?
```

Changes edit `config.toml` in place, keeping comments and anything else written there;
a structure that can't be edited safely (an inline or dotted table) is reported, not
rewritten. Each change says whether it applied at once or waits for a restart. API keys
are never here: see `whim auth`.

## Restarts: `whim restart`

`whim restart [--reason TEXT]` works only when whim runs as its systemd user service
(`whim service install`). whim stops starting new work (input is still accepted), lets
the current wake finish, then checks in a fresh process that the saved configuration
loads, the source imports and the provider can be set up. If that fails nothing
restarts and the reason is recorded. Otherwise whim exits with status 76 and systemd
starts it again about 10 seconds later. A foreground `whim run` refuses; restart it by
hand.

If whim fails at startup after a restart, systemd keeps retrying every 10 seconds; check
`journalctl --user -u whim`. There is no automatic rollback of edited source.
