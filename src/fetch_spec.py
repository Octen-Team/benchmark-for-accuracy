"""Declarative configuration for the fetch evaluation.

Every distribution table is transcribed **once**, here, and checked by assertions that
run at import time — a page set that drifts out of spec fails loudly instead of quietly
producing a report about the wrong pages.

Two rules that are easy to route around, stated up front:

  Thresholds live in `TH`.  Numbers baked into judge prompts must match it; `fetch_score`
                            asserts in both directions.
  Cross-cutting axes are not types.  Anti-bot defence cuts across social walls and WAFs;
                            protection strength cuts across all of them. Modelling those
                            as types would shatter the buckets, so they are independent
                            slice axes rather than members of `TYPE_COUNTS`.

Method and operating guide: `docs/benchmarks/fetch_eval.md`.
"""
from __future__ import annotations

# ── The five types (mutually exclusive; they drive the headline metric) ──────────
CATEGORY_TO_TYPE = {
    "Static Docs": "baseline",
    "E-commerce/SPA": "render",
    "Documents": "docfmt",
    "Social/Login": "antibot",
    "Defended (WAF/Reviews/Paywall)": "antibot",
    "Robustness": "reliability",
}
TYPE_COUNTS = {"baseline": 8, "render": 18, "docfmt": 22, "antibot": 42, "reliability": 10}

# ── Three sub-classes of anti-bot page ──────────────────────────────────────────
# "Passing" means something different for each, so averaging them together is
# meaningless:
#   waf         pass = the article body was retrieved
#   login_wall  pass = the pre-wall content was retrieved **and identified as a wall**;
#               content from behind the wall is not expected
#   paywall     pass = the free portion was retrieved; a full body is instead flagged
#               as suspicious_bypass
_LOGIN_WALL = ["x.com", "www.instagram.com", "www.linkedin.com", "www.tiktok.com",
               "www.facebook.com", "www.threads.com", "www.pinterest.com", "bsky.app"]
_PAYWALL = ["www.wsj.com", "www.bloomberg.com", "www.barrons.com", "www.nytimes.com",
            "www.sciencedirect.com", "medium.com"]
_WAF = ["stackoverflow.com", "math.stackexchange.com", "www.reddit.com", "www.quora.com",
        "www.g2.com", "www.trustpilot.com", "www.yelp.com", "www.tripadvisor.com",
        "www.glassdoor.com", "www.indeed.com", "www.crunchbase.com", "www.capterra.com",
        "www.goodreads.com", "www.zillow.com", "www.bbb.org", "www.sitejabber.com",
        "www.ticketmaster.com", "seatgeek.com", "www.producthunt.com", "www.wikihow.com",
        "www.reuters.com", "habr.com", "qiita.com", "www.leboncoin.fr", "www.idealo.de"]
ANTIBOT_SUBCLASS = {h: "login_wall" for h in _LOGIN_WALL}
ANTIBOT_SUBCLASS.update({h: "paywall" for h in _PAYWALL})
ANTIBOT_SUBCLASS.update({h: "waf" for h in _WAF})


# ── Cross-cutting axis: protection strength (anti-bot pages only) ───────────────
# Derived from the two ground-truth channels, never labelled by hand:
#   soft     a headless browser gets through
#   medium   headless is blocked, real Chrome gets through
#   hard     real Chrome is blocked too
#   unknown  the real-Chrome channel was not run — **never default this to hard**,
#            which would disguise "not measured" as "hardest"
STRENGTHS = frozenset({"soft", "medium", "hard", "unknown"})

# ── Per-page labels ─────────────────────────────────────────────────────────────
EXPECT = frozenset({"content", "error", "redirect_final"})
PROBES = frozenset({"oversize", "url_quirk", "raw_direct", "redirect",
                    "empty_thin", "encoding", "plain_http"})

# ── Failure taxonomy, plus a separate fault attribution ─────────────────────────
# `fault` exists so that failures caused by the harness itself — our own size cap, our
# own crashed normalizer, an expired key — can be reported separately instead of being
# charged to the provider. Without that column those show up as provider weakness.
FAILURE_REASONS = ("nothing_extractable", "anti_bot_blocked", "other", "our_size_cap",
                   "timeout_upstream", "rate_limited", "normalizer_crashed",
                   "content_type_or_404", "blocklisted_domain")
FAULTS = ("harness", "provider", "page")


# ── Thresholds (the single definition site) ─────────────────────────────────────
# This evaluation scores **fetch capability only**, not parsing quality, so the
# thresholds answer exactly one question: is this the substantive content of this page —
# not "how much of it" or "how cleanly".
TH = {
    # Here coverage is a **success gate**, not a completeness score: the low threshold
    # only separates "got the real content" from "got a shell / a wall / another page".
    "fetch_ok": 0.3,
    "fetch_lost": 0.05,
    "render_ok": 0.4,       # an SPA shell with no hydrated content is not the content
    "vocab_min": 12,        # below this, skip the mechanical gate and let the panel judge
    "slow_loss_ms": 10_000,
}

