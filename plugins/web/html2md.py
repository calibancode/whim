"""HTML to readable Markdown for a language model.

Parsing and Markdown come from JustHTML (a pure-Python, spec-compliant HTML5 parser),
shipped here as its unmodified PyPI wheel and imported straight from it. This module only
prepares the tree first, like a browser with JavaScript off would show it:

- drops what a reader never sees: scripts, styles, hidden elements, form controls, SVG
- narrows to the page's main region (<main> or role=main, or a lone <article>) without
  its menus (<nav> other than pagination), unless the whole page is asked for; pages
  without such a region are kept whole, navigation included
- makes every link absolute (honouring <base>), so it can be fetched as is; drops links
  that go nowhere (javascript:, same-page anchors such as heading permalinks)
- replaces images with their alt text
- turns tables into Markdown tables, or into plain lines when they are layout tables
- fences code blocks with their language when the page names it (language-x, highlight-x)
- keeps definition lists, figures and <details> as separate blocks
- records where each element id ends up, so a URL's #fragment can open at that point
"""

from __future__ import annotations

import codecs
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin

sys.path.insert(0, str(Path(__file__).with_name("justhtml-3.13.0-py3-none-any.whl")))

import justhtml.serializer.markdown as _jmd  # noqa: E402
from justhtml import JustHTML  # noqa: E402
from justhtml.dom import Element, Text  # noqa: E402

# JustHTML escapes Markdown syntax and '&' / '<' in text so the result renders safely.
# This output is read, not rendered, and escapes ("snake\_case", "AT&amp;T") are noise.
_jmd._markdown_escape_text = lambda s: s or ""

HIDDEN = ", ".join([
    "head", "script", "style", "template", "svg", "canvas", "iframe", "object", "embed",
    "button", "select", "input", "textarea", "dialog", "[hidden]", "[aria-hidden=true]",
    '[style*="display:none"]', '[style*="display: none"]',
])
SAME_PAGE_SYMBOLS = {"", "#", "¶", "§", "🔗", "link", "permalink"}
MAIN_SHARE = 0.1  # a main region must hold at least this share of the page's text


