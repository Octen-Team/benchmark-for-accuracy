"""Adapters for the fetch/extract providers.

**Deliberately separate from the search backends.** That abstraction is
`search(query, k)`; this one is `fetch(url)`. Welding them together distorts both.

Three rules:

  A missing key fails hard.   Skipping silently produces a round with zero coverage and a
                              report showing zero faults.
  Cache freshness is pinned.  Every adapter explicitly requests a live fetch where the
                              API offers the knob. Unpinned, the latency column ranks
                              cache hit rates rather than fetch speed.
  Our own limits are harness  A size cap we imposed, or a normalizer of ours that
  faults.                     crashed, must not be charged to the provider. That is what
                              the `fault` field exists for.

**Providers whose endpoint has not been verified** carry only a skeleton. Inventing an
endpoint from memory yields a whole column of fake failures, which is worse than having
no column at all, so they raise on a missing key and say the shape is unverified. Filling
in `endpoint` / `_body` / `_pluck` from the official docs is all that is needed.
"""
from __future__ import annotations

import json
import os
import re
import time
from html import unescape as _html_unescape
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

from .backends import _load_dotenv
from .fetch_checks import len_norm as _len_norm

_load_dotenv()

# Well above the longest page in the set (a full public-domain book runs to roughly
# 700k characters). Hitting it records a harness fault rather than truncating silently.
MAX_TEXT_CHARS = 5_000_000
DEFAULT_TIMEOUT = 60


@dataclass
class FetchResponse:
    """Everything one fetch produced. These field names are shared by the runner that
    persists them, the scorer that consumes them and the report that aggregates them, so
    renaming one means renaming it in all of those places."""
    url: str
    provider: str
    status: str                      # "ok" | "error"
    text: str = ""
    len_norm: int = 0
    latency_ms: float = 0.0
    http_status: int | None = None
    error: str | None = None
    failure_reason: str | None = None    # one of the nine; can be set even when status=ok
    fault: str | None = None             # harness | provider | page
    cache_pinned: str = "unpinned"        # pinned | no_knob | unpinned
    raw_meta: dict = field(default_factory=dict)


# Wording that means the provider **refuses to serve a domain by policy**. That is a
# different thing from the target site blocking it:
#   blocklisted_domain  it will not fetch this domain for you — a **policy choice**
#   anti_bot_blocked    it tried and the target site stopped it — a **capability gap**
# For anyone choosing a provider these mean opposite things. Folding both into
# anti_bot_blocked makes "we do not do this business" look like "we cannot get through".
_POLICY_REFUSAL = re.compile(
    r"do not support this site|not supported|unsupported (site|domain|url)"
    r"|blocked by (our )?policy|domain is (not allowed|blocked|blacklisted)"
    r"|we (don't|do not) (allow|scrape)", re.I)


# Some providers wrap "the site banned us" in a custom status code. Left in `other`, a
# genuine block reads as an unexplained error, and those mean different things in the
# attribution table.
_SITE_BAN = re.compile(r'"title"\s*:\s*"Website Ban"|website ban|banned by (the )?(site|target)',
                       re.I)


def classify_body(code: int, body: str) -> tuple[str, str] | None:
    """The reason the response body **states explicitly**; None when it says nothing and
    the status code has to decide.

    The two categories mean different things in the attribution table:
      blocklisted_domain  it will not fetch this domain for you — a policy choice
      anti_bot_blocked    it tried and the target site blocked it — a capability gap
    """
    if not body:
        return None
    if _POLICY_REFUSAL.search(body):
        return "blocklisted_domain", "provider"
    if _SITE_BAN.search(body):
        return "anti_bot_blocked", "provider"
    return None


def _classify_http(code: int, body: str = "") -> tuple[str, str]:
    """HTTP status -> (failure_reason, fault). **An explicit reason in the body wins
    over the status code.**"""
    told = classify_body(code, body)
    if told:
        return told
    if code == 429:
        return "rate_limited", "provider"
    if code == 402:
        # An exhausted balance is **the state of our account**, not the provider's fetch
        # capability. Charging it to the provider scores our unpaid invoice as their
        # weakness.
        return "other", "harness"
    if code in (408, 425, 500, 502, 503, 504):
        return "timeout_upstream", "provider"
    if code in (404, 410, 415):
        return "content_type_or_404", "provider"
    if code == 451:
        return "blocklisted_domain", "provider"
    if code in (401, 403):
        return "anti_bot_blocked", "provider"
    return "other", "provider"


