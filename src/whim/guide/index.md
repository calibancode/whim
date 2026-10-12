# Guide to yourself and your harness

Short topics, read on demand with host_guide(topic). Each points into the source, which
you can read with file_read: `<package>` below is the `package` path this tool returns.
Reading is always allowed; changing the implementation needs your human's permission.

- architecture   how a wake runs: the ledger, context, tools, the single worker
- compaction     how your working memory is condensed when it grows large
- configuration  settings: reading and changing them (host_config)
- access         folders beyond your workspace, and how to ask for more (host_access)
- shell          running commands: sessions, terminals, limits (shell_exec)
- restart        restarting the harness to apply changes (host_restart)
- plugins        making tools of your own
- recovery       what happens when the harness stops unexpectedly