BLOCKS = "dl, dt, dd, figure, figcaption, details, summary, address, fieldset, legend, center, hgroup, search"
CODE_LANG = re.compile(r"(?:^|\s)(?:language|lang|highlight-source|highlight)-([A-Za-z0-9_+#.-]+)")
NOT_LANGS = {"default", "text", "none", "plain", "plaintext", "notranslate", "nohighlight"}
MARK = re.compile("\ue000(\\d+)\ue001")  # an element id's position, until it is recorded
MAX_ANCHORS = 5000
UNMARKABLE = {"table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "select", "colgroup"}


@dataclass
class Page:
    title: str
    markdown: str
    narrowed: bool              # only the main region was converted
    notes: list[str] = field(default_factory=list)
    anchors: dict[str, int] = field(default_factory=dict)  # element id -> offset in markdown
    needs_javascript: bool = False  # so little text that the page probably builds itself


def _text_len(node) -> int:
    return len(re.sub(r"\s+", "", node.to_text()))


def _remove(node) -> None:
    if node.parent is not None:
        node.parent.remove_child(node)


def _unwrap(node) -> None:
    parent = node.parent
    if parent is None:
        return
    for child in list(node.children or []):
        parent.insert_before(child, node)
    parent.remove_child(node)


def _inside(node, tag: str) -> bool:
    node = node.parent
    while node is not None:
        if getattr(node, "name", "") == tag:
            return True
        node = node.parent
    return False


def _block(text: str) -> Element:
    p = Element("p", {}, "html")
    p.append_child(Text(text))
    return p


def _images(root) -> None:
    for img in root.query("img"):
        alt = re.sub(r"\s+", " ", (img.attrs or {}).get("alt") or "").strip()
        if not alt:
            _remove(img)
        elif img.parent is not None:
            img.parent.replace_child(Text(alt if _inside(img, "a") else f"[image: {alt}]"), img)


def _links(root, base: str, page: str) -> None:
    here = urldefrag(page)[0]
    for a in root.query("a"):
        attrs = a.attrs or {}
        href = (attrs.get("href") or "").strip()
        text = a.to_text().strip()
        if not href or href.lower().startswith(("javascript:", "data:", "vbscript:")):
            _unwrap(a)
            continue
        target = urljoin(base, href)
        if href.startswith("#") or (urldefrag(target)[0] == here and "#" in href):
            (_remove if text.lower() in SAME_PAGE_SYMBOLS else _unwrap)(a)
            continue
        if not text:
            label = (attrs.get("aria-label") or attrs.get("title") or "").strip()
            if not label:
                _remove(a)
                continue
            a.append_child(Text(label))
        a.attrs = {"href": target}


def _cells(row) -> list:
    return [c for c in (row.children or []) if getattr(c, "name", "") in ("td", "th")]


def _rows(table) -> list:
    rows = []
    for child in table.children or []:
        name = getattr(child, "name", "")
        if name == "tr":
            rows.append(child)
        elif name in ("thead", "tbody", "tfoot"):
            rows.extend(r for r in child.children or [] if getattr(r, "name", "") == "tr")
    return rows


def _cell_md(cell) -> str:
    return re.sub(r"\n{3,}", "\n\n", cell.to_markdown().strip())


def _table(table, token_re: re.Pattern) -> str:
    rows = [[_cell_md(c) for c in _cells(r)] for r in _rows(table)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    # Drop columns with nothing below the header, and repeats of an earlier column
    # (responsive pages often repeat a column for small screens).
    columns = list(zip(*rows))

    def repeats(j: int, i: int) -> bool:
        return all(b in ("", a) for a, b in zip(columns[i], columns[j]))

    keep = [j for j, col in enumerate(columns)
            if any(col[1:] if len(rows) > 1 else col) and not any(repeats(j, i) for i in range(j))]
    if not keep:
        return ""
    rows = [[r[i] for i in keep] for r in rows]
    width = len(keep)
    # A data table has header cells; anything else is laid out with a table (HN, old forums).
    role = ((table.attrs or {}).get("role") or "").lower()
    layout = (role in ("presentation", "none") or width < 2 or not table.query("th")
              or any("\n" in c or token_re.search(c) for r in rows for c in r))
    if layout:
        lines = [" ".join(c for c in r if c) if not any("\n" in c for c in r) else "\n\n".join(c for c in r if c)
                 for r in rows]
        return ("\n\n" if any("\n" in line for line in lines) else "\n").join(lines)
    caption = next((c for c in table.children or [] if getattr(c, "name", "") == "caption"), None)
    out = [f"Table: {caption.to_text().strip()}", ""] if caption is not None and caption.to_text().strip() else []
    for i, r in enumerate(rows):
        cells = [c.replace("|", "\\|") for c in r]
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("|" + " --- |" * width)
    return "\n".join(out)


def _code_language(pre) -> str:
    code = next((c for c in pre.children or [] if getattr(c, "name", "") == "code"), None)
    node, candidates = pre.parent, [pre] + ([code] if code is not None else [])
    for _ in range(2):  # GitHub and Sphinx put it on wrapping <div>s
        if node is not None and getattr(node, "attrs", None) is not None:
            candidates.append(node)
            node = node.parent
    for n in candidates:
        attrs = n.attrs or {}
        if attrs.get("data-lang"):
            return attrs["data-lang"].strip()
        for m in CODE_LANG.finditer(attrs.get("class") or ""):
            if m[1].lower() not in NOT_LANGS:
                return m[1]
    return ""


def _code_blocks(root, rendered: dict[str, str]) -> None:
    """Replace each <pre> with a placeholder for a fenced block that names its language."""
    for pre in root.query("pre"):
        if pre.parent is None or _inside(pre, "pre"):
            continue
        code = pre.to_text(separator="", strip=False).strip("\n")
        fence = "```"
        while fence in code:
            fence += "`"
        token = f"WHIMBLOCK{len(rendered)}X"
        rendered[token] = f"{fence}{_code_language(pre)}\n{code}\n{fence}"
        pre.parent.replace_child(_block(token), pre)


def _tables(root, rendered: dict[str, str]) -> None:
    """Replace each table (innermost first) with a placeholder paragraph, for Markdown
    substituted after serialising."""
    token_re = re.compile(r"WHIMBLOCK\d+X")
    for table in reversed(root.query("table")):
        token = f"WHIMBLOCK{len(rendered)}X"
        rendered[token] = _table(table, token_re)
        if table.parent is not None:
            table.parent.replace_child(_block(token), table)


def _mark_ids(root) -> list[str]:
    """Put a marker where each element with an id (or a named anchor) starts."""
    names: list[str] = []
    for el in root.query("[id], a[name]"):
        attrs = el.attrs or {}
        ident = (attrs.get("id") or attrs.get("name") or "").strip()
        if not ident or len(names) >= MAX_ANCHORS:
            continue
        mark = Text(f"\ue000{len(names)}\ue001")
        # Inside blocks, so the offset is where their text starts; before links, which
        # may be removed (heading permalinks carry ids) or contain no text.
        inside = el.name not in UNMARKABLE and el.name != "a"
        if inside:
            el.insert_before(mark, (el.children or [None])[0])
        elif el.parent is not None and getattr(el.parent, "name", "") not in UNMARKABLE:
            el.parent.insert_before(mark, el)
        else:
            continue
        names.append(ident)
    return names


def _unmark(md: str, names: list[str]) -> tuple[str, dict[str, int]]:
    """Remove the id markers, recording their offsets; then close up the blank lines that
    markers alone kept apart, keeping the offsets in step."""
    pieces, positions, pos, length = [], [], 0, 0
    for m in MARK.finditer(md):
        pieces.append(md[pos:m.start()])
        length += m.start() - pos
        positions.append((names[int(m[1])], length))
        pos = m.end()
    pieces.append(md[pos:])
    text = "".join(pieces)
    out, cuts, pos, removed = [], [], 0, 0
    for m in re.finditer(r"\n(?:[ \t]*\n){2,}", text):
        out.append(text[pos:m.start()] + "\n\n")
        cuts.append((m.start(), m.end(), removed))
        removed += len(m[0]) - 2
        pos = m.end()
    out.append(text[pos:])
    anchors: dict[str, int] = {}
    for name, offset in positions:
        new = offset - removed
        for start, end, before in cuts:
            if offset < end:  # before this run, or inside it: where the next content begins
                new = offset - before if offset <= start else start + 2 - before
                break
        anchors.setdefault(name, max(0, new))
    return "".join(out), anchors


def _region(body) -> tuple[object, bool]:
    total = _text_len(body)
    if total == 0:
        return body, False
    mains = body.query("main, [role=main]")
    articles = body.query("article")
    candidates = mains or (articles if len(articles) == 1 else [])
    if candidates:
        best = max(candidates, key=_text_len)
        if _text_len(best) >= MAIN_SHARE * total and best is not body:
            return best, True
    return body, False


JS_REQUIRED = re.compile(r"(enable|turn on|requires?|need) ?javascript|javascript (is )?(required|disabled|needed)",
                         re.IGNORECASE)


def _needs_javascript(scripts: int, text_chars: int, markdown: str) -> bool:
    return (scripts > 0 and text_chars < 200) or (text_chars < 2000 and bool(JS_REQUIRED.search(markdown)))


def encoding(body: bytes, charset: str | None) -> str | None:
    """The declared charset if Python knows it, else UTF-8 if the bytes are valid UTF-8,
    else None to let the parser find a <meta charset> (or fall back as browsers do)."""
    if charset:
        try:
            return codecs.lookup(charset).name
        except LookupError:
            pass
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return None


def convert(body: bytes, url: str, charset: str | None, *, whole_page: bool = False) -> Page:
    doc = JustHTML(body, sanitize=False, encoding=encoding(body, charset), scripting_enabled=False)
    title_node = doc.query_one("title")
    title = re.sub(r"\s+", " ", title_node.to_text()).strip() if title_node is not None else ""
    base_node = doc.query_one("base[href]")
    base = urljoin(url, base_node.attrs["href"].strip()) if base_node is not None else url
    scripts = len(doc.query("script"))
    for node in doc.query(HIDDEN):
        _remove(node)
    root = doc.query_one("body") or doc.root
    narrowed = False
    if not whole_page:
        root, narrowed = _region(root)
    if narrowed:  # menus inside the main region too, but keep pagination
        for nav in root.query("nav, [role=navigation]"):
            label = " ".join((nav.attrs or {}).get(k) or "" for k in ("aria-label", "class", "id")).lower()
            if "pagin" not in label and "pager" not in label:
                _remove(nav)
    for node in root.query(BLOCKS):
        node.name = "div"
    names = _mark_ids(root)
    _images(root)
    _links(root, base, url)
    blocks: dict[str, str] = {}
    _code_blocks(root, blocks)
    _tables(root, blocks)
    md = root.to_markdown()
    for token in reversed(list(blocks)):  # outer tables contain inner blocks' tokens
        md = md.replace(token, blocks[token])
    md = re.sub(r"(?<=[)*])\[(?=[^\]\n]*\]\()", " [", md)  # links side by side in the source
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"(?m)^[ \t]*([-*]|#+)[ \t]*$\n?", "", md)  # list items and headings left empty
    md, anchors = _unmark(md.strip(), names)
    page = Page(title, md, narrowed, anchors=anchors)
    text_chars = len(re.sub(r"\s+", "", re.sub(r"\]\([^)]*\)", "]", md)))
    if _needs_javascript(scripts, text_chars, md):
        page.needs_javascript = True
        page.notes.append(f"Little readable text ({text_chars} characters): the page probably builds its "
                          "content with JavaScript. Other sources may help (the project's README, an API, "
                          "a /llms.txt), or view='source' for data embedded in the HTML.")
    return page
