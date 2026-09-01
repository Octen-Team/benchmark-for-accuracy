"""The fetch-capability report.

Fetch capability only: one headline metric, the **fetch success rate**, on a single unit
shared by every page. Because of that a total is meaningful, and the five page types
become slice axes rather than five separate headline metrics. Text purity, structural
fidelity and truncation completeness are parsing quality and were removed from the code.

**A missing bucket prints as unlabelled; it is never silently zeroed.**
"""
from __future__ import annotations

import argparse
import json
import statistics as stat
from collections import Counter, defaultdict
from pathlib import Path

from src.fetch_spec import TH

WEIGHT = {"pass": 1.0, "partial": 0.5, "lost": 0.0}
UNLABELLED = "unlabelled"
TYPE_LABEL = {"baseline": "static docs", "render": "render / SPA",
              "docfmt": "document files", "antibot": "anti-bot", "reliability": "robustness"}
SUBCLASS_LABEL = {"waf": "WAF", "login_wall": "login wall", "paywall": "paywall"}
STRENGTH_LABEL = {"soft": "soft", "medium": "medium", "hard": "hard",
                  "unknown": "not measured"}


def load_jsonl(p: Path) -> list[dict]:
    # **Split on "\n" only — never splitlines().** The latter also splits on U+2028,
    # U+2029 and U+0085, which occur legitimately in page text and which json.dumps does
    # not escape. One record gets torn in half and surfaces as "Unterminated string".
    return [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]


def weighted(verdicts) -> dict:
    """Fetch success rate = (pass x1.0 + partial x0.5) / N.
    **Unjudged cells are counted separately: not in the denominator, and not zero.**"""
    judged = [v for v in verdicts if v in WEIGHT]
    unjudged = len(list(verdicts)) - len(judged) if not isinstance(verdicts, list) \
        else len(verdicts) - len(judged)
    if not judged:
        return {"weighted": None, "n": 0, "unjudged": unjudged,
                "pass": 0, "partial": 0, "lost": 0}
    c = Counter(judged)
    return {"weighted": sum(WEIGHT[v] for v in judged) / len(judged),
            "n": len(judged), "unjudged": unjudged,
            "pass": c["pass"], "partial": c["partial"], "lost": c["lost"]}


def _by(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return out


def _slice(rows, providers, keyfn, gapped: set | None = None):
    """Each bucket also records `_gap_share`: how many of its verdicts fall on pages
    where no ground truth could be built.

    **Reporting a bucket without its confidence misleads.** On the hardest pages our own
    browser cannot obtain a reference either, so the panel judges from the fetched
    content alone — and it is more lenient in that situation. The result is an inversion
    where the harder tier scores higher than the easier one. That is an artefact of
    measurement confidence, not a capability difference.
    """
    gapped = gapped or set()
    buckets: dict = {}
    for bucket, rs in _by([r for r in rows if keyfn(r) is not None], keyfn).items():
        b = {p: weighted([x["verdict"] for x in rs if x["provider"] == p])
             for p in providers}
        # **A human-verified cell is not low-confidence.** These start as the weakest
        # evidence in the set and become the strongest once reviewed. Without excluding
        # them the warning never clears and the review work counts for nothing.
        weak = [r for r in rs if r["pid"] in gapped and r.get("reason") != "human_gold"]
        b["_gap_share"] = (len(weak) / len(rs)) if rs else 0.0
        b["_human_verified"] = sum(1 for r in rs if r.get("reason") == "human_gold")
        b["_n_pages"] = len({r["pid"] for r in rs})
        buckets[bucket] = b
    return buckets


# The direction of each column. **A column with no direction is not ranked** — ranking
# it anyway turns "for reference only" into "bigger is better".
#   True = higher is better   False = lower is better   None = not ranked, with a reason
DIAG_DIRECTION = {
    # Latency is **not ranked**: any provider that was paced carries our deliberate
    # waiting inside its timings. The report already warns that latency is not comparable
    # across providers; ranking it anyway would contradict that in the same document.
    # Comparing speed requires a separate, unpaced round.
    "latency_p50": None, "latency_p90": None, "latency_n": True,
    "slow_losses": False, "dishonest": False, "wrong_page": False,
    "mojibake": False, "suspicious_bypass": False,
    "len_norm_median": None,   # providers strip boilerplate differently; longer != better
    "panel_split": None,       # measures how hard the cells were, not provider quality
}
NO_RANK_WHY = {
    "latency_p50": "paced providers carry deliberate waiting; not comparable across providers",
    "latency_p90": "as above",
    "len_norm_median": "providers strip boilerplate differently; longer is not better",
    "panel_split": "measures how hard these cells were to judge, not provider quality",
}


def rank_of(values: dict, higher_is_better: bool) -> dict:
    """{key: rank}. **Ties share a rank.** None values do not take part."""
    got = [(k, v) for k, v in values.items() if v is not None]
    if not got:
        return {}
    got.sort(key=lambda kv: -kv[1] if higher_is_better else kv[1])
    out, last, rank = {}, object(), 0
    for i, (k, v) in enumerate(got, 1):
        if v != last:
            rank, last = i, v
        out[k] = rank
    return out


def _rk(n) -> str:
    return "" if not n else " <sup>%d</sup>" % n


def _rk_md(n) -> str:
    return "" if not n else " ⁽%d⁾" % n


# Diagnostic column definitions, **shared by the markdown and HTML renderers** — two
# copies would drift apart.
DIAG_COLS_SPEC = [
    ("P50", "latency_p50", lambda d: "%.0f ms" % d["latency_p50"] if d["latency_p50"] else None),
    ("P90", "latency_p90", lambda d: "%.0f ms" % d["latency_p90"] if d["latency_p90"] else None),
    ("timed calls", "latency_n", lambda d: str(d["latency_n"])),
    ("median length", "len_norm_median",
     lambda d: _fmt(d["len_norm_median"], pct=False) if d["len_norm_median"] else None),
    ("slow losses", "slow_losses", lambda d: str(d["slow_losses"])),
    ("dishonest", "dishonest", lambda d: str(d["dishonest"])),
    ("wrong page", "wrong_page", lambda d: str(d["wrong_page"])),
    ("mojibake", "mojibake", lambda d: str(d["mojibake"])),
    ("suspected bypass", "suspicious_bypass", lambda d: str(d["suspicious_bypass"])),
    ("panel split", "panel_split", lambda d: str(d["panel_split"])),
]


def _cache_states(providers) -> dict:
    """Each provider's live-fetch state. **Read from the adapters, never transcribed** —
    a hand-written table drifts away from the code."""
    try:
        from src.fetch_backends import FETCHERS
    except Exception:                            # noqa: BLE001
        return {}
    return {p: FETCHERS[p].cache_pinned for p in providers if p in FETCHERS}


def _pct(xs, q: int):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((q / 100) * (len(s) - 1)))))
    return s[k]


