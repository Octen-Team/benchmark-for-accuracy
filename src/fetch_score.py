"""Fetch-success judging: a mechanical layer plus a three-model panel.

**Fetch capability only.** The headline metric answers exactly one question: was this
page retrieved. Text purity, structural fidelity and truncation completeness are not
scored — they are parsing quality, and their code was removed rather than left dormant,
because an unscored metric sitting in the report reads as though it counts.

Because a single unit is left, the score is comparable across page types, so a total is
meaningful; the five types become slice axes rather than five separate headline metrics.

Judging has two strictly separated layers:
  Mechanical  a band computed by pure functions. Reproducible, traceable to evidence.
  Panel       handles only the cells the mechanical layer could not settle. Three models
              from three different families judge blind, majority wins, and a three-way
              split keeps the mechanical verdict. Rulings are cached by fingerprint so a
              re-run does not spend tokens twice.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock

from . import fetch_checks as C
from .fetch_spec import TH

RUBRIC_VERSION = "fetch-v3-20260901"   # v3: fetch success only
PANEL_TEXT_BUDGET = 6000

_PASS, _PARTIAL, _LOST = "pass", "partial", "lost"
WEIGHT = {_PASS: 1.0, _PARTIAL: 0.5, _LOST: 0.0}


def _v(verdict, reason, **kw) -> dict:
    """`final=True` marks a mechanical verdict as terminal: no panel review."""
    return {"verdict": verdict, "reason": reason, "needs_panel": False, "final": False,
            "dishonest": False, "suspicious_bypass": False, **kw}


# On a gap page only the **identity** fields remain usable. The content vocabulary was
# derived from whatever our own browser received, which on a blocked page is the
# challenge screen; using that as the reference marks providers that genuinely got the
# content as lost.
_WEAK_KEYS = ("anchors", "anchor_source", "serp_title", "serp_snippet", "title")


def effective_gt(page: dict) -> dict:
    """The ground truth that may be used for judging. Both the mechanical layer and the
    panel must go through this one accessor.

    Gap pages come in two kinds:
      Weak anchors from a neutral SERP  keep only identity fields (`anchors` and
                                        friends), **never the content vocabulary**.
                                        Identity is still decidable ("is this that
                                        page?"); completeness is not.
      No anchors at all                 return empty; only the cross-provider panel can
                                        judge these.
    """
    gt = page.get("gt") or {}
    if not gt.get("gt_gap"):
        return gt
    if gt.get("anchor_source") == "serp" and gt.get("anchors"):
        return {k: gt[k] for k in _WEAK_KEYS if k in gt}
    return {}


def run_checks(page: dict, resp: dict) -> dict:
    """The mechanical checks computable for this cell. Anything the ground truth cannot
    support returns None ("undefined"), never 0."""
    gt = effective_gt(page)
    text = resp.get("text") or ""
    return {
        "coverage": C.coverage(text, gt.get("vocab")),
        "render_hit": C.render_hit(text, gt.get("render_anchors")),
        "identity_ok": C.identity_ok(text, gt.get("anchors")),
        "encoding_ok": C.encoding_ok(text),
        "len_norm": C.len_norm(text),
        "wall_hit": C.wall_hit(text),
        "degenerate": bool(gt.get("degenerate")),
    }


def _band(value: float | None, ok: float, lost: float) -> str | None:
    if value is None:
        return None
    if value >= ok:
        return _PASS
    if value < lost:
        return _LOST
    return _PARTIAL


def _verdict_expect_error(page: dict, resp: dict, ch: dict) -> dict:
    """Pages where `expect == "error"` (404 / 503): **the correct behaviour is a clean
    error.**

    Returning the error page's body as content is wrong — downstream receives a chunk of
    "Not Found" and treats it as content, with no way to tell. When the metric only
    measures success, this "did not get it but claims it did" case is the one that most
    needs to be visible.
    """
    if resp.get("status") == "error":
        return _v(_PASS, "clean_error", final=True)
    text = (resp.get("text") or "").lower()
    if any(k in text for k in ("404", "not found", "503", "unavailable", "error")):
        return _v(_LOST, "error_page_returned_as_content", dishonest=True)
    return _v(_PARTIAL, "content_returned_for_an_error_url", needs_panel=True)


def _verdict_expect_redirect(page: dict, resp: dict, ch: dict) -> dict:
    if resp.get("status") == "error":
        return _v(_LOST, "did_not_follow_redirect", final=True)
    if ch["coverage"] is None:
        return _v(None, "no_gt_for_redirect_target", needs_panel=True)
    return _v(_band(ch["coverage"], TH["fetch_ok"], TH["fetch_lost"]), "redirect_target")


def mechanical_verdict(page: dict, resp: dict, ch: dict) -> dict:
    """Was the page retrieved? `verdict=None` means the mechanical layer cannot decide
    and the cell goes to the panel."""
    expect = page.get("expect", "content")
    if expect == "error":
        return _verdict_expect_error(page, resp, ch)
    if expect == "redirect_final":
        return _verdict_expect_redirect(page, resp, ch)

    if resp.get("status") == "error":
        # A transport failure is terminal: there is no text, so the panel has nothing to
        # judge and sending it would only spend tokens.
        return _v(_LOST, "fetch_failed:%s" % (resp.get("failure_reason") or "error"),
                  final=True)

    # ── Two hard vetoes. Under the question "was the page retrieved", both mean no. ──
    # These are terminal (`final=True`): the evidence is mechanical and certain, so they
    # are not sent for panel review. Sending them gets them overturned — a parser that
    # returns raw PDF bytes fails the encoding check, but the panel sees only a preview
    # of `%PDF-1.4 ... obj ... stream` and reads it as "content arrived".
    if ch["identity_ok"] is False:
        # This is not the content of this URL. It can slip past a coverage gate, which
        # makes it worse than an outright failure: nothing downstream notices.
        #
        # **A mismatch found via weak anchors (a SERP title/snippet) is not terminal.**
        # Search-engine titles go stale, so the evidence is not strong enough for a
        # verdict with no appeal; those go to the panel.
        weak = (page.get("gt") or {}).get("anchor_source") == "serp"
        return _v(_LOST, "wrong_page" + ("_weak" if weak else ""),
                  dishonest=not weak, final=not weak, needs_panel=weak)
    if ch["encoding_ok"] is False:
        return _v(_LOST, "mojibake", final=True)

    # ── Anti-bot pages: "passing" differs per sub-class, so the mechanical layer
    #    cannot decide and the panel takes over. ────────────────────────────────
    if page["type"] == "antibot":
        sus = (page.get("antibot_subclass") == "paywall" and ch["len_norm"] > 1500)
        return _v(None, "needs_wall_judgement", needs_panel=True, suspicious_bypass=sus)

    # ── SPA: retrieving the server-side shell is not retrieving the page. ──────
    if page["type"] == "render":
        if ch["render_hit"] is None:
            # Without render anchors there is no way to separate shell from content:
            # coverage would pass the shell. The panel must decide; falling back to the
            # generic gate would be wrong.
            return _v(None, "no_render_anchors", needs_panel=True)
        return _v(_band(ch["render_hit"], TH["render_ok"], TH["fetch_lost"]),
                  "render_anchors")

    # ── Everything else: coverage as the success gate. The threshold is deliberately
    #    low; it only separates real content from an empty shell. ─────────────
    if ch["degenerate"] or ch["coverage"] is None:
        return _v(None, "no_mechanical_basis", needs_panel=True)
    out = _v(_band(ch["coverage"], TH["fetch_ok"], TH["fetch_lost"]), "content_present")
    if out["verdict"] is None:
        out["needs_panel"] = True
    return out


# ── Panel layer ───────────────────────────────────────────────────────────

_SHARED_ANCHOR = """The only question is whether this fetch GOT THE PAGE. Do not grade
formatting, tidiness, or how completely the page was captured.

  pass     the substantive content of this URL came through
  partial  some of it came through: a wall-front excerpt, a fragment, a partial render
  lost     nothing usable came through: blocked, empty, an error, the wrong page, or garbled

Raw unrendered payload returned verbatim -- HTML source, a PDF byte stream
(`%PDF-... obj ... stream`), base64 blobs -- is NOT content. That is `lost`, however
faithfully the bytes were transferred: the caller asked for the page, not the file.

A long, tidy extraction of the WRONG thing is `lost`, not `pass`. A short extraction of
the right thing is `pass` if the page itself is short. Missing sections, lost tables or
stripped formatting do NOT reduce the verdict -- we are not grading parsing this round."""

_TYPE_RUBRIC = {
    "content": """This is an ordinary page (static doc, document file, or an awkward input
like a huge body / odd content type / very thin page). Did its actual content come through?""",
    "render": """This page builds its content with JavaScript. An extraction holding only
the server-rendered shell (nav, footer, boilerplate, a loading state) did NOT get the page,
however long it is -- that is `lost`.""",
    "antibot": """This page is defended. What counts as getting through depends on the wall:
  waf         pass = the actual page body came through
  login_wall  pass = the publicly visible portion came through AND the extraction makes it
              evident that a wall exists. Content that could only come from behind a login
              is NOT a better result -- flag it.
  paywall     pass = the free portion came through. Full article text from behind a paywall
              is NOT a better result -- flag it.
An extraction that returns the wall page itself (challenge screen, login prompt, subscribe
prompt) AS IF it were the content is `lost`, and is worse than an honest failure:
downstream cannot tell it went wrong.""",
}

_RUBRIC_FOR = {"antibot": "antibot", "render": "render"}


def rubric_key(page_type: str) -> str:
    return _RUBRIC_FOR.get(page_type, "content")


def panel_system(page_type: str) -> str:
    """The judging rubric. The shared anchor text is identical word for word across
    variants; the rubric only branches where "passing" genuinely means something
    different.

    Three variants, not five: static docs, document files and robustness edge cases all
    ask the same question (did the content arrive), so writing one rubric each would only
    add wording that can drift apart.
    """
    return ("You are grading one web fetch. We are measuring FETCH CAPABILITY only.\n\n"
            + _SHARED_ANCHOR + "\n\n" + _TYPE_RUBRIC[rubric_key(page_type)]
            + '\n\nOutput JSON only: {"verdict": "pass"|"partial"|"lost", '
              '"dishonest": true|false, "why": "<one short clause>"}\n'
              '"dishonest" is true only when the extraction presents a wall page, an error '
              'page, or a different page AS IF it were the requested content.')


def panel_user(page: dict, resp: dict, ch: dict) -> str:
    gt = effective_gt(page)
    text = (resp.get("text") or "")[:PANEL_TEXT_BUDGET]
    parts = [
        "URL: %s" % page.get("url", ""),
        "Page title (from our own render): %s" % (gt.get("title") or "unknown"),
    ]
    if page.get("antibot_subclass"):
        parts.append("Wall type: %s" % page["antibot_subclass"])
    if gt.get("vocab"):
        parts.append("Terms our reference render found in the page body: %s"
                     % ", ".join(gt["vocab"][:40]))
    elif gt.get("serp_title"):
        # With no full-text reference, a neutral search engine's title and snippet are
        # the only identity signal available. The panel must see them, or it can only ask
        # "does this look like web content", which is a far more lenient question.
        parts.append("We could not fetch this page ourselves, so there is no reference "
                     "body text. What a neutral search engine has indexed for this exact "
                     "URL:\n  title:   %s\n  snippet: %s"
                     % (gt["serp_title"], gt.get("serp_snippet") or "(none)"))
        parts.append("Use that to decide whether this is the right page. It does not tell "
                     "you how complete the extraction is, and completeness is not graded.")
    else:
        parts.append("We have NO reference for this page (our own browser was blocked and "
                     "search engines have not indexed it). Judge the extraction on its own "
                     "terms: does it look like the substantive content of this URL?")
    parts.append("\n--- FETCH UNDER TEST (%d chars, truncated for review) ---\n%s"
                 % (len(resp.get("text") or ""), text))
    return "\n".join(parts)


def judge_cache_key(page: dict, resp: dict, model: str, user: str = "") -> str:
    """Fingerprint = page + provider + fetched text + **the exact user prompt the panel
    saw** + rubric version + model.

    `user` has to be in the key. After a ground-truth fix, a key that omits it would pull
    the old, wrong ruling straight back out of the cache.
    """
    h = hashlib.sha256()
    for part in (page.get("pid", ""), resp.get("provider", ""),
                 resp.get("text") or "", user, RUBRIC_VERSION, model):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class PanelCache:
    """Ruling cache. Appended row by row so a re-run does not spend tokens twice.
    **Thread-safe.**"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = Lock()
        self.mem: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").split("\n"):
                try:
                    r = json.loads(line)
                    self.mem[r["key"]] = r["value"]
                except Exception:                # noqa: BLE001
                    continue

    def get(self, key: str):
        return self.mem.get(key)

    def put(self, key: str, value: dict) -> None:
        with self.lock:
            self.mem[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "value": value},
                                   ensure_ascii=False) + "\n")


