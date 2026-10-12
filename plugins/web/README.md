# web

Read-only research on the public internet: search for sources, then read them: web
pages, documentation, source code, PDFs and public JSON/XML APIs. It sends GET requests
for everything it reads, and has no accounts, cookies, forms, credentials or browser.

Tools (as the character sees them):

- `web_search(query, max_results=8, goal="")`: titles, URLs, dates and excerpts.
- `web_fetch(url, offset=0, find="", max_chars=10000, view="content", refresh=false, accept="")`:
  one window of a document, or with `find` the passages that match, each with an offset.

Installing and approving it: [docs/web_plugin.md](../../docs/web_plugin.md) in the whim
repository.

## Files

| File | What it does |
| --- | --- |
| `server.py` | the two tools: what they accept, reading windows, `find`, fragments |
| `fetching.py` | HTTP: public addresses only, redirects, limits, decompression |
| `html2md.py` | HTML to Markdown on top of JustHTML |
| `pdf.py` | PDF text on top of pypdf |
| `providers.py` | search and the external reader (Exa, Parallel), with back-off |
| `cache.py` | documents already read, in `/data/cache` |
| `*.whl` | JustHTML and pypdf, unmodified wheels from PyPI |

## Search

General web search comes from two services built for AI agents that offer free,
rate-limited use without an account: [Exa](https://exa.ai/docs/reference/exa-mcp)
(`mcp.exa.ai`) and [Parallel](https://docs.parallel.ai/integrations/mcp/search-mcp)
(`search.parallel.ai`). OpenCode's web search uses them the same way. Each search is one
stateless JSON-RPC `tools/call` POST to their MCP endpoint. Exa is asked first; Parallel
when Exa refuses, fails or finds nothing. Results whose text hardly mentions the query are
flagged rather than presented as matches.

Their limits are not published and are small. Exa's refusal arrives as an ordinary
result ("You've hit Exa's free MCP rate limit"), which is recognised. A provider that
refuses is not asked again for 10 minutes (2 after other failures); the times are kept in
`/data/providers.json`. Identical searches within the hour are answered from the cache.

Scraping search engines' result pages was tried first and dropped. DuckDuckGo's HTML
endpoint blocked this client after about six queries and kept blocking for over an hour;
Bing then answered with plausible but unrelated results; Mojeek and Brave require a
JavaScript challenge. Maintained scrapers (such as `ddgs`) cope by imitating browsers' TLS
fingerprints, which the standard library can't do.

What these services see: the queries, and the URLs handed to their reader. Parallel also
receives a random session id that the plugin keeps in `/data` (it asks for one to apply
its free-tier limits).

## Reading

**Fetching** (`fetching.py`) uses `http.client` directly. Every connection, including
each redirect, resolves the host and refuses it if any address is not public (loopback,
private, link-local, carrier NAT, multicast...), then connects to the address it
checked, so DNS can't swap in a local one. This check lives in the plugin: whim's `net`
permission gives the sandbox the host's network, so the check prevents accidents (a link
into a local service), but code that chose to skip it would not be stopped. Limits: 35 s
per fetch, every socket read within that deadline, 8 redirects, 25 MB of (decompressed)
body. Only gzip and deflate are accepted.

**HTML** (`html2md.py`) is parsed by [JustHTML](https://github.com/EmilStenstrom/justhtml),
a pure-Python, spec-compliant HTML5 parser with a Markdown serialiser. Before serialising,
the plugin removes what a reader never sees (scripts, styles, hidden elements, form
controls), narrows to the main region (`<main>`, `role=main` or a lone `<article>`,
without its menus) unless `view="page"`, makes links absolute, replaces images with
their alt text, fences code blocks with their language, renders data tables as Markdown
tables and layout tables as lines, and keeps definition lists and `<details>` as blocks.
It records where every element id lands, so `https://.../page#section` opens there.

**PDFs** (`pdf.py`) are read by [pypdf](https://github.com/py-pdf/pypdf), page by page,
with `[page N]` lines for citing. Entries of the PDF's outline (bookmarks) become
Markdown headings where their title appears. Up to 400 pages; scanned PDFs have no text
to extract (no OCR).

**JSON** is pretty-printed, with a one-line sketch of its structure when it is long.
API responses also show pagination (`Link`) and rate-limit headers. `accept` sets the
Accept header (content negotiation, such as citation formats from doi.org).

**Bot checks and JavaScript.** When a direct fetch meets a bot check (Cloudflare's
challenge, a short 403/419/429/503 refusal, a "Just a moment..." page) or a page that only
builds itself with JavaScript, the plugin asks the external reader (Exa, then Parallel)
and labels the result as that service's version. It can be cached or restructured: for
some sites (PubMed Central, for instance) Exa returns a summary built from metadata rather
than the article. GitHub issues, pull requests and discussions go to the reader directly;
GitHub file pages are read from `raw.githubusercontent.com`.

**Long documents.** A converted document is kept in `/data/cache` for an hour, so
continuing to read, `find`, and jumping to a fragment cost no download and always see the
same text. Each reply shows one window of up to 16,000 characters, cut at a line break;
the first lists the headings with their offsets, and each ends with the offset to
continue from. `find` matches phrases case- and spacing-insensitively and falls back to
lines containing all the important words; each match shows its offset and section.
After an hour, or with `refresh=true`, the page is fetched again and the reply says if
its text changed. The cache keeps at most 300 documents and 100 MB, least recently read
first out.

## Wheels

Both libraries are shipped as their unmodified PyPI wheels and imported straight from
the zip (both are pure Python and read no data files of their own). That keeps the plugin
at 10 files (6 modules, 2 wheels, plugin.json and this README), about 630 KiB, within
whim's limits of 50 files and 1 MiB.

| Wheel | Licence | sha256 (as on PyPI) |
| --- | --- | --- |
| `justhtml-3.13.0-py3-none-any.whl` | MIT | `b1628903f7888cd814520c5a569acdf1713717d2a2cb8a3f7874e6e002f93f48` |
| `pypdf-6.19.0-py3-none-any.whl` | BSD-3-Clause | `7e5d6e730e7dae87d560a2cee218b852f6498c8be61966f3cd02ead971e48d14` |

The licence texts are inside the wheels (`*.dist-info/licenses/LICENSE`) and in whim's
THIRD_PARTY_NOTICES.md. To upgrade one: `pip download --no-deps NAME==X.Y.Z`, check the
hash against PyPI, replace the file and update its name where it is imported
(`html2md.py`, `pdf.py`). pypdf 6.19.0 was chosen over the then three-day-old 6.20.0.

Considered and not used: Trafilatura (needs lxml, a C extension the sandbox can't load)
and pdfminer.six (6.6 MB, over the plugin size limit).

## Known limits

- General search depends on two companies' free tiers, which can run out for a while
  (`web_search` then says so, with the sites that can be searched directly).
- No JavaScript of its own: JavaScript-only pages need the external reader, which sees
  the URL and may serve an old or restructured version.
- Scanned PDFs and images: no OCR.
- GitHub's API allows 60 requests an hour without login (10 searches a minute).
- No POST, custom headers (other than Accept), cookies or authentication: nothing that
  acts on a site.
