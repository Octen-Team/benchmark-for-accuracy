"""Ground-truth construction — the parse channel, for non-HTML documents.

The document pages carry the best ground truth in the set: the reference for a CSV is
that CSV, the reference for a spreadsheet is the parsed sheet. No browser, no keys,
entirely local.

Because only fetch capability is scored, the parsers exist solely to turn a document
into comparable text for the coverage check. They deliberately do not emit structure
counts — table and slide fidelity is parsing quality, which is out of scope.

A parse failure always returns a rule name rather than raising, so the caller can tell
"this page cannot be parsed" apart from "our parser crashed".
"""
from __future__ import annotations

import signal
import threading
from contextlib import contextmanager

import csv as _csv
import io
import json as _json
import re

_MAGIC = ((b"%PDF", "pdf"), (b"PK\x03\x04", "zip"))
_ZIP_KINDS = {"docx", "xlsx", "pptx"}
_CT_MAP = {
    "application/pdf": "pdf",
    "text/csv": "csv",
    "application/json": "json",
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/html": "html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def _import(name: str):
    """Single import site, so tests can stub it out as a missing dependency."""
    try:
        return __import__(name)
    except ImportError:
        return None


def sniff_doc_type(raw: bytes, content_type: str, declared: str) -> tuple[str, str]:
    """Return (doc_type, rule_name). Three levels: magic bytes > content-type > URL.

    A URL like `arxiv.org/pdf/1706.03762` has no suffix, so `declared` is unknown and the
    later levels settle it — that page exists precisely to test whether a provider gets
    this wrong. The rule name is recorded per page so it is always possible to check
    which level made the call.
    """
    head = (raw or b"")[:8]
    for sig, kind in _MAGIC:
        if head.startswith(sig):
            if kind == "zip":
                # docx/xlsx/pptx are all zips; magic bytes alone cannot separate them
                if declared in _ZIP_KINDS:
                    return declared, "declared_zip"
                return "zip", "magic_bytes"
            return kind, "magic_bytes"
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_MAP:
        return _CT_MAP[ct], "content_type"
    if declared and declared != "unknown":
        return declared, "declared"
    return "unknown", "undetermined"


def _table_md(rows: list[list]) -> tuple[str, int]:
    """Render a table as a Markdown pipe table; returns (text, data row count).

    Header + separator + data rows, so a consumer counting pipe rows subtracts 2 and the
    two sides agree.
    """
    if not rows:
        return "", 0
    head = rows[0]
    out = ["| " + " | ".join(str(c if c is not None else "") for c in head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(str(c if c is not None else "") for c in r) + " |")
    return "\n".join(out), max(0, len(rows) - 1)


def _ok(text: str, doc_type: str, rule: str = "parsed") -> dict:
    if not (text or "").strip():
        rule = "parsed_no_text"
    return {"text": text or "", "doc_type": doc_type, "rule": rule}


def _fail(doc_type: str, rule: str) -> dict:
    return {"text": "", "doc_type": doc_type, "rule": rule}


def parse_document(raw: bytes, doc_type: str, url: str) -> dict:
    """Parse one non-HTML document into Markdown text. **Failure returns a rule name;
    it never raises.**"""
    try:
        return _PARSERS.get(doc_type, _parse_text)(raw, doc_type)
    except Exception:                            # noqa: BLE001
        return _fail(doc_type, "parse_failed")


def _parse_csv(raw: bytes, doc_type: str) -> dict:
    rows = list(_csv.reader(io.StringIO(raw.decode("utf-8", "replace"))))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    text, n = _table_md(rows)
    return _ok(text, doc_type)


def _parse_json(raw: bytes, doc_type: str) -> dict:
    obj = _json.loads(raw.decode("utf-8", "replace"))
    text = _json.dumps(obj, ensure_ascii=False, indent=2)
    return _ok(text, doc_type)


def _parse_feed(raw: bytes, doc_type: str) -> dict:
    fp = _import("feedparser")
    if fp is None:
        return _fail(doc_type, "dep_missing")
    d = fp.parse(raw)
    items = d.get("entries") or []
    text = "\n".join("- " + (e.get("title") or "").strip() for e in items)
    return _ok(text, doc_type)


def _parse_xml(raw: bytes, doc_type: str) -> dict:
    s = raw.decode("utf-8", "replace")
    nodes = len(re.findall(r"<(?!\?|!)([a-zA-Z][\w:-]*)", s))
    text = re.sub(r"<[^>]+>", " ", s)
    text = re.sub(r"\s+", " ", text).strip()
    return _ok(text, doc_type)


def _parse_pdf(raw: bytes, doc_type: str) -> dict:
    pypdf = _import("pypdf")
    if pypdf is None:
        return _fail(doc_type, "dep_missing")
    r = pypdf.PdfReader(io.BytesIO(raw))
    parts = []
    for i, page in enumerate(r.pages, 1):
        parts.append("## Page %d" % i)
        parts.append((page.extract_text() or "").strip())
    return _ok("\n\n".join(p for p in parts if p), doc_type)


def _parse_docx(raw: bytes, doc_type: str) -> dict:
    if _import("docx") is None:
        return _fail(doc_type, "dep_missing")
    import docx
    d = docx.Document(io.BytesIO(raw))
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    blocks = list(paras)
    for t in d.tables:
        rows = [[c.text for c in row.cells] for row in t.rows]
        md, _ = _table_md(rows)
        blocks.append(md)
    return _ok("\n\n".join(blocks),
               {"paragraphs": len(paras), "tables": len(d.tables)}, doc_type)


def _parse_xlsx(raw: bytes, doc_type: str) -> dict:
    if _import("openpyxl") is None:
        return _fail(doc_type, "dep_missing")
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    blocks, total = [], 0
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)
                if any(c is not None and str(c).strip() for c in r)]
        md, n = _table_md(rows)
        total += n
        # Sheet names go to the detail view only, never the score: provider markdown
        # does not carry this marker.
        blocks.append("## Sheet: %s\n%s" % (ws.title, md))
    return _ok("\n\n".join(blocks),
               {"sheets": len(wb.worksheets), "rows": total}, doc_type)