def panel_verdict(page: dict, resp: dict, ch: dict, panel: list[str], *,
                  cache: PanelCache | None = None, max_tokens: int = 500) -> dict:
    """Three models from three families, judging blind. **They are shown neither the
    mechanical verdict nor each other's rulings.**"""
    from .llm import call_llm_json
    system = panel_system(page["type"])
    user = panel_user(page, resp, ch)
    votes: dict[str, dict] = {}
    for model in panel:
        key = judge_cache_key(page, resp, model, user)
        hit = cache.get(key) if cache else None
        if hit is not None:
            votes[model] = hit
            continue
        try:
            v = call_llm_json(system, user, model=model, max_tokens=max_tokens,
                              temperature=0.0)
            v = {"verdict": (v.get("verdict") or "").strip(),
                 "dishonest": bool(v.get("dishonest")),
                 "why": (v.get("why") or "")[:200]}
        except Exception as e:                   # noqa: BLE001
            v = {"verdict": "error", "dishonest": False,
                 "why": "%s: %s" % (type(e).__name__, str(e)[:120])}
        votes[model] = v
        if cache and v["verdict"] != "error":
            cache.put(key, v)
    return aggregate_votes(votes)


def aggregate_votes(votes: dict[str, dict]) -> dict:
    """Majority vote. **A three-way split is not forced into a decision**: it returns
    split=True and the caller keeps the mechanical verdict."""
    valid = [v["verdict"] for v in votes.values() if v.get("verdict") in WEIGHT]
    if not valid:
        return {"verdict": None, "panel_split": True, "dishonest": False, "votes": votes}
    counts = {v: valid.count(v) for v in set(valid)}
    top = max(counts, key=lambda k: counts[k])
    if counts[top] < 2:
        return {"verdict": None, "panel_split": True, "dishonest": False, "votes": votes}
    dis = sum(1 for v in votes.values() if v.get("dishonest")) >= 2
    return {"verdict": top, "panel_split": False, "dishonest": dis, "votes": votes}