VERDICT_ORDER = {"lost": 0, "partial": 1, "pass": 2}
ORDER_NAME = {0: "lost", 1: "partial", 2: "pass"}


def collapse_rounds(verdicts: list[dict], pick: str = "median") -> list[dict]:
    """Collapse the several rounds of one cell into a single row.

    **The headline takes the median, not the first round.** On defended pages the result
    varies from call to call: the same URL can return the article one moment and a
    challenge screen the next. The first round is simply an arbitrary round, and using it
    as the headline writes one roll of the dice into the report.

    `pick` also accepts `best` / `worst`, which produce the envelope around the headline.
    With the envelope reported, a reader can tell whether a few points between two
    providers is a real difference or round-to-round noise.
    For an even number of rounds the median takes the **lower** middle: better to
    understate than to manufacture a high score.
    """
    assert pick in ("median", "best", "worst"), pick
    groups: dict = {}
    for v in verdicts:
        groups.setdefault((v["pid"], v["provider"]), []).append(v)
    out = []
    for rows in groups.values():
        rows = sorted(rows, key=lambda r: r.get("run_seq", 0))
        judged = [r for r in rows if r.get("verdict") in VERDICT_ORDER]
        if not judged:
            out.append({**rows[0], "rounds": len(rows), "round_verdicts": []})
            continue
        scores = sorted(VERDICT_ORDER[r["verdict"]] for r in judged)
        take = {"median": scores[(len(scores) - 1) // 2],
                "best": scores[-1], "worst": scores[0]}[pick]
        name = ORDER_NAME[take]
        # The representative row is the **first round with that verdict**, so the
        # diagnostic columns come from the same round as the verdict itself
        rep = next(r for r in judged if r["verdict"] == name)
        out.append({**rep, "rounds": len(rows),
                    "round_verdicts": [r.get("verdict") for r in rows],
                    "unstable": len({r["verdict"] for r in judged}) > 1})
    return sorted(out, key=lambda r: (r["pid"], r["provider"]))


def aggregate(verdicts: list[dict], pages: list[dict],
              run_meta: dict | None = None) -> dict:
    """Multiple rounds collapse per cell to their **median** (see collapse_rounds)."""
    pmap = {p["pid"]: p for p in pages}
    base = collapse_rounds(verdicts)
    providers = sorted({v["provider"] for v in base})

    # ── Headline: a single fetch success rate, comparable across page types ───
    overall = {p: weighted([r["verdict"] for r in base if r["provider"] == p])
               for p in providers}
    ranking = sorted([(p, overall[p]["weighted"]) for p in providers
                      if overall[p]["weighted"] is not None], key=lambda x: -x[1])

    # ── Slices ───────────────────────────────────────────────────────────────
    gapped = {p["pid"] for p in pages if (p.get("gt") or {}).get("gt_gap")}
    slices = {
        "type": _slice(base, providers, lambda r: r["type"], gapped),
        "antibot_subclass": _slice(base, providers,
                                   lambda r: r.get("antibot_subclass"), gapped),
        "strength": _slice([r for r in base if r["type"] == "antibot"], providers,
                           lambda r: r.get("strength") or "unknown", gapped),
        "doc_type": _slice(base, providers, lambda r: pmap[r["pid"]].get("doc_type"), gapped),
    }
    probe_b: dict = {}
    for probe in sorted({x for p in pages for x in (p.get("probes") or [])}):
        pids = {p["pid"] for p in pages if probe in (p.get("probes") or [])}
        rs = [r for r in base if r["pid"] in pids]
        probe_b[probe] = {p: weighted([r["verdict"] for r in rs if r["provider"] == p])
                          for p in providers}
        _weak = [r for r in rs if r["pid"] in gapped and r.get("reason") != "human_gold"]
        probe_b[probe]["_gap_share"] = (len(_weak) / len(rs)) if rs else 0.0
        probe_b[probe]["_human_verified"] = sum(1 for r in rs
                                                if r.get("reason") == "human_gold")
        probe_b[probe]["_n_pages"] = len(pids)
    slices["probes"] = probe_b

    # ── Secondary metric, de-duplicated by domain (average within a domain first) ──
    host_dedup = {}
    for prov in providers:
        rs = [r for r in base if r["provider"] == prov and r["verdict"] in WEIGHT]
        per_host = _by(rs, lambda r: pmap[r["pid"]]["host"])
        vals = [stat.mean(WEIGHT[r["verdict"]] for r in g) for g in per_host.values()]
        host_dedup[prov] = {"weighted": stat.mean(vals) if vals else None,
                            "hosts": len(vals)}

    # ── Diagnostic columns (never part of the score) ─────────────────────────
    diag = {}
    for prov in providers:
        rs = [v for v in base if v["provider"] == prov]
        usable = [r["latency_ms"] for r in rs
                  if r["verdict"] in ("pass", "partial") and r.get("latency_ms")]
        diag[prov] = {
            # **Latency counts only calls that returned content**, otherwise it measures
            # how quickly things time out
            "latency_p50": _pct(usable, 50), "latency_p90": _pct(usable, 90),
            "latency_n": len(usable),
            "len_norm_median": _pct([r["len_norm"] for r in rs if r["len_norm"]], 50),
            "slow_losses": sum(1 for r in rs if r["verdict"] == "lost"
                               and (r.get("latency_ms") or 0) >= TH["slow_loss_ms"]),
            "dishonest": sum(1 for r in rs if r.get("dishonest")),
            "wrong_page": sum(1 for r in rs if r.get("reason") == "wrong_page"),
            "mojibake": sum(1 for r in rs if r.get("reason") == "mojibake"),
            "suspicious_bypass": sum(1 for r in rs if r.get("suspicious_bypass")),
            "unjudged": sum(1 for r in rs if r["verdict"] not in WEIGHT),
            "panel_split": sum(1 for r in rs if r.get("panel_split")),
        }

    # ── Failure attribution. When the metric only measures success, "why it was not
    #    retrieved" is the report's second subject. ────────────────────────
    failures = {}
    for prov in providers:
        rs = [v for v in base if v["provider"] == prov and v.get("failure_reason")]
        failures[prov] = dict(Counter("%s/%s" % (r["failure_reason"], r.get("fault") or "?")
                                      for r in rs))
    harness = {p: sum(n for k, n in f.items() if k.endswith("/harness"))
               for p, f in failures.items()}

    gt_gaps = [{"pid": p["pid"], "host": p["host"],
                "why": "walled" if (p.get("gt") or {}).get("gt_wall_hit") else "no_vocab"}
               for p in pages if (p.get("gt") or {}).get("gt_gap")]
    ab = [p for p in pages if p["type"] == "antibot"]

    return {
        "scope": "Fetch capability only: the headline metric is the fetch success rate. "
                 "Parsing quality is not scored.",
        "type_counts": {t: n for t, n in Counter(p["type"] for p in pages).items()},
        "providers": providers,
        # Every table uses the same row order, by overall rank; per-table ordering would
        # stop the reader matching rows across tables
        "providers_ranked": [p for p, _ in ranking]
                            + [p for p in providers if overall[p]["weighted"] is None],
        "overall": overall, "ranking": ranking,
        "slices": slices, "host_dedup": host_dedup, "diagnostics": diag,
        "failures": failures, "harness_faults": harness,
        "meta": {
            "n_pages": len(pages), "n_cells": len(base),
            "gt_gaps": gt_gaps,
            "strength_counts": dict(Counter((p.get("gt") or {}).get("strength")
                                            for p in ab)),
            "unjudged_total": sum(d["unjudged"] for d in diag.values()),
            "verdicts_on_gt_gap_pages": sum(
                1 for v in base if (pmap[v["pid"]].get("gt") or {}).get("gt_gap")
                and v.get("reason") != "human_gold"),
            "human_verified": sum(1 for v in base if v.get("reason") == "human_gold"),
            "cache_pinned": _cache_states(providers),
            # Rounds: how many, how many cells disagreed across them, and the best/worst
            # envelope of the headline. With the envelope reported, a reader can tell
            # whether a few points of difference is signal or round-to-round noise.
            # Which providers were paced, and by how much. The report must state this
            # from the actual run parameters: hard-coding one provider's pacing into the
            # template makes the sentence false for every other set of providers.
            "pace": {k: v for k, v in ((run_meta or {}).get("pace") or {}).items() if v},
            "rounds": max((v.get("rounds", 1) for v in base), default=1),
            "unstable_cells": sum(1 for v in base if v.get("unstable")),
            "envelope": {
                p: {k: weighted([r["verdict"] for r in collapse_rounds(verdicts, k)
                                 if r["provider"] == p])["weighted"]
                    for k in ("worst", "median", "best")}
                for p in providers},
        },
    }


# ── Metric definitions, kept alongside the report so readers need nothing else ──

MAIN_COLS = [
    ("fetch success rate",
     "(pass x1.0 + partial x0.5) / n. One unit for every page, so it is comparable "
     "across page types and across providers"),
    ("pass", "the substantive content of this page came back: non-empty, genuinely this "
             "URL, not mojibake, not a challenge screen passed off as content; where a "
             "reference vocabulary exists, at least 30% of it is present"),
    ("partial", "part of it came back: the free portion in front of a paywall, a "
                "fragment, a dynamic page that only half rendered"),
    ("lost", "blocked / empty / errored / a different page returned / mojibake"),
    ("n", "cells that could be judged; also the denominator of the success rate"),
    ("unjudged", "we could not reach a verdict. **Not scored as zero** — excluded from "
                 "both the numerator and the denominator, and reported on its own"),
    ("de-duplicated by domain",
     "pages from the same site are averaged within that domain before entering the "
     "total, so a site contributing many pages does not dominate"),
]

DIAG_COLS = [
    ("dishonest",
     "**how often a challenge screen, an error page, or a different page came back as "
     "content.** An honest failure tells the caller to retry; dirty data does not. The "
     "single number most worth looking at on its own"),
    ("P50 / P90",
     "median and 90th-percentile time for one call, **counting only calls that returned "
     "content**. **Not comparable across providers** whenever any of them was paced"),
    ("wrong page / mojibake",
     "two hard failures: not one distinctive keyword matched / the text is mojibake or "
     "binary. Judged lost outright, with no panel review"),
    ("suspected bypass",
     "how often an apparently complete body came back from a paywalled page. "
     "**Flagged, never rewarded** — scoring content from behind the wall as better would "
     "reward circumventing it"),
    ("slow losses", "failed and took 10 seconds or more: slow and empty-handed"),
    ("median length",
     "median length of the retrieved content. Comparable across rounds for one provider, "
     "**for reference only across providers**, which strip boilerplate differently"),
    ("panel split",
     "cells where all three panel models disagreed. A high count means those cells were "
     "genuinely hard to judge, not that a provider is bad"),
]

FAULTS_DESC = [
    ("provider", "the provider's side: blocked, timed out, rate-limited, returned an "
                 "error — a real capability difference"),
    ("page", "the page's side: this page has no extractable content to begin with"),
    ("harness", "**our own fault**: an exhausted account, our size cap, our parser "
                "crashing. Reported in its own column, never charged to the provider"),
]


def _traps(agg: dict) -> list[tuple[str, str]]:
    m = agg["meta"]
    low = [k for sl in agg["slices"].values() for k, b in sl.items()
           if isinstance(b, dict) and b.get("_gap_share", 0) >= LOW_CONFIDENCE]
    out = []
    if low:
        out.append(("A bucket marked low-confidence: a high score is not strength",
                    "On those pages our own browser cannot obtain reference content "
                    "either, so the panel judges from the fetched result alone — and it "
                    "is more lenient in that situation. That produces the inversion "
                    "where a harder tier scores higher. **It is an artefact of "
                    "measurement confidence, not a capability difference.**"))
    pace = m.get("pace") or {}
    if pace:
        out.append(("Latency is not comparable across providers",
                    "This round paced %s, because they reject requests at a faster "
                    "cadence. Their timings therefore include waiting we imposed. "
                    "Comparing speed needs a separate, unpaced round."
                    % ", ".join("%s at %gs/request" % (k, v)
                                for k, v in sorted(pace.items()))))
    out.append(("A high success rate with a high dishonest count is worse than a "
                "slightly lower success rate",
                "The success rate only asks whether the content came back. A provider "
                "with a decent rate but a markedly higher dishonest count tends, when it "
                "fails, to return **something that looks like content** — and nothing "
                "downstream can tell. Read the two numbers together before integrating."))
    if m["verdicts_on_gt_gap_pages"]:
        out.append(("Verdicts on ground-truth gap pages rest on weaker evidence",
                    "%d pages could not be fetched by us either, so no reference answer "
                    "exists for them; %d verdicts fall on those pages and depend entirely "
                    "on the panel's judgement. They are named in the methodology notes; "
                    "review them individually if a conclusion is sensitive to them."
                    % (len(m["gt_gaps"]), m["verdicts_on_gt_gap_pages"])))
    return out


def render_glossary_md(agg: dict) -> list[str]:
    L = ["", "## Metric definitions", "",
         "**Scope**: this evaluation answers one question — was the page retrieved. How "
         "cleanly it was parsed and how completely it was structured are parsing "
         "quality, and are out of scope.", "",
         "### Main table columns", "", "| Column | Definition |", "|---|---|"]
    L += ["| %s | %s |" % kv for kv in MAIN_COLS]
    L += ["", "### How a verdict is reached", "",
          "Two layers. **The mechanical layer runs first**: content hit rate, whether "
          "this is the content of this URL, whether the text is mojibake, and whether "
          "content that only appears after JavaScript runs is present. The reference "
          "vocabulary comes from rendering every page ourselves, with navigation and "
          "footer terms removed so the hit rate measures the body. **Anything it cannot "
          "settle goes to the panel**: three models from three different vendors judge "
          "blind and the majority wins. None of them sees the mechanical verdict, the "
          "other providers' attribution, or each other's rulings, and a three-way split "
          "is not forced. Conclusions the mechanical layer is **certain** of — wrong "
          "page, mojibake, transport failure — are never sent for review.",
          "", "### Diagnostic columns (not part of the score)", "",
          "| Column | Definition |", "|---|---|"]
    L += ["| %s | %s |" % kv for kv in DIAG_COLS]
    L += ["", "### Who a failure is attributed to", "", "| Fault | Meaning |", "|---|---|"]
    L += ["| `%s` | %s |" % kv for kv in FAULTS_DESC]
    L += ["", "### Easy things to misread", ""]
    for i, (h, body) in enumerate(_traps(agg), 1):
        L.append("%d. **%s** — %s" % (i, h, body))
    return L


def _fmt(x, pct=True):
    if x is None:
        return UNLABELLED
    return "%.0f%%" % (x * 100) if pct else "%.0f" % x


LOW_CONFIDENCE = 0.5      # half or more of a bucket on gap pages -> mark low confidence


def _slice_table(title: str, buckets: dict, providers: list, labels: dict | None = None):
    if not buckets:
        return []
    keys = sorted(buckets)
    heads = []
    for k in keys:
        mark = " ⚠" if buckets[k]["_gap_share"] >= LOW_CONFIDENCE else ""
        heads.append("%s(%d)%s" % ((labels or {}).get(k, k), buckets[k]["_n_pages"], mark))
    # Rank **within each column** (ties share a rank). Rows are ordered by the overall
    # score, so the rank has to be printed in the cell: ordering each table by its own
    # column would give the five tables five different row orders.
    rk = {k: rank_of({p: buckets[k][p]["weighted"] for p in providers}, True) for k in keys}
    L = ["", "### %s" % title, "",
         "| provider | " + " | ".join(heads) + " |",
         "|---" * (len(keys) + 1) + "|"]
    for p in providers:
        L.append("| %s | %s |" % (p, " | ".join(
            _fmt(buckets[k][p]["weighted"]) + _rk_md(rk[k].get(p)) for k in keys)))
    L += ["", "*The superscript is the rank within that column; ties share a rank.*"]
    low = [((labels or {}).get(k, k), buckets[k]["_gap_share"]) for k in keys
           if buckets[k]["_gap_share"] >= LOW_CONFIDENCE]
    if low:
        L += ["", "> ⚠ **Low confidence**: %s. Most verdicts in these buckets fall on "
              "ground-truth gap pages, where our own browser cannot obtain a reference "
              "either, so the panel judges from the fetched content alone — and it is "
              "more lenient in that situation. **Do not read a high score in these "
              "cells as greater capability.**"
              % ", ".join("%s %.0f%%" % (n, v * 100) for n, v in low)]
    return L


def render_markdown(agg: dict) -> str:
    m = agg["meta"]
    L = ["# Fetch provider capability evaluation", "",
         "**%s**" % agg["scope"], "",
         "%d pages · %d judged cells · %d provider%s"
         % (m["n_pages"], m["n_cells"], len(agg["providers"]),
            "" if len(agg["providers"]) == 1 else "s"),
         "", "## Headline: fetch success rate", "",
         "A pass counts 1.0 and a partial 0.5. `n` is the number of cells that **could "
         "be judged** — unjudged cells enter neither the denominator nor the score.",
         "", "| # | provider | success rate | pass | partial | lost | n | unjudged | "
         "by domain |",
         "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for i, (prov, w) in enumerate(agg["ranking"], 1):
        o = agg["overall"][prov]
        hd = agg["host_dedup"][prov]["weighted"]
        L.append("| %d | %s | **%s** | %d | %d | %d | %d | %d | %s |"
                 % (i, prov, _fmt(w), o["pass"], o["partial"], o["lost"], o["n"],
                    o["unjudged"], _fmt(hd)))
    for prov in agg["providers"]:
        if agg["overall"][prov]["weighted"] is None:
            L.append("| — | %s | %s | 0 | 0 | 0 | 0 | %d | %s |"
                     % (prov, UNLABELLED, agg["overall"][prov]["unjudged"], UNLABELLED))

    L += ["", "## Slices"]
    pr = agg["providers_ranked"]
    L += _slice_table("By page type", agg["slices"]["type"], pr, TYPE_LABEL)
    L += _slice_table("Anti-bot pages by wall type", agg["slices"]["antibot_subclass"],
                      pr, SUBCLASS_LABEL)
    L += _slice_table("Anti-bot pages by protection strength", agg["slices"]["strength"],
                      pr, STRENGTH_LABEL)
    L += _slice_table("Document files by format", agg["slices"]["doc_type"], pr)
    L += _slice_table("Robustness probes", agg["slices"]["probes"], pr)

    fail_n = {p: sum(agg["failures"][p].values()) for p in agg["providers"]}
    fr = rank_of(fail_n, False)                  # fewer failures is better
    hr = rank_of(agg["harness_faults"], False)   # fewer of our own faults is better
    L += ["", "## Why it was not retrieved", "",
          "When the metric only measures success, failure attribution is the report's "
          "second subject. `harness` counts our own faults and is listed separately. "
          "**Lower is better in both columns**; the superscript is the rank.", "",
          "| provider | failures | harness | detail |", "|---|--:|--:|---|"]
    for prov in agg["providers_ranked"]:
        f = agg["failures"][prov]
        L.append("| %s | %d%s | %d%s | %s |"
                 % (prov, fail_n[prov], _rk_md(fr.get(prov)),
                    agg["harness_faults"][prov], _rk_md(hr.get(prov)),
                    ", ".join("%s×%d" % kv for kv in sorted(f.items())) or "—"))

    ranks = {}
    for _, field, _f in DIAG_COLS_SPEC:
        d = DIAG_DIRECTION.get(field)
        ranks[field] = ({} if d is None
                        else rank_of({p: agg["diagnostics"][p][field]
                                      for p in agg["providers"]}, d))
    arrow = {True: " ↑", False: " ↓", None: " —"}
    L += ["", "## Diagnostic columns (not part of the score)", "",
          "The superscript is the rank within the column. **↑ higher is better · "
          "↓ lower is better · — not ranked** (reasons below the table).",
          "",
          "| provider | " + " | ".join(
              n + arrow[DIAG_DIRECTION.get(f)] for n, f, _ in DIAG_COLS_SPEC) + " |",
          "|---" * (len(DIAG_COLS_SPEC) + 1) + "|"]
    for prov in agg["providers_ranked"]:
        d = agg["diagnostics"][prov]
        cells = []
        for _, field, fmt in DIAG_COLS_SPEC:
            val = fmt(d)
            cells.append((val or UNLABELLED) + _rk_md(ranks[field].get(prov)))
        L.append("| %s | %s |" % (prov, " | ".join(cells)))
    L += ["", "**Columns that are not ranked**: " + "; ".join(
        "`%s` — %s" % (n, NO_RANK_WHY[f]) for n, f, _ in DIAG_COLS_SPEC
        if DIAG_DIRECTION.get(f) is None)
        + ". Ranking them would turn \"for reference only\" into \"bigger is better\"."]

    L += [""]
    L += render_glossary_md(agg)
    L += ["", "## Methodology notes", "",
          "- **Fetch capability only.** The headline metric asks whether the page was "
          "retrieved. Text purity, structural fidelity and truncation completeness are "
          "parsing quality and were removed from the code.",
          "- On anti-bot pages, passing is defined per wall type: WAF = the body was "
          "retrieved; login wall = the pre-wall content was retrieved **and identified "
          "as a wall**; paywall = the free portion was retrieved. **Content from behind "
          "the wall earns nothing** and is flagged `suspicious_bypass`.",
          "- Protection tiers, derived by comparing the two ground-truth channels: %s"
          % (m["strength_counts"] or UNLABELLED)]
    if m["gt_gaps"]:
        L.append("- **%d ground-truth gap pages.** The judge skips their vocabulary and "
                 "the panel rules on the fetched content itself: %s"
                 % (len(m["gt_gaps"]),
                    ", ".join("%s/%s(%s)" % (g["pid"], g["host"], g["why"])
                              for g in m["gt_gaps"])))
    L.append("- %d cells could not be judged; left genuinely unjudged, never scored as "
             "zero" % m["unjudged_total"])
    if m["verdicts_on_gt_gap_pages"]:
        L.append("- **%d verdicts fall on ground-truth gap pages.** The panel had no "
                 "reference render and ruled on the fetched content alone, so the "
                 "evidence is weaker than on pages with ground truth"
                 % m["verdicts_on_gt_gap_pages"])
    if m.get("rounds", 1) > 1:
        env = m.get("envelope") or {}
        L.append("- **%d rounds were run and the headline takes the per-cell median.** "
                 "%d of %d cells did not agree across rounds. Best and worst round: %s. "
                 "A difference smaller than that envelope is round-to-round noise, not a "
                 "capability gap."
                 % (m["rounds"], m["unstable_cells"], m["n_cells"],
                    "; ".join("%s %s-%s" % (p, _fmt(e["worst"]), _fmt(e["best"]))
                              for p, e in sorted(env.items()))))
    else:
        L.append("- **Only one round was run**, so the headline carries the full "
                 "round-to-round variance of a single sample. On defended pages that is "
                 "substantial; use `--repeat` before comparing providers.")
    L.append("- Latency counts only calls that returned content. Median length is "
             "comparable across rounds for one provider, and for reference only across "
             "providers")
    cp = agg["meta"].get("cache_pinned") or {}
    if cp:
        L.append("- **Each provider's live-fetch (cache-bypass) switch was set "
                 "explicitly**: %s. `no_knob` means the documented API has no such "
                 "parameter; `unpinned` means the provider is not wired up yet."
                 % " · ".join("%s=%s" % kv for kv in sorted(cp.items())))
    L.append("- **Content freshness is not measured.** The switch guarantees a live "
             "fetch happened; it does not guarantee that every provider saw the page at "
             "the same moment. A time-sensitive use case needs a separate round.")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════
# Standalone HTML page
# ══════════════════════════════════════════════════════════════════════════

_CSS = """
:root{--fx-bg:#fbfbfa;--fx-fg:#1a1a19;--fx-mut:#6b6b66;--fx-line:#e2e2dd;
      --fx-card:#fff;--fx-ok:#1f7a4d;--fx-mid:#b07d18;--fx-bad:#a33a2c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --fx-bg:#161614;--fx-fg:#ececea;--fx-mut:#9a9a94;--fx-line:#2e2e2a;
  --fx-card:#1e1e1b;--fx-ok:#5fbf8e;--fx-mid:#d9a63f;--fx-bad:#e0705e}}
:root[data-theme="dark"]{--fx-bg:#161614;--fx-fg:#ececea;--fx-mut:#9a9a94;
  --fx-line:#2e2e2a;--fx-card:#1e1e1b;--fx-ok:#5fbf8e;--fx-mid:#d9a63f;--fx-bad:#e0705e}
body{background:var(--fx-bg);color:var(--fx-fg);margin:0;padding:2rem 1.25rem 4rem;
  font:15px/1.6 ui-sans-serif,-apple-system,"Helvetica Neue",sans-serif}
.fx-wrap{max-width:1100px;margin:0 auto}
.fx-h1{font-size:1.8rem;font-weight:650;margin:0 0 .3rem;letter-spacing:-.01em}
.fx-sub{color:var(--fx-mut);margin:0 0 .4rem}
.fx-scope{display:inline-block;padding:.15rem .55rem;border-radius:6px;font-size:.82rem;
  border:1px solid var(--fx-line);color:var(--fx-mut);margin-bottom:2rem}
.fx-sec{font-size:1.05rem;font-weight:620;margin:2.4rem 0 .35rem}
.fx-note{color:var(--fx-mut);font-size:.87rem;margin:0 0 .85rem}
.fx-scroll{overflow-x:auto;border:1px solid var(--fx-line);border-radius:10px;
  background:var(--fx-card)}
table.fx-t{border-collapse:collapse;width:100%;font-size:.88rem;background:transparent}
table.fx-t th,table.fx-t td{border:0;border-bottom:1px solid var(--fx-line);
  padding:.5rem .7rem;text-align:right;vertical-align:middle;color:var(--fx-fg);
  background:transparent;font-weight:400;white-space:nowrap;
  font-variant-numeric:tabular-nums}
table.fx-t th{font-weight:600;color:var(--fx-mut);font-size:.8rem;letter-spacing:0}
table.fx-t th:first-child,table.fx-t td:first-child{text-align:left;font-weight:500}
table.fx-t tr:last-child td{border-bottom:0}
.fx-na{color:var(--fx-mut);font-style:italic}
.fx-ok{color:var(--fx-ok)}.fx-mid{color:var(--fx-mid)}.fx-bad{color:var(--fx-bad)}
.fx-big{font-size:1.02rem;font-weight:640}
.fx-n{color:var(--fx-mut);font-size:.8em;margin-left:.25rem}
table.fx-t sup{color:var(--fx-mut);font-size:.72em;font-weight:600;margin-left:.15rem}
.fx-card{border:1px solid var(--fx-line);border-radius:10px;background:var(--fx-card);
  padding:.9rem 1.1rem;margin:.5rem 0}
.fx-card ul,.fx-card ol{margin:.3rem 0 0;padding-left:1.15rem}
.fx-card li{margin:.35rem 0}
.fx-sub2{font-size:.92rem;font-weight:620;color:var(--fx-mut);margin:1.5rem 0 .45rem}
table.fx-kv td,table.fx-kv th{text-align:left;white-space:normal;vertical-align:top}
table.fx-kv td:first-child{white-space:nowrap;font-weight:560;width:1%}
table.fx-kv td:last-child{line-height:1.55;max-width:62ch}
ol.fx-traps li::marker{color:var(--fx-mut);font-weight:600}
"""


def _cls(x):
    if x is None:
        return "fx-na"
    return "fx-ok" if x >= 0.75 else ("fx-mid" if x >= 0.45 else "fx-bad")


def _sl_html(title, buckets, providers, labels=None):
    if not buckets:
        return ""
    keys = sorted(buckets)
    rk = {k: rank_of({p: buckets[k][p]["weighted"] for p in providers}, True) for k in keys}
    head = "".join("<th>%s<span class=\"fx-n\">%d</span>%s</th>"
                   % ((labels or {}).get(k, k), buckets[k]["_n_pages"],
                      " &#9888;" if buckets[k]["_gap_share"] >= LOW_CONFIDENCE else "")
                   for k in keys)
    rows = []
    for p in providers:
        tds = "".join('<td class="%s">%s%s</td>'
                      % (_cls(buckets[k][p]["weighted"]), _fmt(buckets[k][p]["weighted"]),
                         _rk(rk[k].get(p)))
                      for k in keys)
        rows.append("<tr><td>%s</td>%s</tr>" % (p, tds))
    low = [((labels or {}).get(k, k), buckets[k]["_gap_share"]) for k in keys
           if buckets[k]["_gap_share"] >= LOW_CONFIDENCE]
    warn = ('<p class="fx-note">&#9888; <strong>Low confidence</strong>: %s. Most '
            'verdicts in these buckets fall on ground-truth gap pages, where our own '
            'browser cannot obtain a reference either, so the panel rules on the '
            'fetched content alone &mdash; and it is more lenient in that situation. '
            '<strong>Do not read a high score here as greater capability.</strong></p>'
            % ", ".join("%s %.0f%%" % (n, v * 100) for n, v in low)) if low else ""
    return ('<div class="fx-sec">%s</div>%s<div class="fx-scroll"><table class="fx-t">'
            '<thead><tr><th>provider</th>%s</tr></thead><tbody>%s</tbody></table></div>'
            % (title, warn, head, "".join(rows)))


def _kv_table(pairs) -> str:
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, _md_bold(v)) for k, v in pairs)
    return ('<div class="fx-scroll"><table class="fx-t fx-kv"><thead><tr><th>Column</th>'
            '<th>Definition</th></tr></thead><tbody>%s</tbody></table></div>' % rows)


