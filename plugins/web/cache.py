"""Documents already fetched and converted, kept in the plugin's own /data folder.

Reading on (another offset, a find, a #fragment) reuses the stored text instead of
downloading and converting the page again, and every window of one reading comes from the
same copy, so offsets stay consistent. A copy is reused for MAX_AGE; after that, or when
asked to refresh, the page is fetched again and the reply says if its text changed.

Bounded: at most MAX_ENTRIES documents and MAX_BYTES in all; the least recently read go
first. Each entry is one JSON file, written to a temporary name and renamed into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from providers import DATA

ROOT = DATA / "cache"
MAX_AGE = 60 * 60
MAX_ENTRIES = 300
MAX_BYTES = 100 * 1024 * 1024
MAX_TEXT = 6_000_000  # characters; longer documents are cut


def key(url: str, view: str, accept: str) -> str:
    return hashlib.sha256(f"{url}\0{view}\0{accept}".encode()).hexdigest()[:24]


def revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def load(k: str) -> dict | None:
    path = ROOT / f"{k}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        os.utime(path)  # recently read: evicted last
        return doc
    except (OSError, ValueError):
        return None


def store(k: str, doc: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / f".{k}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ROOT / f"{k}.json")
    except OSError:
        tmp.unlink(missing_ok=True)  # a full disk only costs the cache
        return
    _evict()


def _evict() -> None:
    entries = []
    for p in ROOT.glob("*.json"):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
    entries.sort(reverse=True)  # newest first
    total = 0
    for i, (mtime, size, p) in enumerate(entries):
        total += size
        if i >= MAX_ENTRIES or total > MAX_BYTES or time.time() - mtime > 7 * 24 * 3600:
            p.unlink(missing_ok=True)