def _classify_exc(e: Exception) -> tuple[str, str]:
    if isinstance(e, requests.Timeout):
        return "timeout_upstream", "provider"
    if isinstance(e, (requests.ConnectionError, requests.exceptions.SSLError)):
        return "timeout_upstream", "provider"
    return "other", "provider"


class FetchProvider(ABC):
    """One provider. `fetch` always returns a FetchResponse; **only a missing key raises.**"""
    name: str = ""
    env_key: str | None = None
    # **Four states, not a boolean:**
    #   pinned    the knob exists and we set it to fetch live, bypassing the cache
    #   no_knob   verified: this API has no such parameter (or always fetches live)
    #   unknown   could not determine. Unknown parameters are silently ignored rather
    #             than rejected, so guessing a name would leave us believing the cache
    #             was off. Say "unknown" rather than pretend it is pinned.
    #   unpinned  the knob exists and we did not set it (should not occur once wired)
    # Collapsing this to a boolean makes "the provider lacks the capability" and "we
    # forgot to set it" look identical.
    cache_pinned: str = "unpinned"
    endpoint_verified: bool = True

    def _key(self) -> str:
        if not self.env_key:
            return ""
        v = os.environ.get(self.env_key, "").strip()
        if not v:
            hint = ("" if self.endpoint_verified else
                    "; note: this adapter's endpoint shape is unverified — fill it in "
                    "from the official docs once a key is available")
            raise RuntimeError(
                f"{self.name}: missing environment variable {self.env_key}{hint}")
        return v

    def _finish(self, url: str, text: str, t0: float, **kw) -> FetchResponse:
        """Final shaping: compute len_norm, apply our own size cap, attribute empty text to
    the page."""
        ms = (time.time() - t0) * 1000
        meta = kw.pop("raw_meta", {})
        if not (text or "").strip():
            return FetchResponse(url=url, provider=self.name, status="error", text="",
                                 latency_ms=ms, failure_reason="nothing_extractable",
                                 fault="page", cache_pinned=self.cache_pinned,
                                 raw_meta=meta, **kw)
        reason = fault = None
        if len(text) > MAX_TEXT_CHARS:
            # Keep what was retrieved. Discarding it would disguise our own cap as the
            # provider failing to retrieve anything.
            text = text[:MAX_TEXT_CHARS]
            reason, fault = "our_size_cap", "harness"
            meta = {**meta, "truncated": True}
        return FetchResponse(url=url, provider=self.name, status="ok", text=text,
                             len_norm=_len_norm(text), latency_ms=ms,
                             failure_reason=reason, fault=fault,
                             cache_pinned=self.cache_pinned, raw_meta=meta, **kw)

    def _fail(self, url: str, t0: float, *, error: str, reason: str, fault: str,
              http_status: int | None = None) -> FetchResponse:
        return FetchResponse(url=url, provider=self.name, status="error",
                             latency_ms=(time.time() - t0) * 1000,
                             http_status=http_status, error=error[:300],
                             failure_reason=reason, fault=fault,
                             cache_pinned=self.cache_pinned)

    @abstractmethod
    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        ...


class _HttpFetcher(FetchProvider):
    """Shared shell for HTTP providers. A subclass supplies endpoint / _headers /
    _body / _pluck and nothing else."""
    endpoint: str = ""
    output_form: str = "text"        # text | html — html is normalised, and declared
    auth_basic: bool = False         # key travels as HTTP Basic rather than a header
    jsonl_body: bool = False         # response is JSONL (one object per line), not JSON

    def _headers(self, key: str) -> dict:
        raise NotImplementedError

    def _body(self, url: str, timeout: int) -> dict:
        raise NotImplementedError

    def _pluck(self, data: dict) -> str:
        raise NotImplementedError

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        key = self._key()                       # a missing key raises here, never swallowed
        if not self.endpoint:
            raise RuntimeError(f"{self.name}: endpoint shape unverified — fill in "
                               "endpoint/_body/_pluck from the official docs")
        # The request body is built outside the try: a configuration error should fail
        # hard. Inside, it would be caught as a transport fault and recorded as one more
        # error row, disguising our misconfiguration as the provider failing.
        headers, body = self._headers(key), self._body(url, timeout)
        t0 = time.time()
        try:
            r = requests.post(self.endpoint, headers=headers, json=body,
                              auth=(key, "") if self.auth_basic else None,
                              timeout=timeout + 10)
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        if r.status_code >= 400:
            reason, fault = _classify_http(r.status_code, r.text or "")
            return self._fail(url, t0, error=(r.text or "")[:300], reason=reason,
                              fault=fault, http_status=r.status_code)
        try:
            if self.jsonl_body:
                # JSONL: one object per line. A whole-body json.loads raises, which gets
                # recorded as "normalizer_crashed" and disguises a response-shape
                # question as our parser breaking.
                rows = [json.loads(l) for l in (r.text or "").split("\n") if l.strip()]
                data = rows[0] if rows else {}
            else:
                data = r.json()
        except Exception as e:                  # noqa: BLE001
            return self._fail(url, t0, error=f"response is not JSON: {e}",
                              reason="normalizer_crashed", fault="harness",
                              http_status=r.status_code)
        try:
            text = self._pluck(data) or ""
        except Exception as e:                  # noqa: BLE001
            # A field-mapping error must be visible, not disguised as a failed fetch
            return self._fail(url, t0, error=f"field mapping failed: {type(e).__name__}: {e}",
                              reason="normalizer_crashed", fault="harness",
                              http_status=r.status_code)
        meta = {}
        if self.output_form == "html" and text:
            meta = {"output_form": "html", "raw_len": len(text)}
            text = html_to_text(text)
        return self._finish(url, text, t0, http_status=r.status_code, raw_meta=meta)