def _md_bold(t: str) -> str:
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def render_glossary_html(agg: dict) -> str:
    H = ['<div class="fx-sec">Metric definitions</div>',
         '<p class="fx-note"><strong>Scope</strong>: this evaluation answers one '
         'question &mdash; was the page retrieved. How cleanly it was parsed and how '
         'completely it was structured are parsing quality, and are out of scope.</p>',
         '<div class="fx-sub2">Main table columns</div>', _kv_table(MAIN_COLS),
         '<div class="fx-sub2">How a verdict is reached</div>',
         '<p class="fx-note">Two layers. <strong>The mechanical layer runs first</strong>: '
         'content hit rate, whether this is the content of this URL, whether the text is '
         'mojibake, and whether content that only appears after JavaScript runs is '
         'present. The reference vocabulary comes from rendering every page ourselves, '
         'with navigation and footer terms removed so the hit rate measures the body. '
         '<strong>Anything it cannot settle goes to the panel</strong>: three models from '
         'three different vendors judge blind and the majority wins. None sees the '
         'mechanical verdict, the provider attribution, or the other models&rsquo; '
         'rulings, and a three-way split is not forced. Conclusions the mechanical layer '
         'is <strong>certain</strong> of &mdash; wrong page, mojibake, transport failure '
         '&mdash; are never sent for review.</p>',
         '<div class="fx-sub2">Diagnostic columns (not part of the score)</div>',
         _kv_table(DIAG_COLS),
         '<div class="fx-sub2">Who a failure is attributed to</div>',
         _kv_table([("<code>%s</code>" % k, v) for k, v in FAULTS_DESC]),
         '<div class="fx-sub2">Easy things to misread</div>']
    items = "".join("<li><strong>%s</strong> &mdash; %s</li>" % (h, _md_bold(b))
                    for h, b in _traps(agg))
    H.append('<div class="fx-card"><ol class="fx-traps">%s</ol></div>' % items)
    return "".join(H)


