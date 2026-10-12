# The web plugin

`plugins/web` lets the character research the public web: `web_search` finds sources and
`web_fetch` reads them (web pages as Markdown, PDFs, source files, JSON and XML APIs),
a window at a time, with `find` to locate passages. It reads with GET requests only and
refuses private and local network addresses. It has no accounts, cookies, credentials or
browser. How it works and what it can't do: [plugins/web/README.md](../plugins/web/README.md).

It is an ordinary plugin: it runs in the plugin sandbox with the `net` permission only,
and keeps its cache in its own data folder. Installing it changes nothing about the
character's other access.

**Third parties.** Searches go to [Exa](https://exa.ai) or [Parallel](https://parallel.ai),
search services for AI agents, used through their free, keyless tiers. When a page needs
JavaScript or blocks automated clients, its URL goes to their reader services too. These
companies see those queries and URLs. Everything else is fetched directly from the site.

## Installing it

Plugins are installed by the character from its workspace. Copy the folder there:

```sh
mkdir -p ~/.local/share/whim/workspace/plugins
cp -r plugins/web ~/.local/share/whim/workspace/plugins/
```

(Adjust the path if your whim home is elsewhere: the workspace is `<home>/workspace`.)

Then ask the character to install and activate it, for example: "I put a web plugin in
plugins/web. Install it with plugin_manage and activate it if you'd like it." It calls
`plugin_manage install` (the code is copied and checked; nothing runs with network yet)
and then `plugin_manage activate`, which asks you for approval of `plugin.run` and
`net` for this exact version.

Approve it with `whim approvals` and `whim approve <id>`, or from the Administration
screen of `whim tui` (f6). The tools appear immediately, even in the middle of a wake.

To take it away again: `whim revoke <grant id>` (see `whim approvals`), or ask the
character to `plugin_manage remove` it. Its cache stays in
`<home>/plugins/data/web` until removed by hand.

## Changing it

The installed copy is frozen. If you or the character edit the files in
`workspace/plugins/web`, installing again creates a new version, which needs approval
again before it replaces the old one.