class GoldStore:
    """Human-verified verdicts. **Highest priority**, overriding both the mechanical
    layer and the panel.

    Guarded by `text_sha`: if the fetched text changed (a new round was run), the gold
    entry expires automatically. A person judged one specific payload, and that judgement
    does not carry over to a different one.
    """

    def __init__(self, path: str | Path | None):
        self.by_key: dict[tuple[str, str], dict] = {}
        if not path:
            return
        p = Path(path)
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:                    # noqa: BLE001
                continue
            if r.get("verdict") in WEIGHT:
                self.by_key[(r["pid"], r["provider"])] = r

    def lookup(self, page: dict, resp: dict, provider: str | None = None) -> str | None:
        """An explicit `provider` wins. The cross-provider path keys by dict entry, so
        if the payload's own provider field disagrees, the lookup would return another
        provider's gold."""
        r = self.by_key.get((page.get("pid"), provider or resp.get("provider")))
        if not r:
            return None
        want = r.get("text_sha")
        if want:
            got = hashlib.sha256((resp.get("text") or "")
                                 .encode("utf-8", "replace")).hexdigest()[:16]
            if got != want:
                return None                      # text changed; the gold no longer applies
        return r["verdict"]

    def __len__(self) -> int:
        return len(self.by_key)


def _apply_gold(rec: dict, page: dict, resp: dict, gold, provider: str | None = None) -> bool:
    v = gold.lookup(page, resp, provider) if gold else None
    if v is None:
        return False
    rec["verdict"] = v
    rec["reason"] = "human_gold"
    rec["dishonest"] = rec["dishonest"] and v == _LOST
    rec["panel"] = None
    rec["panel_split"] = False
    return True