def render_html(agg: dict, title: str = "Fetch provider capability") -> str:
    """A standalone HTML page. Data is embedded as a JavaScript literal, every class name
    carries an `fx-` prefix so nothing collides with a host stylesheet, and cell
    attributes are restated explicitly on every `th`/`td`."""
    m = agg["meta"]
    H = ["<title>%s</title>" % title, "<style>%s</style>" % _CSS,
         '<div class="fx-wrap">', '<h1 class="fx-h1">%s</h1>' % title,
         '<p class="fx-sub">%d pages &middot; %d judged cells &middot; %d provider%s</p>'
         % (m["n_pages"], m["n_cells"], len(agg["providers"]),
            "" if len(agg["providers"]) == 1 else "s"),
         '<div class="fx-scope">%s</div>' % agg["scope"]]

    H.append('<div class="fx-sec">Fetch success rate</div>')
    H.append('<p class="fx-note">A pass counts 1.0 and a partial 0.5. <code>n</code> is '
             'the number of cells that could be judged; unjudged cells enter neither the '
             'denominator nor the score.</p>')
    body = []
    for i, (prov, w) in enumerate(agg["ranking"], 1):
        o = agg["overall"][prov]
        hd = agg["host_dedup"][prov]["weighted"]
        body.append('<tr><td>%d</td><td>%s</td><td class="%s fx-big">%s</td>'
                    '<td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td>'
                    '<td class="%s">%s</td></tr>'
                    % (i, prov, _cls(w), _fmt(w), o["pass"], o["partial"], o["lost"],
                       o["n"], o["unjudged"], _cls(hd), _fmt(hd)))
    H.append('<div class="fx-scroll"><table class="fx-t"><thead><tr><th>#</th>'
             '<th>provider</th><th>success rate</th><th>pass</th><th>partial</th>'
             '<th>lost</th><th>n</th><th>unjudged</th><th>by domain</th></tr></thead>'
             '<tbody>%s</tbody>'
             '</table></div>' % "".join(body))

    pr = agg["providers_ranked"]
    H.append(_sl_html("By page type", agg["slices"]["type"], pr, TYPE_LABEL))
    H.append(_sl_html("Anti-bot pages by wall type", agg["slices"]["antibot_subclass"],
                      pr, SUBCLASS_LABEL))
    H.append(_sl_html("Anti-bot pages by protection strength", agg["slices"]["strength"],
                      pr, STRENGTH_LABEL))
    H.append(_sl_html("Document files by format", agg["slices"]["doc_type"], pr))
    H.append(_sl_html("Robustness probes", agg["slices"]["probes"], pr))

    H.append('<div class="fx-sec">Why it was not retrieved</div>')
    H.append('<p class="fx-note">When the metric only measures success, failure '
             'attribution is the second subject. <code>harness</code> counts our own '
             'faults and is listed separately. <strong>Lower is better in both '
             'columns</strong>; the superscript is the rank.</p>')
    fail_n = {p: sum(agg["failures"][p].values()) for p in agg["providers"]}
    fr, hr = rank_of(fail_n, False), rank_of(agg["harness_faults"], False)
    frows = []
    for prov in agg["providers_ranked"]:
        f = agg["failures"][prov]
        frows.append("<tr><td>%s</td><td>%d%s</td><td>%d%s</td><td>%s</td></tr>"
                     % (prov, fail_n[prov], _rk(fr.get(prov)),
                        agg["harness_faults"][prov], _rk(hr.get(prov)),
                        ", ".join("%s&times;%d" % kv for kv in sorted(f.items())) or "—"))
    H.append('<div class="fx-scroll"><table class="fx-t"><thead><tr><th>provider</th>'
             '<th>failures &darr;</th><th>harness &darr;</th><th>detail</th></tr></thead>'
             '<tbody>%s</tbody></table></div>' % "".join(frows))

    H.append('<div class="fx-sec">Diagnostic columns (not part of the score)</div>')
    H.append('<p class="fx-note">The superscript is the rank within the column. '
             '<strong>&uarr; higher is better &middot; &darr; lower is better &middot; '
             '&mdash; not ranked</strong> (reasons below the table).</p>')
    ranks = {}
    for _, field, _f in DIAG_COLS_SPEC:
        d = DIAG_DIRECTION.get(field)
        ranks[field] = ({} if d is None
                        else rank_of({p: agg["diagnostics"][p][field]
                                      for p in agg["providers"]}, d))
    arrow = {True: "&uarr;", False: "&darr;", None: "&mdash;"}
    head = "".join('<th>%s <span class="fx-n">%s</span></th>'
                   % (n, arrow[DIAG_DIRECTION.get(f)]) for n, f, _ in DIAG_COLS_SPEC)
    drows = []
    for prov in agg["providers_ranked"]:
        d = agg["diagnostics"][prov]
        tds = []
        for _, field, fmt in DIAG_COLS_SPEC:
            val = fmt(d)
            tds.append('<td class="%s">%s%s</td>'
                       % ("fx-na" if val is None else "", val or UNLABELLED,
                          _rk(ranks[field].get(prov))))
        drows.append("<tr><td>%s</td>%s</tr>" % (prov, "".join(tds)))
    H.append('<div class="fx-scroll"><table class="fx-t"><thead><tr><th>provider</th>%s'
             '</tr></thead><tbody>%s</tbody></table></div>' % (head, "".join(drows)))
    H.append('<p class="fx-note"><strong>Columns that are not ranked</strong>: %s. '
             'Ranking them would turn &ldquo;for reference only&rdquo; into '
             '&ldquo;bigger is better&rdquo;.</p>'
             % "; ".join("<code>%s</code> &mdash; %s" % (n, NO_RANK_WHY[f])
                         for n, f, _ in DIAG_COLS_SPEC if DIAG_DIRECTION.get(f) is None))

    H.append(render_glossary_html(agg))

    H.append('<div class="fx-sec">Methodology notes</div>')
    notes = ["<li><strong>Fetch capability only.</strong> The headline metric asks "
             "whether the page was retrieved. Text purity, structural fidelity and "
             "truncation completeness are parsing quality and were removed from the "
             "code.</li>",
             "<li>On anti-bot pages, passing is defined per wall type: WAF = the body "
             "was retrieved; login wall = the pre-wall content was retrieved "
             "<strong>and identified as a wall</strong>; paywall = the free portion "
             "was retrieved. <strong>Content from behind the wall earns "
             "nothing.</strong></li>",
             "<li>Protection tiers, derived by comparing the two ground-truth "
             "channels: %s</li>"
             % (m["strength_counts"] or UNLABELLED),
            ]
    if m["gt_gaps"]:
        notes.append("<li><strong>%d ground-truth gap pages.</strong> The judge skips "
                     "their vocabulary and the panel rules on the fetched content "
                     "itself: %s</li>"
                     % (len(m["gt_gaps"]),
                        ", ".join("%s/%s" % (g["pid"], g["host"]) for g in m["gt_gaps"])))
    cp = m.get("cache_pinned") or {}
    if cp:
        notes.append("<li><strong>Each provider's live-fetch (cache-bypass) switch was "
                     "set explicitly</strong>: %s. <code>no_knob</code> = the "
                     "documented API has no such parameter; <code>unpinned</code> = the "
                     "provider is not wired up yet.</li>"
                     % " · ".join("%s=%s" % kv for kv in sorted(cp.items())))
    notes.append("<li><strong>Content freshness is not measured.</strong> The switch "
                 "guarantees a live fetch happened; it does not guarantee every "
                 "provider saw the page at the same moment. A time-sensitive use case "
                 "needs a separate round.</li>")
    if m.get("rounds", 1) > 1:
        env = m.get("envelope") or {}
        notes.append("<li><strong>%d rounds were run and the headline takes the per-cell "
                     "median.</strong> %d of %d cells did not agree across rounds. Best "
                     "and worst round: %s. A difference smaller than that envelope is "
                     "round-to-round noise, not a capability gap.</li>"
                     % (m["rounds"], m["unstable_cells"], m["n_cells"],
                        "; ".join("%s %s&ndash;%s" % (p, _fmt(e["worst"]), _fmt(e["best"]))
                                  for p, e in sorted(env.items()))))
    else:
        notes.append("<li><strong>Only one round was run</strong>, so the headline "
                     "carries the full round-to-round variance of a single sample. On "
                     "defended pages that is substantial; use <code>--repeat</code> "
                     "before comparing providers.</li>")
    notes.append("<li>%d cells could not be judged; left genuinely unjudged, never "
                 "scored as zero</li>" % m["unjudged_total"])
    if m["verdicts_on_gt_gap_pages"]:
        notes.append("<li><strong>%d verdicts fall on ground-truth gap pages.</strong> "
                     "The panel had no reference render and ruled on the fetched "
                     "content alone, so the evidence is weaker than on pages with "
                     "ground truth</li>"
                     % m["verdicts_on_gt_gap_pages"])
    H.append('<div class="fx-card"><ul>%s</ul></div>' % "".join(notes))

    # Data is embedded as a JavaScript literal: a `<script type="application/json">`
    # block does not survive publishing, and the tables silently render empty.
    H.append("<script>window.FETCH_EVAL_DATA = %s;</script>"
             % json.dumps(agg, ensure_ascii=False))
    H.append("</div>")
    html = "\n".join(H)
    _assert_table_columns(html)
    return html