# ── Adapters whose request/response shape has been verified ───────────────

class OctenFetcher(_HttpFetcher):
    name = "octen"
    env_key = "OCTEN_API_KEY"
    endpoint = "https://api.octen.ai/extract"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"X-Api-Key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        # The API defaults to a day-long cache window. Zero is accepted and measurably
        # slower than a cached call, which confirms it really bypasses the cache. Use
        # zero rather than leaving a several-minute window open.
        return {"urls": [url], "format": "markdown", "max_age_seconds": 0,
                "timeout": min(timeout, 60)}

    def _pluck(self, data):
        res = (data.get("data") or {}).get("results") or []
        return (res[0].get("full_content") or res[0].get("text") or "") if res else ""


class FirecrawlFetcher(_HttpFetcher):
    name = "firecrawl"
    env_key = "FIRECRAWL_API_KEY"
    endpoint = "https://api.firecrawl.dev/v1/scrape"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url, "formats": ["markdown"], "maxAge": 0,
                "timeout": min(timeout, 60) * 1000}

    def _pluck(self, data):
        return (data.get("data") or {}).get("markdown") or ""


class ExaFetcher(_HttpFetcher):
    name = "exa"
    env_key = "EXA_API_KEY"
    endpoint = "https://api.exa.ai/contents"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        # **livecrawl must be set to always.** Left unset it defaults to a fallback mode
        # that consults the provider's own index first and returns immediately on a hit.
        # Always is measurably slower, which confirms it genuinely fetches. Without it
        # this column measures index coverage, not fetch capability.
        return {"urls": [url], "text": True, "livecrawl": "always"}

    def _pluck(self, data):
        res = data.get("results") or []
        return (res[0].get("text") or "") if res else ""


class BrightDataFetcher(_HttpFetcher):
    """Uses the **Datasets v3 scrape** endpoint (the `dataset_id` one), not the Web
    Unlocker `/request` endpoint.

    They are different products: `/request` needs an unlocker-type zone on the account,
    and an account with only residential-static or SERP zones cannot use it. Datasets v3
    is synchronous and returns **JSONL** with markdown / html2text / page_html fields;
    we take `markdown` so the shape matches the other providers.
    """
    name = "brightdata"
    env_key = "BRIGHTDATA_API_KEY"
    endpoint = ("https://api.brightdata.com/datasets/v3/scrape"
                "?dataset_id=%s&notify=false&include_errors=true")
    jsonl_body = True
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"input": [{"url": url}], "limit_per_input": None}

    def _pluck(self, data):
        return data.get("markdown") or data.get("html2text") or ""

    def fetch(self, url, timeout=DEFAULT_TIMEOUT):
        ds = os.environ.get("BRIGHTDATA_SCRAPE_DATASET", "").strip()
        if not ds:
            # No fake default: a guessed dataset_id buys a whole column of 400s that
            # looks like the provider failing to fetch
            raise RuntimeError("brightdata: BRIGHTDATA_SCRAPE_DATASET is required "
                               "(the Datasets v3 dataset_id)")
        self.endpoint = type(self).endpoint % ds
        return super().fetch(url, timeout)


