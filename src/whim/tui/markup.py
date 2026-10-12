"""Text for the TUI as Textual `Content`, which is what makes it selectable.

Textual's text selection maps a mouse position to a (character, logical line) offset
recorded in the rendered segments. Only Textual `Content` carries those offsets: a Rich
renderable (a Group, Rich's Markdown) renders fine but can't be selected or copied. Because
the offsets refer to logical lines, a selection survives rewrapping on resize, and a
wrapped paragraph copies as one line.

`chat_content` renders the Markdown a character writes in a message as one `Content`.
Textual's Markdown widget would do this with a widget per block, which measured ten times
slower to mount and to reflow on resize. The price of a single `Content`: wrapped list
items don't get a hanging indent, so lists with long items are spaced out instead.
"""

from __future__ import annotations

from typing import Iterable

from markdown_it import MarkdownIt
from markdown_it.token import Token
from rich.text import Text
from textual.content import Content

# breaks: a single newline is a line break, as people mean it in chat.
_MD = MarkdownIt("commonmark", {"breaks": True}).enable("strikethrough").enable("table")
INLINE = {"strong": "bold", "em": "italic", "s": "strike", "link": "underline"}
HEADINGS = {"h1": "bold underline", "h2": "bold", "h3": "bold italic"}
CODE = "$text-accent"
QUOTE = "▌ "


def to_content(parts: Text | Content | Iterable[Text | Content | str]) -> Content:
    """Rich Text, or several pieces joined by newlines, as one `Content`."""
    if isinstance(parts, Content):
        return parts
    if isinstance(parts, Text):
        return Content.from_rich_text(parts)
    return Content("\n").join(p if isinstance(p, Content) else Content.from_rich_text(p) for p in parts)


def _inline(token: Token) -> Content:
    parts: list[str | tuple[str, str]] = []
    styles: list[str] = []
    for child in token.children or []:
        kind = child.type
        style = " ".join(styles)
        if kind in ("text", "html_inline"):
            parts.append((child.content, style) if style else child.content)
        elif kind == "code_inline":
            parts.append((child.content, f"{style} {CODE}".strip()))
        elif kind in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif kind == "image":
            parts.append(child.content or str(child.attrGet("src") or ""))
        elif kind.endswith("_open"):
            styles.append(INLINE.get(kind[:-5], ""))
        elif kind.endswith("_close") and styles:
            styles.pop()
    return Content.assemble(*parts)


LONG_ITEM = 60  # characters: an item this long wraps in a typical bubble


def _has_long_items(tokens: list[Token], start: int) -> bool:
    """Without hanging indents, wrapped items run together; a list with long items is
    spaced out instead, one blank line between items."""
    depth = 0
    for tok in tokens[start:]:
        depth += tok.nesting
        if tok.type == "inline" and len(tok.content) > LONG_ITEM:
            return True
        if depth == 0:
            return False
    return False


def chat_content(text: str) -> Content:
    """A message's Markdown as one `Content`: headings, emphasis, code, lists, quotes."""
    source = text.split("\n")
    blocks: list[tuple[Content, bool]] = []  # (block, joined to the previous one by a single newline)
    lists: list[list] = []  # [ordered, next number, items so far, loose] per open list
    item_start = False
    quote = 0
    tokens = _MD.parse(text)

    def add(block: Content, tight: bool = False) -> None:
        if quote:
            block = Content("\n").join(Content.assemble((QUOTE * quote, "dim"), line)
                                       for line in block.split("\n", allow_blank=True))
        blocks.append((block, tight))

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        kind = tok.type
        indent = "  " * max(0, len(lists) - 1)
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append([kind == "ordered_list_open", int(tok.attrGet("start") or 1), 0, _has_long_items(tokens, i)])
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            item_start = True
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "heading_open":
            add(_inline(tokens[i + 1]).stylize(HEADINGS.get(tok.tag, "bold")))
            i += 2
        elif kind == "paragraph_open":
            body = _inline(tokens[i + 1])
            i += 1
            if lists:
                ordered, number, _, loose = lists[-1]
                marker = f"{number}. " if ordered else "• "
                if item_start:
                    # A list's first item starts a new block, unless the list is nested.
                    first = lists[-1][2] == 0
                    lists[-1][1] += 1
                    lists[-1][2] += 1
                    prefix, item_start = indent + marker, False
                    add(Content.assemble(prefix, body),
                        tight=tok.hidden and not loose and (not first or len(lists) > 1))
                else:
                    add(Content.assemble(indent + " " * len(marker), body), tight=tok.hidden)
            else:
                add(body)
        elif kind in ("fence", "code_block"):
            add(Content.styled(tok.content.rstrip("\n"), CODE))
        elif kind == "hr":
            add(Content.styled("─" * 24, "dim"))
        elif kind == "table_open" and tok.map:
            add(Content("\n".join(source[tok.map[0]:tok.map[1]])))
            while i < len(tokens) and tokens[i].type != "table_close":
                i += 1
        elif kind == "html_block":
            add(Content(tok.content.rstrip("\n")))
        i += 1
    if not blocks:
        return Content(text)
    out = [blocks[0][0]]
    for block, tight in blocks[1:]:
        out.append(Content("\n" if tight else "\n\n"))
        out.append(block)
    return Content.assemble(*out)