def score_one(page: dict, resp: dict, panel: list[str] | None = None, *,
              cache: PanelCache | None = None, gold: "GoldStore | None" = None) -> dict:
    """The full verdict for one cell: mechanical layer, then the panel if needed."""
    ch = run_checks(page, resp)
    mech = mechanical_verdict(page, resp, ch)
    out = {"pid": page["pid"], "provider": resp.get("provider"), "type": page["type"],
           "antibot_subclass": page.get("antibot_subclass"),
           "strength": (page.get("gt") or {}).get("strength"),
           "checks": ch, "mechanical": mech, "verdict": mech["verdict"],
           "dishonest": mech["dishonest"], "suspicious_bypass": mech["suspicious_bypass"],
           "reason": mech["reason"], "panel": None, "panel_split": False,
           "latency_ms": resp.get("latency_ms"), "len_norm": ch["len_norm"],
           "failure_reason": resp.get("failure_reason"), "fault": resp.get("fault"),
           "run_seq": resp.get("run_seq", 0)}
    if _apply_gold(out, page, resp, gold):
        return out                               # human verdict wins; skip the panel
    clean_pass = mech["verdict"] == _PASS and not mech["needs_panel"]
    if clean_pass or mech.get("final") or not panel:
        if out["verdict"] is None:
            # No panel and no mechanical basis: leave it genuinely unjudged.
            # **It is not scored as 0.**
            out["reason"] = mech["reason"] + "|unjudged"
        return out
    pv = panel_verdict(page, resp, ch, panel, cache=cache)
    out["panel"] = pv
    out["panel_split"] = pv["panel_split"]
    if pv["verdict"] is not None:
        out["verdict"] = pv["verdict"]
        out["dishonest"] = out["dishonest"] or pv["dishonest"]
        out["reason"] = "panel"
    return out


