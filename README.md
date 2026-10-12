# whim

Whim is a harness for autonomous AI characters.

It runs a single, evolving character with continuous history and a persistent memory. The character can maintain its own memory, pursue goals, schedule tasks, create and hot-load tools, and decide when (or whether) to communicate.

Whim is not intended to be your personal assistant. Expect rough edges, odd behavior, and hints of unprompted anthropomorphism.

## Getting started

Requires Linux, Python 3.12+, and a [DeepSeek API key](https://platform.deepseek.com/). Sandboxed plugins additionally require [bubblewrap](https://github.com/containers/bubblewrap).

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[tui]'   # or `pip install -e .` without the terminal UI

whim init
whim auth set
```

Start the daemon with `whim run`, then open another terminal and run `whim tui` ([the terminal interface](docs/tui.md)), or `whim chat` for a plain line-based chat.

See `whim --help` for available commands, [administration](docs/administration.md) for access, settings and restarts, and [the client protocol](docs/client_protocol.md) for writing your own interface.

## License

AGPL-3.0-only. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