def _parse_pptx(raw: bytes, doc_type: str) -> dict:
    if _import("pptx") is None:
        return _fail(doc_type, "dep_missing")
    from pptx import Presentation
    pres = Presentation(io.BytesIO(raw))
    blocks = []
    for i, slide in enumerate(pres.slides, 1):
        texts = [sh.text.strip() for sh in slide.shapes
                 if getattr(sh, "has_text_frame", False) and sh.text.strip()]
        blocks.append("## Slide %d\n%s" % (i, "\n".join(texts)))
    return _ok("\n\n".join(blocks), doc_type)


def _parse_text(raw: bytes, doc_type: str) -> dict:
    s = raw.decode("utf-8", "replace")
    lines = [l for l in s.splitlines() if l.strip()]
    return _ok(s, doc_type)


_PARSERS = {
    "csv": _parse_csv, "json": _parse_json,
    "rss": _parse_feed, "atom": _parse_feed,
    "xml": _parse_xml, "pdf": _parse_pdf,
    "docx": _parse_docx, "xlsx": _parse_xlsx, "pptx": _parse_pptx,
    "txt": _parse_text, "md": _parse_text,
}


# ══════════════════════════════════════════════════════════════════════════
# Browser channel: ground truth for HTML pages
# ══════════════════════════════════════════════════════════════════════════

from collections import Counter                                    # noqa: E402

from .fetch_checks import tokenize                                 # noqa: E402
from .fetch_spec import TH                                         # noqa: E402

