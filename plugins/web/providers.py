"""Search and an external page reader, through services built for AI agents.

Exa (mcp.exa.ai) and Parallel (search.parallel.ai) both offer their MCP servers for free,
rate-limited use without an account or key; OpenCode's web search uses them the same way.
Each call is one stateless JSON-RPC `tools/call` POST; the reply is JSON or a short
server-sent event stream. Queries and the URLs given to the reader go to these companies.

When a provider refuses (HTTP 429, its rate-limit message) or fails, it is left alone for
a while: the time it may be used again is kept in /data/providers.json, so repeated calls
back off instead of hammering it. Scraping search engines' result pages was tried first
and dropped: DuckDuckGo blocked this kind of client within a handful of queries, and Bing
answered with plausible but unrelated results.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

import fetching

DATA = Path("/data") if Path("/data").is_dir() else Path(os.environ.get("TMPDIR", "/tmp")) / "whim-web-data"
STATE = DATA / "providers.json"
EXA = "https://mcp.exa.ai/mcp"
PARALLEL = "https://search.parallel.ai/mcp"
CALL_SECONDS = 25.0
RATE_LIMITED_PAUSE = 10 * 60
REFUSAL = re.compile(r"(you('ve| have) (hit|exceeded|reached)|too many requests|exceeded).{0,60}(rate.?limit|quota)|"
                     r"rate.?limit (exceeded|reached)", re.IGNORECASE)
FAILURE_PAUSE = 2 * 60
READER_CHARS = 150_000
# These services are built for agents: say plainly what is calling (no browser disguise).
HEADERS = {"User-Agent": "whim-web/0.2 (+https://github.com/calibancode/whim)"}


class Unavailable(Exception):
    """A provider refused or failed; the message says why."""


# -- state ----------------------------------------------------------------------------


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def paused_until(name: str) -> float:
    return float(_state().get("paused", {}).get(name, 0))


def _pause(name: str, seconds: float) -> None:
    state = _state()
    state.setdefault("paused", {})[name] = time.time() + seconds
    _save(state)


def _session_id() -> str:
    """Parallel asks free users for a stable session id (used for its rate limiting)."""
    state = _state()
    if "parallel_session" not in state:
        state["parallel_session"] = "whim-" + secrets.token_hex(16)
        _save(state)
    return state["parallel_session"]


# -- calling --------------------------------------------------------------------------


def _call(name: str, url: str, tool: str, arguments: dict, seconds: float = CALL_SECONDS) -> dict:
    wait = paused_until(name) - time.time()
    if wait > 0:
        raise Unavailable(f"{name} is paused for {int(wait // 60) + 1} more minutes after refusing earlier")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    try:
        resp = fetching.get(url, accept="application/json, text/event-stream", json_body=body, headers=HEADERS,
                            deadline=time.monotonic() + seconds, max_bytes=2 * 1024 * 1024)
    except fetching.FetchError as e:
        if "timed out" not in str(e):  # a slow page read says nothing about the service
            _pause(name, FAILURE_PAUSE)
        raise Unavailable(f"{name}: {e}") from None
    text = resp.body.decode("utf-8", "replace")
    if resp.status == 429 or (resp.status >= 400 and re.search(r"rate.?limit", text[:2000], re.IGNORECASE)):
        retry = resp.headers.get("Retry-After", "")
        _pause(name, int(retry) if retry.isdigit() else RATE_LIMITED_PAUSE)
        raise Unavailable(f"{name} is rate limiting these free requests (HTTP {resp.status})")
    if resp.status >= 400:
        _pause(name, FAILURE_PAUSE)
        raise Unavailable(f"{name} answered HTTP {resp.status} {resp.reason}")
    message = _message(text)
    if message is None:
        _pause(name, FAILURE_PAUSE)
        raise Unavailable(f"{name} sent a reply that isn't JSON-RPC")
    if "error" in message:
        err = message["error"] if isinstance(message["error"], dict) else {}
        detail = str(err.get("message", "error"))[:300]
        limited = re.search(r"rate.?limit|too many|quota", detail, re.IGNORECASE)
        _pause(name, RATE_LIMITED_PAUSE if limited else FAILURE_PAUSE)
        raise Unavailable(f"{name}: {detail}")
    result = message.get("result") or {}
    # Exa reports its free-tier limit as an ordinary result whose text says so.
    if not result.get("structuredContent") and REFUSAL.search(_text(result)[:400]):
        _pause(name, RATE_LIMITED_PAUSE)
        raise Unavailable(f"{name} is rate limiting these free requests")
    if result.get("isError"):
        detail = " ".join(str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))[:300]
        if re.search(r"rate.?limit|too many|quota", detail, re.IGNORECASE):
            _pause(name, RATE_LIMITED_PAUSE)
        raise Unavailable(f"{name}: {detail or 'tool error'}")
    return result


def _message(text: str) -> dict | None:
    candidates = [text.strip()] + [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    for c in candidates:
        if c.startswith("{"):
            try:
                value = json.loads(c)
            except ValueError:
                continue
            if isinstance(value, dict) and ("result" in value or "error" in value):
                return value
    return None


def _text(result: dict) -> str:
    return "\n".join(str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))


# -- search ---------------------------------------------------------------------------


def _exa_search(query: str, goal: str, limit: int) -> list[dict]:
    result = _call("exa", EXA, "web_search_exa", {"query": query, "objective": goal or query, "numResults": limit})
    out = []
    for block in re.split(r"\n\s*---\s*\n", _text(result)):
        url = re.search(r"^URL:\s*(\S+)", block, re.MULTILINE)
        if not url:
            continue
        title = re.search(r"^Title:\s*(.+)$", block, re.MULTILINE)
        date = re.search(r"^Published:\s*(.+)$", block, re.MULTILINE)
        body = re.search(r"^(?:Highlights|Text|Summary):\s*\n?([\s\S]*)", block, re.MULTILINE)
        out.append({"url": url[1], "title": _known(title[1]) if title else "",
                    "date": _known(date[1])[:10] if date else "", "excerpt": body[1].strip() if body else ""})
    return out


def _parallel_search(query: str, goal: str, limit: int) -> list[dict]:
    result = _call("parallel", PARALLEL, "web_search", {"objective": goal or query, "search_queries": [query],
                                                        "session_id": _session_id()})
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        try:
            data = json.loads(_text(result))
        except ValueError:
            raise Unavailable("parallel sent results in an unexpected shape") from None
    out = []
    for r in (data.get("results") or [])[:limit]:
        if isinstance(r, dict) and isinstance(r.get("url"), str):
            out.append({"url": r["url"], "title": r.get("title") or "", "date": (r.get("publish_date") or "")[:10],
                        "excerpt": "\n".join(x for x in r.get("excerpts") or [] if isinstance(x, str))})
    return out


def _known(value: str) -> str:
    value = value.strip()
    return "" if value in ("N/A", "None", "null") else value


SEARCHERS = (("exa", _exa_search), ("parallel", _parallel_search))


def off_topic(query: str, results: list[dict]) -> bool:
    """True when hardly any result mentions a word of the query: a sign of a degraded or
    confused answer, which must not be presented as a confident match."""
    words = set(re.findall(r"\w{3,}", re.sub(r"\b\w+:\S*", " ", query).lower()))
    if not words or not results:
        return False
    hits = sum(any(w in f"{r['title']} {r['excerpt']} {r['url']}".lower() for w in words) for r in results)
    return hits < 0.3 * len(results)


def search(query: str, goal: str, limit: int) -> tuple[str, list[dict], list[str]]:
    """(provider, results, problems with providers tried first). Raises Unavailable when
    no provider gave usable results."""
    problems: list[str] = []
    fallback = None
    for name, searcher in SEARCHERS:
        try:
            results = searcher(query, goal, limit)
        except Unavailable as e:
            problems.append(str(e))
            continue
        if not results:
            problems.append(f"{name} found nothing")
            continue
        if off_topic(query, results):
            problems.append(f"{name}'s results hardly mention the query words")
            fallback = fallback or (name, results)
            continue
        return name, results, problems
    if fallback is not None:
        return fallback[0], fallback[1], problems + ["these results may be off topic"]
    if problems and all(p.endswith("found nothing") for p in problems):
        return "", [], problems
    raise Unavailable("; ".join(problems))


# -- reader ---------------------------------------------------------------------------


def read(url: str) -> tuple[str, str]:
    """(provider, Markdown) of a page as an external reader service sees it: they render
    JavaScript and get past many bot checks, but may serve a cached copy."""
    problems = []
    try:
        text = _text(_call("exa", EXA, "web_fetch_exa", {"urls": [url], "maxCharacters": READER_CHARS}, 20))
        if text.strip() and not re.match(r"\s*(Error|No content)", text):
            return "exa", text
        problems.append("exa returned no content")
    except Unavailable as e:
        problems.append(str(e))
    try:
        result = _call("parallel", PARALLEL, "web_fetch", {"urls": [url], "full_content": True,
                                                           "session_id": _session_id()}, 30)
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            data = json.loads(_text(result) or "{}")
        for r in data.get("results") or []:
            text = r.get("full_content") or "\n\n".join(r.get("excerpts") or [])
            if text.strip():
                title = r.get("title")
                return "parallel", (f"# {title}\n\n" if title else "") + text
        problems.append("parallel returned no content")
    except (Unavailable, ValueError) as e:
        problems.append(str(e))
    raise Unavailable("; ".join(problems))