assert set(_TYPE_RUBRIC) == {"content", "render", "antibot"}
assert set(_RUBRIC_FOR.values()) <= set(_TYPE_RUBRIC)


# ══════════════════════════════════════════════════════════════════════════
# Cross-provider judging, for pages that have no reference answer
# ══════════════════════════════════════════════════════════════════════════

CROSS_TEXT_BUDGET = 2200          # characters shown per provider (several must fit)

CROSS_SYSTEM = """Several different services each tried to fetch the SAME web page.
You are shown what each returned. We could not fetch this page ourselves, so there is no
reference answer -- but you have the returns side by side, and that is the point: if any of
them got the real page, the ones that did not will stand out against it.

For each labelled return, decide:

  pass     this is the substantive content of that URL
  partial  part of it: a wall-front excerpt, a fragment, a partial render
  lost     not the page: a bot-check or login or subscribe screen, an error page, an empty
           shell, a DIFFERENT page (the site's home page, a listing, an onboarding screen),
           raw unrendered payload (HTML source, a `%PDF ... stream` byte dump), or garbled text

**Default to `lost`.** Only move a return up when there is positive evidence it is this
specific page's own content -- matching topic, matching entity, detail that could only come
from this URL. Length is not evidence. Tidiness is not evidence. Looking generally
web-like is not evidence.

We are grading FETCH CAPABILITY, not parsing: missing sections, lost tables and stripped
formatting do NOT lower a verdict.

Output JSON only, one entry per label you were shown:
{"verdicts": {"A": {"verdict": "pass|partial|lost", "dishonest": true|false,
                    "why": "<one short clause>"}, "B": {...}}}
"dishonest" is true when the return presents a wall, an error, or a different page AS IF it
were the requested content."""


def _cross_order(pid: str, providers: list[str]) -> list[str]:
    """Label order is derived from a hash of (pid, provider): **stable, but different on
    every page.**

    A fixed order would land the panel's position bias on the same provider every time;
    a random order would not be reproducible.
    """
    return sorted(providers,
                  key=lambda p: hashlib.sha256((pid + "|" + p).encode()).hexdigest())


def cross_user(page: dict, resps: dict[str, dict]) -> tuple[str, dict[str, str]]:
    """Return (user prompt, label -> provider). **Provider names are hidden**, or the
    models' brand priors leak into the verdict — and these are exactly the pages where
    only the content should count."""
    gt = page.get("gt") or {}
    order = _cross_order(page["pid"], sorted(resps))
    label_of = {}
    parts = ["URL: %s" % page.get("url", "")]
    if gt.get("serp_title"):
        parts.append("Title, from a neutral search engine's index: %s" % gt["serp_title"])
    if gt.get("serp_snippet"):
        parts.append("Search-engine snippet: %s" % gt["serp_snippet"])
    if page.get("antibot_subclass"):
        parts.append("This page is behind a %s." % page["antibot_subclass"])
    for i, prov in enumerate(order):
        label = chr(ord("A") + i)
        label_of[label] = prov
        r = resps[prov]
        if r.get("status") == "error":
            body = "(the service reported an error: %s)" % (r.get("failure_reason") or "error")
        else:
            body = (r.get("text") or "")[:CROSS_TEXT_BUDGET] or "(empty)"
        parts.append("\n--- RETURN %s (%d chars total) ---\n%s"
                     % (label, len(r.get("text") or ""), body))
    return "\n".join(parts), label_of


