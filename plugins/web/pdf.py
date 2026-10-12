"""Text from PDF files, with pypdf (pure Python, shipped as its unmodified PyPI wheel).

Works for PDFs that contain text; scanned pages (images of text) have none to extract.
Each page starts with a "[page N]" line so passages can be cited. When the PDF has an
outline (bookmarks), each entry becomes a Markdown heading at the line where its title
appears on its page (or at the top of that page), so the reader can jump to sections.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("pypdf-6.19.0-py3-none-any.whl")))

from pypdf import PdfReader  # noqa: E402
from pypdf.errors import PdfReadError  # noqa: E402

logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-PDF chatter goes to stderr otherwise

MAX_PAGES = 400


class PdfError(Exception):
    pass


def _outline(reader: PdfReader) -> list[tuple[int, int, str]]:
    """(page index, level, title) for the outline entries."""
    out: list[tuple[int, int, str]] = []

    def walk(items, level):
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(item)
                title = re.sub(r"\s+", " ", str(item.title)).strip()
            except Exception:  # broken outline entries are common; skip them
                continue
            if page is not None and page >= 0 and title:
                out.append((page, min(level, 5), title))
    try:
        walk(reader.outline, 1)
    except Exception:
        return []
    return out


def _place_headings(text: str, entries: list[tuple[int, str]]) -> str:
    """Prefix the line where each title appears with its heading marks."""
    lines = text.split("\n")
    before: dict[int, list[str]] = {}
    used: set[int] = set()
    top: list[str] = []
    for level, title in entries:
        mark = "#" * (level + 1)
        key = re.sub(r"\W+", "", title).lower()
        norms = [re.sub(r"\W+", "", line).lower() for line in lines]
        # the title's own line: contains it, plus at most a section number or so
        own = next((i for i, n in enumerate(norms) if i not in used and key and key in n and len(n) <= len(key) + 12),
                   None)
        if own is not None:
            lines[own] = f"{mark} {lines[own].strip()}"
            used.add(own)
            continue
        # a run-in heading ("Methods: We searched..."): a heading line before it
        lead = next((i for i, n in enumerate(norms) if i not in used and key and n.startswith(key)), None)
        if lead is not None:
            before.setdefault(lead, []).append(f"{mark} {title}")
            used.add(lead)
        else:
            top.append(f"{mark} {title}")
    out = top[:]
    for i, line in enumerate(lines):
        out.extend(before.get(i, []))
        out.append(line)
    return "\n".join(out)


def convert(body: bytes, deadline: float) -> tuple[str, str, list[str]]:
    """(title, text, notes)."""
    notes: list[str] = []
    try:
        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("the PDF is encrypted with a password")
        pages = reader.pages
        count = len(pages)
    except PdfError:
        raise
    except Exception as e:
        raise PdfError(f"pypdf could not read this PDF ({type(e).__name__}: {e})") from None
    meta = {}
    try:
        meta = reader.metadata or {}
    except (PdfReadError, ValueError):
        pass
    title = str(meta.get("/Title") or "").strip()
    outline: dict[int, list[tuple[int, str]]] = {}
    for page, level, name in _outline(reader):
        outline.setdefault(page, []).append((level, name))
    parts: list[str] = []
    empty = 0
    for i in range(min(count, MAX_PAGES)):
        if time.monotonic() > deadline:
            notes.append(f"Extraction stopped at page {i + 1} of {count} (time limit); the rest is missing.")
            break
        try:
            text = pages[i].extract_text() or ""
        except Exception as e:  # one damaged page shouldn't lose the document
            text = f"(text of this page could not be extracted: {type(e).__name__})"
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        if not text:
            empty += 1
        parts.append(f"[page {i + 1}]\n" + _place_headings(text, outline.get(i, [])))
    else:
        if count > MAX_PAGES:
            notes.append(f"Only the first {MAX_PAGES} of {count} pages were extracted.")
    if parts and empty == len(parts):
        raise PdfError(f"none of its {len(parts)} pages has extractable text; it is probably scanned images, "
                       "which would need OCR")
    if empty > len(parts) // 3:
        notes.append(f"{empty} of {len(parts)} pages have no extractable text (scanned images or figures).")
    return title, f"PDF, {count} pages.\n\n" + "\n\n".join(parts), notes
