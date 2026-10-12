"""The web plugin (plugins/web): the promises that hold without the network."""

from __future__ import annotations

import http.client
import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

WEB = Path(__file__).resolve().parents[1] / "plugins" / "web"
MODULES = ("fetching", "html2md", "providers")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(WEB))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the folder gets copied into workspaces
    for name in MODULES:
        sys.modules.pop(name, None)
    mods = SimpleNamespace(**{name: importlib.import_module(name) for name in MODULES})
    monkeypatch.setattr(mods.providers, "DATA", tmp_path)
    monkeypatch.setattr(mods.providers, "STATE", tmp_path / "providers.json")
    yield mods
    for name in MODULES:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("url", ["http://localhost:9/", "http://127.0.0.1:9/", "http://[::1]:9/",
                                 "http://2130706433:9/", "http://10.0.0.1:9/", "http://169.254.169.254/"])
def test_fetching_refuses_non_public_addresses(web, url):
    with pytest.raises(web.fetching.FetchError, match="non-public"):
        web.fetching.get(url, accept="*/*", deadline=time.monotonic() + 5)


PAGE = b"""<html><head><title>Guide</title><base href="https://docs.example.org/v2/"></head><body>
<nav><a href="/">Home</a></nav>
<main><h1 id="intro">Intro</h1><p>See <a href="guide.html">the guide</a> <a href="#intro">\xc2\xb6</a>.</p>
<div class="highlight-python"><pre>def f():
    return 1</pre></div>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>a</td><td>1</td></tr></table>
<dl><dt>key</dt><dd>meaning</dd></dl>
<h2 id="api">API</h2><p>Details.</p>
<script>alert(1)</script><p hidden>secret</p></main></body></html>"""


def test_html_becomes_markdown_a_model_can_navigate(web):
    page = web.html2md.convert(PAGE, "https://docs.example.org/v2/index.html", "utf-8")
    md = page.markdown
    assert page.title == "Guide" and page.narrowed and "Home" not in md
    assert "[the guide](https://docs.example.org/v2/guide.html)" in md  # absolute, so it can be fetched
    assert "\xb6" not in md and "alert" not in md and "secret" not in md
    assert "```python\ndef f():\n    return 1\n```" in md
    assert "| Name | Value |" in md and "| a | 1 |" in md
    assert "key\n\nmeaning" in md
    api = page.anchors["api"]  # a URL's #api opens on that heading's line
    assert md.rfind("\n", 0, api) + 1 == md.index("## API")


def test_a_provider_refusing_in_its_answer_is_skipped_and_left_alone(web, monkeypatch):
    """Exa reports its free-tier limit as an ordinary result. That must count as a refusal
    (not "nothing found"), the next provider must answer, and the refusing one must not be
    asked again right away."""
    asked = []

    def fake_get(url, **kw):
        asked.append(url)
        if "exa" in url:
            body = ('data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":'
                    '"You\'ve hit Exa\'s free MCP rate limit. Create an API key."}]}}\n').encode()
        else:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {
                "results": [{"url": "https://pypdf.readthedocs.io/", "title": "pypdf",
                             "excerpts": ["pypdf can extract text from PDF files."]}]}}}).encode()
        return web.fetching.Response(url, 200, "OK", http.client.HTTPMessage(), body, False, [])

    monkeypatch.setattr(web.fetching, "get", fake_get)
    provider, results, problems = web.providers.search("pypdf extract text", "", 5)
    assert provider == "parallel" and results[0]["url"] == "https://pypdf.readthedocs.io/"
    assert "rate limit" in problems[0]
    asked.clear()
    web.providers.search("pypdf extract text", "", 5)
    assert asked and not any("exa" in url for url in asked)