# ── Per-page labels: URL substring -> label. **Transcribed only here.** ─────────
# Probes are orthogonal labels and can hang off any type. On reliability pages they are
# the headline metric; elsewhere they are diagnostic columns.
PAGE_LABELS = {
    # The three pages where expect != content: correct behaviour is a clean error, or
    # following the chain to its endpoint.
    "httpbingo.org/status/404": {"expect": "error", "probes": ["empty_thin"]},
    "httpbingo.org/status/503": {"expect": "error", "probes": ["empty_thin"]},
    "httpbingo.org/redirect/3": {"expect": "redirect_final", "probes": ["redirect"]},
    # Very large bodies: silent truncation still passes under a coverage gate, as long
    # as the vocabulary falls in the retained part.
    "eur-lex.europa.eu/legal-content": {"probes": ["oversize"]},
    "w3.org/TR/html52/": {"probes": ["oversize"]},
    "arxiv.org/pdf/2005.14165.pdf": {"probes": ["oversize"]},
    "gutenberg.org/files/1342": {"probes": ["oversize", "raw_direct"]},
    # URL quirk: no .pdf suffix, and the URL serves a PDF viewer shell, not the file.
    "arxiv.org/pdf/1706.03762": {"probes": ["url_quirk"]},
    # Non-HTML direct links: does the provider force them through an HTML renderer?
    "raw.githubusercontent.com": {"probes": ["raw_direct"]},
    "wordpress.org/sitemap.xml": {"probes": ["raw_direct"]},
    "feeds.bbci.co.uk/news/rss.xml": {"probes": ["raw_direct"]},
    "go.dev/blog/feed.atom": {"probes": ["raw_direct"]},
    "jsonplaceholder.typicode.com": {"probes": ["raw_direct"]},
    "api.github.com/repos": {"probes": ["raw_direct"]},
    # Empty / very thin pages and directory listings: invent content, or report honestly?
    "example.com/": {"probes": ["empty_thin"]},
    "textfiles.com/computers/": {"probes": ["empty_thin", "plain_http"]},
    # Encoding
    "aozora.gr.jp": {"probes": ["encoding"]},
    # Pre-TLS era HTML
    "cs.cmu.edu/~rgs/alice-I.html": {"probes": ["plain_http"]},
}

_SUFFIX_DOC_TYPE = {
    ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".csv": "csv", ".md": "md", ".txt": "txt", ".atom": "atom",
}


def doc_type_from_url(url: str) -> tuple[str, str]:
    """Return (doc_type, rule_name). **Anything the URL cannot settle stays `unknown`**
    and is deferred to the observed content-type.

    `arxiv.org/pdf/1706.03762` has no suffix. Guessing "pdf" from the path would defeat
    the point of that page, which exists to test whether a provider returns the PDF
    viewer shell instead of the file itself. The rule name is recorded per page so it is
    always possible to check which level made the call.
    """
    low = url.lower().split("?")[0].split("#")[0]
    for suf, dt in _SUFFIX_DOC_TYPE.items():
        if low.endswith(suf):
            return dt, "suffix" + suf
    if low.endswith("rss.xml"):
        return "rss", "suffix_rss_xml"
    if low.endswith(".xml"):
        return "xml", "suffix_xml"
    if "api.github.com/" in low or "jsonplaceholder.typicode.com/" in low:
        return "json", "known_json_api"
    if "/pdf/" in low or "go.microsoft.com/fwlink" in low:
        return "unknown", "no_suffix_defer"
    return "html", "default_html"


assert all(set(v) <= {"expect", "probes"} for v in PAGE_LABELS.values())
assert all(p in PROBES for v in PAGE_LABELS.values() for p in v.get("probes", []))
assert all(v["expect"] in EXPECT for v in PAGE_LABELS.values() if "expect" in v)


_TYPES = frozenset(TYPE_COUNTS)


def assert_pageset(rows: list[dict]) -> None:
    """Marginal assertions on the page set. Run once before filling any cells, and
    again before freezing it."""
    assert len(rows) == 100, f"page set should hold 100 rows, found {len(rows)}"
    counts: dict[str, int] = {}
    for r in rows:
        assert r["type"] in _TYPES, f"unknown type: {r['type']}"
        assert r["expect"] in EXPECT, f"unknown expect: {r['expect']}"
        for p in r.get("probes") or []:
            assert p in PROBES, f"unknown probe: {p}"
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    assert counts == TYPE_COUNTS, f"type counts do not match: {counts} != {TYPE_COUNTS}"
    n_def = sum(1 for r in rows if r.get("defended"))
    assert n_def == 42, f"defended subset should hold 42 rows, found {n_def}"


# ── Import-time self-check: the config tables must agree with each other ────────
assert sum(TYPE_COUNTS.values()) == 100
assert set(CATEGORY_TO_TYPE.values()) <= _TYPES
assert len(_LOGIN_WALL) == 8, "the 10 Social/Login rows span 8 hosts (x.com and linkedin twice)"
assert len(_PAYWALL) == 6
assert len(_WAF) == 25, "the 32 Defended rows span 31 hosts (reddit twice), minus 6 paywalls"
assert len(ANTIBOT_SUBCLASS) == len(_LOGIN_WALL) + len(_PAYWALL) + len(_WAF), \
    "a host appears in more than one sub-class list"
assert len(FAILURE_REASONS) == 9 and len(set(FAILURE_REASONS)) == 9