def _assert_table_columns(html: str) -> None:
    """Assert every table's header and its data rows agree on column count. A mismatch
    misaligns the report while still rendering."""
    import re as _re
    for tbl in _re.findall(r"<table.*?</table>", html, _re.S):
        heads = _re.findall(r"<thead>(.*?)</thead>", tbl, _re.S)
        bodies = _re.findall(r"<tbody>(.*?)</tbody>", tbl, _re.S)
        if not heads or not bodies:
            continue
        n_head = heads[0].count("<th")
        for tr in _re.findall(r"<tr>(.*?)</tr>", bodies[0], _re.S):
            n = tr.count("<td")
            assert n == n_head, ("table column mismatch: header %d, data row %d"
                                 % (n_head, n))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--out-html")
    ap.add_argument("--run-meta",
                    help="run_meta.json from the fetch round; defaults to the "
                         "verdicts directory")
    a = ap.parse_args()
    # Run parameters sit next to the verdicts; absent, the report simply omits the
    # pacing caveat rather than inventing one.
    meta_path = Path(a.run_meta) if a.run_meta else Path(a.verdicts).parent / "run_meta.json"
    run_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    agg = aggregate(load_jsonl(Path(a.verdicts)), load_jsonl(Path(a.pageset)), run_meta)
    md = render_markdown(agg)
    if a.out_md:
        Path(a.out_md).write_text(md, encoding="utf-8")
        print("Markdown -> %s" % a.out_md)
    else:
        print(md)
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        print("JSON -> %s" % a.out_json)
    if a.out_html:
        html = render_html(agg)
        Path(a.out_html).write_text(html, encoding="utf-8")
        print("HTML -> %s (%.2f MB)"
              % (a.out_html, len(html.encode()) / 1e6))


if __name__ == "__main__":
    main()