def cross_cache_key(page: dict, resps: dict[str, dict], model: str) -> str:
    h = hashlib.sha256()
    h.update((page.get("pid", "") + "|cross|" + RUBRIC_VERSION + "|" + model).encode())
    for prov in sorted(resps):
        h.update(prov.encode())
        h.update((resps[prov].get("text") or "")[:CROSS_TEXT_BUDGET].encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def panel_cross(page: dict, resps: dict[str, dict], panel: list[str], *,
                cache: PanelCache | None = None, max_tokens: int = 1200) -> dict:
    """Judge every provider for one page in a single call. Returns {provider: verdict}.

    This is both **more accurate and cheaper** than judging each provider alone: more
    accurate because the side-by-side comparison exposes the wrong answers as soon as one
    provider genuinely got the page, and cheaper because N providers cost one call
    instead of N.
    """
    from .llm import call_llm_json
    user, label_of = cross_user(page, resps)
    per_model: dict[str, dict] = {}
    for model in panel:
        key = cross_cache_key(page, resps, model)
        hit = cache.get(key) if cache else None
        if hit is not None:
            per_model[model] = hit
            continue
        try:
            raw = call_llm_json(CROSS_SYSTEM, user, model=model,
                                max_tokens=max_tokens, temperature=0.0)
            got = raw.get("verdicts") or {}
            v = {label_of[k]: {"verdict": (val.get("verdict") or "").strip(),
                               "dishonest": bool(val.get("dishonest")),
                               "why": (val.get("why") or "")[:160]}
                 for k, val in got.items() if k in label_of}
        except Exception as e:                   # noqa: BLE001
            v = {p: {"verdict": "error", "dishonest": False,
                     "why": "%s: %s" % (type(e).__name__, str(e)[:100])} for p in resps}
        per_model[model] = v
        if cache and any(x["verdict"] != "error" for x in v.values()):
            cache.put(key, v)

    out = {}
    for prov in resps:
        votes = {m: per_model[m].get(prov, {"verdict": "error"}) for m in per_model}
        out[prov] = aggregate_votes(votes)
        out[prov]["mode"] = "cross"
    return out


def score_page_cross(page: dict, resps: dict[str, dict], panel: list[str] | None = None,
                     *, cache: PanelCache | None = None,
                     gold: "GoldStore | None" = None) -> dict[str, dict]:
    """Judge a whole page at once — for pages with **no reference answer**.

    The mechanical layer still runs first and its hard vetoes remain terminal; whatever
    is left undecided goes to the cross-provider panel, one call for the whole page.
    """
    out, pending = {}, {}
    for prov, resp in resps.items():
        ch = run_checks(page, resp)
        mech = mechanical_verdict(page, resp, ch)
        rec = {"pid": page["pid"], "provider": prov, "type": page["type"],
               "antibot_subclass": page.get("antibot_subclass"),
               "strength": (page.get("gt") or {}).get("strength"),
               "anchor_source": (page.get("gt") or {}).get("anchor_source"),
               "checks": ch, "mechanical": mech, "verdict": mech["verdict"],
               "dishonest": mech["dishonest"], "suspicious_bypass": mech["suspicious_bypass"],
               "reason": mech["reason"], "panel": None, "panel_split": False,
               "latency_ms": resp.get("latency_ms"), "len_norm": ch["len_norm"],
               "failure_reason": resp.get("failure_reason"), "fault": resp.get("fault"),
               "run_seq": resp.get("run_seq", 0)}
        if _apply_gold(rec, page, resp, gold, prov):
            out[prov] = rec
            continue                             # human verdict wins; skip cross-judging
        clean_pass = mech["verdict"] == _PASS and not mech["needs_panel"]
        if not (clean_pass or mech.get("final")) and panel:
            pending[prov] = resp
        elif rec["verdict"] is None:
            rec["reason"] = mech["reason"] + "|unjudged"
        out[prov] = rec

    if pending and panel:
        cross = panel_cross(page, pending, panel, cache=cache)
        for prov, pv in cross.items():
            rec = out[prov]
            rec["panel"] = pv
            rec["panel_split"] = pv["panel_split"]
            if pv["verdict"] is not None:
                rec["verdict"] = pv["verdict"]
                rec["dishonest"] = rec["dishonest"] or pv["dishonest"]
                rec["reason"] = "panel_cross"
            elif rec["verdict"] is None:
                rec["reason"] = rec["mechanical"]["reason"] + "|unjudged"
    return out
