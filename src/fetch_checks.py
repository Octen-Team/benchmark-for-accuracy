"""Mechanical layer: pure functions. No network, no keys, no LLM, so all unit-testable.

**Fetch capability only — parsing quality is out of scope.** Text purity, structural
fidelity and truncation completeness were removed. An unscored metric left sitting in
the code and the report reads as though it counts.

**An empty denominator returns None, never 0.0.** A ratio over an empty set disguises
itself as "everything passed" or "everything failed"; either way the reader is misled.
None means "undefined for this page", and the report prints it as unlabelled rather
than 0%.

`wall_hit` is the one check that **does not feed the verdict**. Treating the mere
presence of a vendor name like "cloudflare" as evidence of blocking judges that
vendor's own documentation as a wall. Wall detection belongs to the ground truth and
the judging panel.
"""
from __future__ import annotations

import re
import unicodedata

from .fetch_spec import TH

_WORD = re.compile("[0-9a-z\u00c0-\u024f]+")
_CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# Mojibake signature: UTF-8 misread as latin-1 — lead byte in C2-EF, continuation in C1
_MOJIBAKE = re.compile("[\u00c2-\u00ef][\u0080-\u00bf]")
_REPLACEMENT = "\ufffd"

_WALL_PATTERNS = {
    "challenge": r"checking your browser|just a moment|verify you are human"
                 r"|enable javascript and cookies",
    "captcha": r"\bcaptcha\b|recaptcha|hcaptcha",
    "login": r"log in to continue|sign in to continue|create an account to",
    "paywall": r"subscribe to (read|continue)|this article is for subscribers",
    "ratelimit": r"too many requests|rate limit exceeded",
}


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation. CJK is tokenised per character, the rest per word."""
    if not text:
        return []
    t = unicodedata.normalize("NFKC", text).lower()
    out: list[str] = []
    for chunk in t.split():
        if _CJK.search(chunk):
            out.extend(_CJK.findall(chunk))
            out.extend(_WORD.findall(_CJK.sub(" ", chunk)))
        else:
            out.extend(_WORD.findall(chunk))
    return out


def len_norm(text: str) -> int:
    """CJK counted per character, everything else per whitespace-delimited word.

    `len(text.split())` is useless for Japanese — a whole sentence counts as one word —
    and the page set contains Japanese pages. Length is a reference figure only and is
    never ranked across providers.
    """
    if not text:
        return 0
    return len(_CJK.findall(text)) + len(_WORD.findall(_CJK.sub(" ", text.lower())))


def _ratio(text: str, terms: list[str] | None) -> float | None:
    if not terms:
        return None
    got = set(tokenize(text))
    return sum(1 for t in terms if t.lower() in got) / len(terms)


def coverage(text: str, vocab: list[str] | None) -> float | None:
    """Recall: how much of the ground-truth content vocabulary came back."""
    return _ratio(text, vocab)


def render_hit(text: str, anchors: list[str] | None) -> float | None:
    """Hit rate over anchors that only appear **after rendering** — the success gate
    for SPA pages.

    This still measures fetch capability, not parsing quality: a provider that returns
    the server-side shell can score respectable coverage (the nav alone carries product
    names) while none of the prices, stock counts or list items that JavaScript produces
    are present. It did not retrieve the page, only its shell.
    """
    return _ratio(text, anchors)


def identity_ok(text: str, anchors: list[str] | None) -> bool | None:
    """Is this the content of *this* URL? More than half the distinctive anchors must hit.

    Catches the silent failure where a provider falls back to the parent page, an index,
    or a search-results page. That is worse than an outright failure, because nothing
    downstream can tell.
    """
    r = _ratio(text, anchors)
    return None if r is None else r >= 0.5


def encoding_ok(text: str) -> bool:
    """Self-contained: inspects the text alone and needs no ground truth, so it still
    applies on defended pages where ground truth is weak or missing."""
    if not text:
        return True
    if text.count(_REPLACEMENT) >= 3:
        return False
    return not _MOJIBAKE.search(text)


def wall_hit(text: str) -> list[str]:
    """**Recorded as evidence only; never feeds the verdict.** Wall detection belongs
    to the ground truth and the panel."""
    low = (text or "").lower()[:20000]
    return sorted(n for n, pat in _WALL_PATTERNS.items() if re.search(pat, low))
