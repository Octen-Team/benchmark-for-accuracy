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
from src.fetch_score import PanelCache, score_one


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
            return score_one(pages[r["pid"]], r, panel=panel, cache=cache)
        except Exception as e:                   # noqa: BLE001
            return _crashed(r, e)

    # **Each provider is judged on its own return, never alongside the others.** A
    # side-by-side comparison would let one provider's success set the bar for the rest,
    # which measures relative completeness rather than whether each got the page.
    t0, done_n = time.time(), 0
    total = len(todo)
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for v in ex.map(_one, todo):
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
