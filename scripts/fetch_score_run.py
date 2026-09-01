"""Judging driver: extractions + a page set with ground truth -> verdicts.jsonl.

Rows are appended one at a time and resume on (pid, provider, run_seq). Panel rulings are
cached by fingerprint, so a re-run does not spend tokens twice. `--no-panel` runs the
mechanical layer alone, which costs nothing and is useful for checking the wiring.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.fetch_io import append, progress
from src.fetch_score import GoldStore, PanelCache, score_one, score_page_cross


def load_jsonl(p: Path) -> list[dict]:
    # **Split on "\n" only — never splitlines().** The latter also splits on U+2028,
    # U+2029 and U+0085, which occur legitimately in page text and which json.dumps does
    # not escape. One record gets torn in half and surfaces as "Unterminated string".
    return [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]


def done_keys(p: Path) -> set:
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").split("\n"):
        try:
            r = json.loads(line)
            out.add((r["pid"], r["provider"], int(r.get("run_seq", 0))))
        except Exception:                        # noqa: BLE001
            continue
    return out


def resolve_panel(override: list[str] | None):
    if override:
        return override
    from src.rubric_review import PANEL_PREFS
    import requests
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    r.raise_for_status()
    ids = {m["id"] for m in r.json()["data"]}
    panel = [next((c for c in cands if c in ids), None) for cands in PANEL_PREFS.values()]
    panel = [p for p in panel if p]
    if len(panel) < 3:
        # Three models from three families is a design requirement; falling back to two
        # silently would weaken the panel without anyone noticing
        raise RuntimeError("could not resolve a panel spanning 3 families; got: %s" % panel)
    return panel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractions", required=True)
    ap.add_argument("--pageset", required=True, help="page set carrying ground truth")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-panel", action="store_true",
                    help="mechanical layer only; spends no LLM tokens")
    ap.add_argument("--panel", nargs="+", help="override the panel models")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--gold", default="data/fetch_gold_gap.jsonl",
                    help="human-verified verdicts, highest priority; skipped if absent")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="cells judged concurrently (the three models within a cell\n                         still run in sequence)")
    a = ap.parse_args()

    pages = {p["pid"]: p for p in load_jsonl(Path(a.pageset))}
    rows = load_jsonl(Path(a.extractions))
    rows = [r for r in rows if r["pid"] in pages]
    rows.sort(key=lambda r: (r["pid"], r["provider"], r.get("run_seq", 0)))
    if a.limit:
        rows = rows[:a.limit]
    out = Path(a.out)
    done = done_keys(out)
    todo = [r for r in rows if (r["pid"], r["provider"], r.get("run_seq", 0)) not in done]

    gold = GoldStore(a.gold)
    if len(gold):
        print("%d gold entries (human-verified, highest priority, override the panel)"
              % len(gold))
    panel = None if a.no_panel else resolve_panel(a.panel)
    cache = None if a.no_panel else PanelCache(out.parent / "panel_cache.jsonl")
    print("%d cells to judge (of %d); panel = %s" % (len(todo), len(rows), panel or "off"))

    def _crashed(r: dict, e: Exception) -> dict:
        p = pages[r["pid"]]
        return {"pid": r["pid"], "provider": r["provider"], "type": p["type"],
                "verdict": None, "reason": "scorer_crashed:%s" % type(e).__name__,
                "checks": {}, "run_seq": r.get("run_seq", 0),
                "latency_ms": r.get("latency_ms"), "len_norm": 0,
                "dishonest": False, "suspicious_bypass": False, "panel_split": False,
                "failure_reason": r.get("failure_reason"), "fault": r.get("fault"),
                "antibot_subclass": p.get("antibot_subclass"),
                "strength": (p.get("gt") or {}).get("strength")}

    def _one(r: dict) -> dict:
        try:
            return score_one(pages[r["pid"]], r, panel=panel, cache=cache, gold=gold)
        except Exception as e:                   # noqa: BLE001
            return _crashed(r, e)

    # **Pages with no reference answer are judged whole.** Every provider's return is
    # shown side by side, so as soon as one of them genuinely got the page the others'
    # failures become visible. More accurate than judging each alone, and N providers
    # cost one call instead of N.
    gap_pids = {pid for pid, p in pages.items() if (p.get("gt") or {}).get("gt_gap")}
    cross_todo: dict[str, dict] = {}
    solo_todo = []
    for r in todo:
        if panel and r["pid"] in gap_pids and r.get("run_seq", 0) == 0:
            cross_todo.setdefault(r["pid"], {})[r["provider"]] = r
        else:
            solo_todo.append(r)
    if cross_todo:
        print("  %d of them have no reference -> cross-judged whole "
              "(%d cells, %d calls instead of %d)"
              % (len(cross_todo), sum(len(v) for v in cross_todo.values()),
                 len(cross_todo) * len(panel),
                 sum(len(v) for v in cross_todo.values()) * len(panel)))

    def _page(pid: str) -> list[dict]:
        try:
            return list(score_page_cross(pages[pid], cross_todo[pid], panel,
                                         cache=cache, gold=gold).values())
        except Exception as e:               # noqa: BLE001
            return [_crashed(r, e) for r in cross_todo[pid].values()]

    t0, done_n = time.time(), 0
    total = len(solo_todo) + sum(len(v) for v in cross_todo.values())
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for group in ex.map(_page, list(cross_todo)):
            for v in group:
                append(out, v)
                done_n += 1
            if (s := progress(done_n, total, t0)):
                print(s)
        for v in ex.map(_one, solo_todo):
            append(out, v)
            done_n += 1
            if (s := progress(done_n, total, t0)):
                print(s)

    allv = load_jsonl(out)
    from collections import Counter
    print("\nverdict distribution: %s" % Counter(v["verdict"] for v in allv))
    print("%d cells unjudged (left genuinely blank, never scored as zero)"
          % sum(1 for v in allv if v["verdict"] is None))
    print("dishonest %d · panel splits %d"
          % (sum(1 for v in allv if v.get("dishonest")),
             sum(1 for v in allv if v.get("panel_split"))))


if __name__ == "__main__":
    main()
