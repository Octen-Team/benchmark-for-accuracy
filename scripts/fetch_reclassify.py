"""离线重分类失败原因 —— 错误原文都存着，不用重抓。

用途：分类规则改了（比如 403 拆成"政策拒绝"与"真被拦"）之后，把已有的抓取结果和
判定结果就地更新。**判定档位不受影响**（传输失败一律 lost），变的只是归因那一列。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from src.fetch_backends import classify_body


def reclassify(rec: dict) -> bool:
    if rec.get("status") != "error":
        return False
    told = classify_body(rec.get("http_status") or 0, rec.get("error") or "")
    if not told:
        return False
    reason, fault = told
    if rec.get("failure_reason") == reason:
        return False
    rec["failure_reason"], rec["fault"] = reason, fault
    if isinstance(rec.get("reason"), str) and rec["reason"].startswith("fetch_failed:"):
        rec["reason"] = "fetch_failed:%s" % reason
    return True


def sync_from(extractions: Path, rows: list[dict]) -> int:
    """把归因从抓取结果同步到判定结果。

    **判定记录里没有 `http_status` / `error`**，自己重新分类是分不出来的 ——
    只能按 (pid, provider) 从抓取结果那边搬过来。不同步的话两个文件会对不上，
    而报告读的是判定文件，等于重分类白做。
    """
    src = {}
    for line in extractions.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        r = json.loads(line)
        src[(r["pid"], r["provider"], r.get("run_seq", 0))] = (
            r.get("failure_reason"), r.get("fault"))
    n = 0
    for rec in rows:
        key = (rec.get("pid"), rec.get("provider"), rec.get("run_seq", 0))
        if key not in src:
            continue
        reason, fault = src[key]
        if reason and rec.get("failure_reason") != reason:
            rec["failure_reason"], rec["fault"] = reason, fault
            if isinstance(rec.get("reason"), str) and rec["reason"].startswith("fetch_failed:"):
                rec["reason"] = "fetch_failed:%s" % reason
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="extractions.jsonl / verdicts.jsonl")
    ap.add_argument("--sync-from", help="判定文件从这份抓取结果同步归因")
    a = ap.parse_args()
    for f in a.files:
        p = Path(f)
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]
        n = sum(1 for r in rows if reclassify(r))
        if a.sync_from:
            n += sync_from(Path(a.sync_from), rows)
        with p.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        after = Counter(r.get("failure_reason") for r in rows if r.get("failure_reason"))
        print("%s: 改了 %d 条" % (p, n))
        for k, v in after.most_common(5):
            print("    %-22s %d" % (k, v))


if __name__ == "__main__":
    main()