class TavilyFetcher(_HttpFetcher):
    """An optional extra provider: it exposes an /extract endpoint of its own."""
    name = "tavily"
    env_key = "TAVILY_API_KEY"
    endpoint = "https://api.tavily.com/extract"
    # The documented /extract parameters contain **no** cache-control option. Note that
    # unknown parameters are silently ignored rather than rejected, so guessing a name
    # would leave us believing the cache was off. Report it honestly as no_knob.
    cache_pinned = "no_knob"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url], "extract_depth": "advanced"}

    def _pluck(self, data):
        res = data.get("results") or []
        return (res[0].get("raw_content") or "") if res else ""


# ── Local libraries (no key required) ─────────────────────────────────────

def _import_trafilatura():
    try:
        import trafilatura
        return trafilatura
    except ImportError:
        return None


def _import_readability():
    try:
        import html2text
        from readability import Document
        return Document, html2text
    except ImportError:
        return None


NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}

_TAG = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Normalise HTML into text.

    Some providers return HTML while others return markdown or plain text. Since only
    fetch capability is scored, HTML-to-text is a parsing step and out of scope. Without
    this normalisation those providers would be judged as having returned a raw payload
    and marked lost across the board, which measures output format rather than fetch
    capability. The step is declared in the report via the `output_form` field.
    """
    if not html:
        return ""
    s = _TAG.sub(" ", html)
    s = _ANY_TAG.sub(" ", s)
    s = _html_unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n\s*\n+", "\n\n", s)).strip()


class TrafilaturaFetcher(FetchProvider):
    name = "trafilatura"
    cache_pinned = "pinned"

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        t0 = time.time()
        mod = _import_trafilatura()
        if mod is None:
            # A missing dependency is our fault, not a failure to fetch — a fault is
            # reported, never scored as zero
            return self._fail(url, t0,
                              error="trafilatura is not installed (requirements-fetch.txt)",
                              reason="normalizer_crashed", fault="harness")
        try:
            raw = mod.fetch_url(url)
            text = mod.extract(raw, include_tables=True, include_links=True) if raw else ""
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        return self._finish(url, text or "", t0)


class ReadabilityFetcher(FetchProvider):
    name = "readability"
    cache_pinned = "pinned"

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        t0 = time.time()
        mods = _import_readability()
        if mods is None:
            return self._fail(url, t0,
                              error="readability-lxml / html2text are not installed",
                              reason="normalizer_crashed", fault="harness")
        Document, html2text = mods
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; search-eval/1.0)",
                                      **NO_CACHE_HEADERS})
            if r.status_code >= 400:
                reason, fault = _classify_http(r.status_code, r.text or "")
                return self._fail(url, t0, error=(r.text or "")[:300], reason=reason,
                                  fault=fault, http_status=r.status_code)
            h = html2text.HTML2Text()
            h.body_width = 0
            text = h.handle(Document(r.text).summary())
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        return self._finish(url, text or "", t0)


# ── Providers with an unverified endpoint shape: skeleton only, hard-fail on key ──

# ── Adapters whose endpoint and parameters were **verified by observed behaviour**,
#    not filled in from memory ───────────────────────────────────────────────

class ZyteFetcher(_HttpFetcher):
    """`browserHtml` renders in a real browser on every call, so there is no cache to
    bypass. The alternatives are a raw base64 response with no JavaScript, and the
    provider's own structured article extraction, which is nearly empty on non-article
    pages. Under a fetch-capability metric, browserHtml is the comparable one."""
    name = "zyte"
    env_key = "ZYTE_API_KEY"
    endpoint = "https://api.zyte.com/v1/extract"
    auth_basic = True                # key as the username, empty password
    output_form = "html"
    cache_pinned = "no_knob"

    def _headers(self, key):
        return {"Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url, "browserHtml": True}

    def _pluck(self, data):
        return data.get("browserHtml") or ""


class YouFetcher(_HttpFetcher):
    """The field is `urls` (a plural array); the API's own validation error states the
    schema. Returns HTML."""
    name = "you"
    env_key = "YOU_API_KEY"
    endpoint = "https://api.you.com/v1/contents"
    output_form = "html"
    cache_pinned = "unknown"         # no freshness parameter found; reported honestly

    def _headers(self, key):
        return {"X-API-Key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url]}

    def _pluck(self, data):
        items = data if isinstance(data, list) else (data.get("results") or [])
        return (items[0].get("html") or items[0].get("text") or "") if items else ""


class LinkupFetcher(_HttpFetcher):
    """`/v1/fetch` returns clean markdown directly. **It does not validate unknown
    parameters** — a nonsense field still returns 200 — so a cache knob cannot be found
    by guessing names: a wrong guess is ignored while we believe it took effect."""
    name = "linkup"
    env_key = "LINKUP_API_KEY"
    endpoint = "https://api.linkup.so/v1/fetch"
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url}

    def _pluck(self, data):
        return data.get("markdown") or data.get("rawHtml") or ""


class ParallelFetcher(_HttpFetcher):
    """Uses `v1beta/extract` with `full_content`, **not the objective mode of
    `v1/extract`**.

    On the same URL, the objective mode returns excerpts narrowed to the stated question,
    while full_content returns the whole page. `objective` is query-driven extraction,
    and a fetch-capability evaluation needs the **whole page**: scoring a
    question-narrowed excerpt conflates "how much was retrieved" with "how well it
    answered". The v1 endpoint rejects `full_content` outright, so full text requires
    v1beta."""
    name = "parallel"
    env_key = "PARALLEL_API_KEY"
    endpoint = "https://api.parallel.ai/v1beta/extract"
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url], "full_content": True}

    def _pluck(self, data):
        res = data.get("results") or []
        if not res:
            return ""
        r = res[0]
        return r.get("full_content") or "\n".join(r.get("excerpts") or [])



class _UnverifiedFetcher(_HttpFetcher):
    # When wiring one of these up, **find and set its live-fetch knob at the same time**.
    # Left unset, the column measures index coverage rather than fetch capability.
    """Once a key is available, fill in endpoint / _headers / _body / _pluck from the
    official docs. Until then the endpoint stays unset: a wrong guess produces a whole
    column of fake failures."""
    endpoint = ""
    endpoint_verified = False
    cache_pinned = "unpinned"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url}

    def _pluck(self, data):
        raise NotImplementedError(f"{self.name}: response text path unverified")



class ContextFetcher(_UnverifiedFetcher):
    name = "context"
    env_key = "CONTEXT_API_KEY"





class CloudflareFetcher(_UnverifiedFetcher):
    name = "cloudflare"
    env_key = "CLOUDFLARE_API_TOKEN"


class ApifyFetcher(_UnverifiedFetcher):
    name = "apify"
    env_key = "APIFY_API_TOKEN"


FETCHERS: dict[str, type[FetchProvider]] = {
    c.name: c for c in (
        OctenFetcher, FirecrawlFetcher, ExaFetcher, BrightDataFetcher, TavilyFetcher,
        TrafilaturaFetcher, ReadabilityFetcher,
        ParallelFetcher, ContextFetcher, YouFetcher, LinkupFetcher,
        ZyteFetcher, CloudflareFetcher, ApifyFetcher,
    )
}

# The full provider roster this lane knows about
ROSTER_13 = ("firecrawl", "context", "octen", "parallel", "exa", "you", "linkup",
             "zyte", "cloudflare", "brightdata", "readability", "apify", "trafilatura")

# Providers that can run now: a key is present and both the endpoint and the live-fetch
# parameter were verified by observed behaviour. When adding one, find and set its
# live-fetch knob at the same time — unset, the column measures index coverage rather
# than fetch capability.
RUNNABLE_TODAY = ("octen", "exa", "tavily", "trafilatura", "readability",
                  "zyte", "you", "linkup", "parallel", "firecrawl", "brightdata")



def env_divergence(providers=None) -> dict[str, tuple[str, str]]:
    """Credentials that differ between `.env` and the shell environment. Returns
    {variable: (value in .env, value in the shell)}, masked.

    **`_load_dotenv` uses `os.environ.setdefault` and does not override variables that
    already exist.** A stale key left in the shell therefore wins silently over the new
    one in `.env` while both look correctly configured. The symptom is an entire round
    failing on authorisation against a key that was never actually used.

    Secrets never reach the log: only a masked prefix and suffix are returned, enough for
    a person to recognise which key is which.
    """
    from pathlib import Path
    path = Path(__file__).parent.parent / ".env"
    if not path.exists():
        return {}
    want = None
    if providers:
        want = {FETCHERS[p].env_key for p in providers
                if p in FETCHERS and FETCHERS[p].env_key}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if want is not None and k not in want:
            continue
        shell = os.environ.get(k)
        if shell is not None and shell != v:
            m = lambda x: (x[:6] + "…" + x[-4:]) if x and len(x) > 12 else "(short)"
            out[k] = (m(v), m(shell))
    return out


def get_fetcher(name: str) -> FetchProvider:
    return FETCHERS[name]()


assert set(ROSTER_13) <= set(FETCHERS), "a roster name has no corresponding adapter"
assert len(ROSTER_13) == 13
assert set(RUNNABLE_TODAY) <= set(FETCHERS)
