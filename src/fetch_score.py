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
      No anchors at all                 return empty. The panel then rules on the
                                        fetched content alone, which is the weakest
                                        evidence in the set — those cells are counted
                                        and reported as such.
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


# Structural labels a provider may report about the page it just fetched.
#
# **"Index Page" is not on its own evidence of anything.** Plenty of URLs in a page set
# genuinely are listings — a shop category, a forum board, a reviews index — and labelling
# one as such is correct and honest. Used as a standalone trigger it flags those as
# dishonest while the extraction is perfectly good. It is meaningful only as
# corroboration, once something else has established the content is not the page.
_CORROBORATES = ("Index Page", "Search Results", "Error Page")

# "No Main Content" is self-contained: the provider is saying the page has no body, so
# returning a small body as that page's content presents a shell as the page.
_SHELL = "No Main Content"

# A long body outweighs the label. Providers mislabel substantial articles as having no
# main content, and treating the label as final there would mark a real retrieval
# dishonest. Above this many characters the body wins.
SELF_REPORT_OVERRIDE_LEN = 2000


def self_reported_structure(resp: dict) -> str | None:
    """The provider's own structural label, when it ships one. Most do not."""
    return ((resp.get("raw_meta") or {}).get("page_structure")) or None


def self_report_says_not_the_page(page: dict, resp: dict, verdict: str | None) -> bool:
    """Does the provider's own label say the return is not this page?

    **Evidence, not a score.** It never sets a verdict — a provider grading its own fetch
    is not a measurement — and it only ever adds the honesty flag, so shipping the field
    can never improve a number.

    Two paths, because the labels establish different things on their own:

      "No Main Content" + a small body   self-contained: the provider says the page has no
                                         body, and returned one anyway.
      "Index Page" and friends           corroboration only, and only on a cell already
                                         judged lost. A listing URL is legitimately a
                                         listing, so the label alone proves nothing.
    """
    if page.get("expect", "content") != "content":
        return False
    label = self_reported_structure(resp)
    text = resp.get("text") or ""
    if not label or not text.strip():
        return False                      # nothing was presented as content
    if label.startswith(_SHELL):
        return len(text) <= SELF_REPORT_OVERRIDE_LEN
    if any(label.startswith(x) for x in _CORROBORATES):
        return verdict == _LOST
    return False


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


def _apply_self_report(out: dict, page: dict, resp: dict) -> None:
    """Fold the provider's own label into the honesty flag, once the verdict is settled.

    Applied last, because the corroborating labels only mean something against a decided
    verdict — and never subtractively: a flag the returned text already earned stands.
    """
    if self_report_says_not_the_page(page, resp, out.get("verdict")):
        out["dishonest"] = True


def score_one(page: dict, resp: dict, panel: list[str] | None = None, *,
              cache: PanelCache | None = None) -> dict:
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
    clean_pass = mech["verdict"] == _PASS and not mech["needs_panel"]
    if clean_pass or mech.get("final") or not panel:
        if out["verdict"] is None:
            # No panel and no mechanical basis: leave it genuinely unjudged.
            # **It is not scored as 0.**
            out["reason"] = mech["reason"] + "|unjudged"
        _apply_self_report(out, page, resp)
        return out
    pv = panel_verdict(page, resp, ch, panel, cache=cache)
    out["panel"] = pv
    out["panel_split"] = pv["panel_split"]
    if pv["verdict"] is not None:
        out["verdict"] = pv["verdict"]
        out["dishonest"] = out["dishonest"] or pv["dishonest"]
        out["reason"] = "panel"
    _apply_self_report(out, page, resp)
    return out


assert set(_TYPE_RUBRIC) == {"content", "render", "antibot"}
assert set(_RUBRIC_FOR.values()) <= set(_TYPE_RUBRIC)
