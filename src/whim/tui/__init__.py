"""Full-screen terminal client (`whim tui`). Needs the optional `tui` extra (Textual)."""

from __future__ import annotations


def main(cfg, name: str | None = None) -> None:
    try:
        import textual  # noqa: F401
    except ImportError:
        raise SystemExit("whim tui needs Textual: pip install 'whim[tui]' (or use `whim chat`)") from None
    from whim.tui.app import run

    run(cfg, name)