# What remains after excluding these subtrees is the body text; the excluded part is
# exactly where the boilerplate vocabulary comes from. Splitting the page structurally
# is far more reliable than a link-density heuristic.
_CHANNELS = frozenset({"playwright_headless", "chrome_real"})

BOILER_SELECTORS = ("nav", "header", "footer", "aside", "[role=navigation]",
                    "[role=banner]", "[role=contentinfo]", ".nav", ".navbar",
                    ".header", ".footer", ".sidebar", ".menu", ".breadcrumb",
                    "#nav", "#header", "#footer", "#sidebar")

VOCAB_CAP = 60
_STOP = frozenset("""
a an the and or but if then else of in on at to for from by with without into onto
is are was were be been being do does did have has had will would can could should
this that these those it its as not no yes you your we our they their he she his her
i me my us them there here what which who whom when where why how all any both each
more most other some such only own same so than too very s t don now
""".split())


def _playwright():
    """Single import site; tests skip on it."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


def _content_terms(text: str, exclude: set[str], cap: int) -> list[str]:
    """Pick content terms by frequency: drop stopwords, single Latin characters, and
    anything in the exclude set."""
    freq = Counter(t for t in tokenize(text)
                   if t not in _STOP and t not in exclude
                   and (len(t) >= 2 or not t.isascii()))
    return [w for w, _ in freq.most_common(cap)]


def derive_vocab(main_text: str, boiler_text: str) -> dict:
    """Produce the content vocabulary — the denominator of the success gate. The
    boilerplate vocabulary is only an exclusion set, never a metric.

    Tail terms come from the last 10% of the body and feed the `oversize` truncation
    check: silent truncation still passes a coverage gate as long as the vocabulary
    falls in the retained half.
    """
    # **A term counts as boilerplate when it is MORE common in the chrome than in the
    # body — not merely when it appears in the chrome.** A documentation sidebar lists
    # every section name, and those are exactly the page's most central content terms.
    # Excluding on mere appearance would strip the recall denominator of the very words
    # the check exists to look for.
    main_freq = Counter(t for t in tokenize(main_text) if t not in _STOP)
    boiler_freq = Counter(t for t in tokenize(boiler_text) if t not in _STOP)
    boiler_only = {t for t, n in boiler_freq.items() if n >= main_freq.get(t, 0)}
    boiler = _content_terms(boiler_text, set(boiler_freq) - boiler_only, VOCAB_CAP)
    vocab = _content_terms(main_text, boiler_only, VOCAB_CAP)
    toks = tokenize(main_text)
    # **Identity anchors must come from the start of the document.** Globally rare terms
    # select values buried deep in tables, which mark *completeness*, not *identity*: a
    # provider that fetched the correct paper but did not extract every table row would
    # then be judged as having returned the wrong page. "Is this that page?" is
    # answerable from the opening.
    head_src = toks[:max(200, int(len(toks) * 0.1))]
    head = _content_terms(" ".join(head_src), boiler_only, VOCAB_CAP)
    return {"vocab": vocab, "vocab_n": len(vocab),
            "boiler_terms": boiler, "vocab_head": head,
            "degenerate": len(vocab) < TH["vocab_min"]}


def gt_is_walled(main_text: str, title: str) -> list[str]:
    """Is the ground truth itself a wall?

    Our browser is architecturally the same thing the unblocking vendors sell, so it gets
    challenged on defended pages too. A render that "succeeded" and produced a vocabulary
    is not proof the content arrived: a challenge screen renders perfectly well, and its
    text becomes the vocabulary. Every provider is then compared against a challenge
    page. Flagging a gap only when the vocabulary is empty does not catch this.
    """
    from .fetch_checks import wall_hit
    return sorted(set(wall_hit(main_text) + wall_hit(title or "")))


def derive_strength(headless_ok: bool | None, chrome_ok: bool | None) -> str:
    """Protection strength falls out of the two ground-truth channels for free.

    **If either channel was not run, the result is `unknown` — never "ran and failed".**
    Defaulting to hard disguises "not measured" as "measured, hardest". Treating a
    missing headless result as False is the same mistake in the other direction: a page
    where only real Chrome ran, and succeeded, would be labelled medium on no evidence.
    """
    if headless_ok:
        return "soft"
    if headless_ok is None or chrome_ok is None:
        return "unknown"
    return "medium" if chrome_ok else "hard"


_JS_EXTRACT = """
() => {
  const sels = %s;
  const doc = document;
  doc.querySelectorAll('script,style,noscript,template').forEach(n => n.remove());
  const boilerNodes = [];
  sels.forEach(s => { try { doc.querySelectorAll(s).forEach(n => boilerNodes.push(n)); }
                      catch (e) {} });
  const boilerText = boilerNodes.map(n => n.innerText || '').join('\\n');
  // Temporarily hide the chrome and read innerText from the **live document**. A cloned
  // node has no layout, so innerText degrades to textContent and CSS inside <style>
  // would be counted as body text.
  const prev = boilerNodes.map(n => n.style.display);
  boilerNodes.forEach(n => { n.style.display = 'none'; });
  const mainText = doc.body.innerText || '';
  boilerNodes.forEach((n, i) => { n.style.display = prev[i]; });
  return {mainText, boilerText, title: doc.title || ''};
}
"""


def render_page(url: str, *, channel: str = "playwright_headless",
                timeout: int = 45, screenshot_dir: str | None = None,
                pid: str = "page") -> dict:
    """Render one page and derive its ground truth. **Clean profile, never signed in,
    cookies accepted only.**

    Building ground truth from a signed-in session would put content behind the login
    wall into the reference, and every provider would then fail against an answer none of
    them can reach. That measures our session, not the providers.
    """
    sync_playwright = _playwright()
    if sync_playwright is None:
        return {"error": "playwright is not installed", "rule": "dep_missing"}
    if channel not in _CHANNELS:
        raise ValueError("unknown channel %r; expected one of %s"
                         % (channel, sorted(_CHANNELS)))
    budget = timeout * 2 + 30          # navigation + scroll + screenshot, with headroom
    try:
        with _watchdog(budget):
            got = _render_once(sync_playwright, url, channel, timeout,
                               screenshot_dir, pid, [])
    except TimeoutError as e:
        return {"channel": channel, "url": url, "rule": "render_failed",
                "error": str(e), "main_text": "", "boiler_text": ""}
    if got.get("rule") == "render_failed" and "ERR_HTTP2" in (got.get("error") or ""):
        # Some sites' HTTP/2 implementations fail to negotiate with chromium. Retry once
        # with H2 disabled before concluding anything, or a protocol negotiation failure
        # gets recorded as "this page could not be fetched".
        try:
            with _watchdog(budget):
                got = _render_once(sync_playwright, url, channel, timeout,
                                   screenshot_dir, pid, ["--disable-http2"])
        except TimeoutError as e:
            return {"channel": channel, "url": url, "rule": "render_failed",
                    "error": str(e), "main_text": "", "boiler_text": ""}
        got["retried_without_http2"] = True
    return got


@contextmanager
def _watchdog(seconds: int):
    """A hard watchdog. **Playwright timeouts cover individual operations only**; when a
    page hangs inside the browser process the whole round blocks indefinitely, producing
    no rows at all. Main thread only (a SIGALRM limitation), which is fine because the
    browser channel runs sequentially anyway.
    """
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _boom(signum, frame):
        raise TimeoutError("render watchdog fired: one page exceeded %ds" % seconds)

    old = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _render_once(sync_playwright, url: str, channel: str, timeout: int,
                 screenshot_dir: str | None, pid: str, args: list[str]) -> dict:
    import json as _j
    out: dict = {"channel": channel, "url": url}
    with sync_playwright() as p:
        # **The channel must genuinely change how the browser launches.** With
        # `headless=True` hard-coded, a real-Chrome channel would be a decorative
        # argument: the command silently runs headless while claiming a real browser,
        # and a real browser fingerprint is the entire point on defended pages.
        if channel == "chrome_real":
            # The locally installed Google Chrome: headed, real fingerprint, **clean
            # profile, never signed in** (a signed-in profile would build ground truth
            # no provider can reach).
            browser = p.chromium.launch(headless=False, channel="chrome", args=args)
        else:
            browser = p.chromium.launch(headless=True, args=args)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900}, locale="en-US")
        page = ctx.new_page()
        try:
            resp = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            out["http_status"] = resp.status if resp else None
            _scroll_to_bottom(page)
            data = page.evaluate(_JS_EXTRACT % _j.dumps(list(BOILER_SELECTORS)))
            out["main_text"] = data["mainText"]
            out["boiler_text"] = data["boilerText"]
            out["title"] = data["title"]
            out["rule"] = "rendered"
            if screenshot_dir:
                # **The screenshot has its own fallback.** A screenshot failure must not
                # discard body text that was already retrieved: a page can return 200
                # with perfectly good text while captureScreenshot raises a protocol
                # error, and without this the whole page's ground truth would be lost.
                try:
                    import os as _os
                    _os.makedirs(screenshot_dir, exist_ok=True)
                    shot = _os.path.join(screenshot_dir, "%s.png" % pid)
                    page.screenshot(path=shot, full_page=True)
                    out["screenshot_path"] = shot
                except Exception as e:           # noqa: BLE001
                    out["screenshot_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        except Exception as e:                   # noqa: BLE001
            out.update({"error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                        "rule": "render_failed", "main_text": "", "boiler_text": ""})
        finally:
            for closer in (ctx.close, browser.close):
                try:
                    closer()
                except Exception:                # noqa: BLE001  close can hang too
                    pass
    return out


def _scroll_to_bottom(page, rounds: int = 3, quiet_ms: int = 2000) -> None:
    """Scroll to the bottom to trigger lazy loading. **Definite stopping condition:**
    the height stops growing, or the round budget runs out."""
    last = 0
    for _ in range(rounds):
        page.mouse.wheel(0, 20000)
        try:
            page.wait_for_load_state("networkidle", timeout=quiet_ms)
        except Exception:                        # noqa: BLE001
            pass
        h = page.evaluate("() => document.body.scrollHeight")
        if h == last:
            break
        last = h


def derive_anchors(title: str, vocab: list[str], df: dict[str, int],
                   n_pages: int, cap: int = 6, url: str = "") -> list[str]:
    """Anchors distinctive to one page: content words from the title plus terms that are
    rare across the whole set.

    With a corpus of only a hundred pages, true distinctiveness cannot be established, so
    document frequency across the set stands in as an approximation. This is a known
    limitation.
    """
    # The document-frequency threshold is tightened to 5%. At 20%, a term appearing on a
    # tenth of the corpus still qualifies, so a common format word slips into the anchors.
    # Loose anchors do not manufacture false losses — that direction is safe — but they
    # hollow out the wrong-page veto: returning a *different* form from the same agency
    # would still hit generic terms like line/see/tax.
    lim = max(1, int(n_pages * 0.05))
    # The last URL segment is the strongest identity signal, ahead of title and vocabulary
    slug_src = re.sub(r"[^a-z0-9]+", " ", (url or "").lower().rsplit("/", 2)[-1])
    out = [t for t in _content_terms(slug_src, set(), 4) if df.get(t, 0) <= lim]
    for t in _content_terms(title or "", set(), 6):
        if t not in out and df.get(t, 0) <= lim:
            out.append(t)
    rare = [t for t in vocab if t not in _STOP and df.get(t, 0) <= lim]
    for t in rare:
        if t not in out:
            out.append(t)
        if len(out) >= cap:
            break
    return out[:cap]
