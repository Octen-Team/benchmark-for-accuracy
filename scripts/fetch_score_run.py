"""判定驱动：抽取结果 + 带 GT 的页面集 -> verdicts.jsonl。

逐条落盘、按 (pid, provider, run_seq) 续跑；面板裁决按指纹缓存，重跑不重复烧 token。
`--no-panel` 只跑机械层（不花 LLM 钱，用于验通路）。
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
    # **按 "\n" 切，不能用 splitlines()**：后者还会在 U+2028 / U+2029 / U+0085 上切，
    # 而那些字符在网页正文里合法出现、json.dumps 也不转义 —— 一条完整记录会被从
    # 中间劈开，报出一个看不懂的 "Unterminated string"。实测 500 条抓取里就有。
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
        # 三 family 三模型是设计要求；凑不齐要喊出来而不是悄悄用两家
        raise RuntimeError("解析不出 3 个 family 的面板，只拿到: %s" % panel)
    return panel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractions", required=True)
    ap.add_argument("--pageset", required=True, help="带 gt 的页面集")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-panel", action="store_true", help="只跑机械层，不花 LLM 钱")
    ap.add_argument("--panel", nargs="+", help="覆盖面板模型")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--gold", default="data/fetch_gold_gap.jsonl",
                    help="人工核过的结论，优先级最高；文件不存在则跳过")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="并发判定的格数（每格内部三个模型仍顺序调）")
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
        print("金标 %d 条（人工核过，优先级最高，覆盖面板）" % len(gold))
    panel = None if a.no_panel else resolve_panel(a.panel)
    cache = None if a.no_panel else PanelCache(out.parent / "panel_cache.jsonl")
    print("待判 %d 格（共 %d）；面板 = %s" % (len(todo), len(rows), panel or "关"))

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

    # **没有参考答案的页整页一起判**：五家的返回并排给面板，只要有一家真拿到了，
    # 其余的错就现形。既比单家裸判准，又把 N 家 N 次调用降成 1 次。
    gap_pids = {pid for pid, p in pages.items() if (p.get("gt") or {}).get("gt_gap")}
    cross_todo: dict[str, dict] = {}
    solo_todo = []
    for r in todo:
        if panel and r["pid"] in gap_pids and r.get("run_seq", 0) == 0:
            cross_todo.setdefault(r["pid"], {})[r["provider"]] = r
        else:
            solo_todo.append(r)
    if cross_todo:
        print("其中 %d 页无参考 -> 整页交叉判（%d 格，%d 次调用而不是 %d 次）"
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
    print("\n判定分布: %s" % Counter(v["verdict"] for v in allv))
    print("判不了 %d 格（如实留空，不当 0 分）"
          % sum(1 for v in allv if v["verdict"] is None))
    print("dishonest %d · 三方分歧 %d"
          % (sum(1 for v in allv if v.get("dishonest")),
             sum(1 for v in allv if v.get("panel_split"))))


if __name__ == "__main__":
    main()
