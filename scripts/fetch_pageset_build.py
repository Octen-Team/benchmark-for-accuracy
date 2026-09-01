"""CSV -> 页面集 JSONL。**输入先拷进 data/ 版本化**，原文件不动。

只做机械转写与标签挂载；GT 由 fetch_gt 回写到 `.gt.jsonl`。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from src import fetch_spec as S


def label_for(url: str) -> dict:
    """URL 子串匹配逐页标签。**多条命中时 probes 取并集** —— gutenberg 同时是
    oversize 与 raw_direct，后一条覆盖前一条就丢了一个探针。"""
    expect, probes = "content", []
    for frag, lab in S.PAGE_LABELS.items():
        if frag in url:
            expect = lab.get("expect", expect)
            probes.extend(lab.get("probes", []))
    return {"expect": expect, "probes": sorted(set(probes))}


def build(csv_path: Path, out_path: Path, *, run_assert: bool = True) -> list[dict]:
    with csv_path.open(encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    hosts = Counter(urlparse(r["url"]).netloc for r in raw)
    rows = []
    for i, r in enumerate(raw, 1):
        url, cat = r["url"].strip(), r["category"].strip()
        host = urlparse(url).netloc
        typ = S.CATEGORY_TO_TYPE[cat]      # 未知类别就该 KeyError，不静默兜底
        dt, rule = S.doc_type_from_url(url)
        rows.append({
            "pid": "p%03d" % i,
            "url": url,
            "host": host,
            "host_dup": hosts[host] > 1,
            "category": cat,
            "type": typ,
            "defended": typ == "antibot",
            "antibot_subclass": S.ANTIBOT_SUBCLASS.get(host) if typ == "antibot" else None,
            "doc_type": dt,
            "doc_type_rule": rule,
            **label_for(url),
        })
    if run_assert:
        S.assert_pageset(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--archive", help="把输入 CSV 拷到这里版本化")
    ap.add_argument("--no-assert", action="store_true",
                    help="跳过 100 条边际断言（仅单测小样本用）")
    a = ap.parse_args()
    if a.archive:
        shutil.copy2(a.csv, a.archive)
    rows = build(Path(a.csv), Path(a.out), run_assert=not a.no_assert)
    print("写出 %d 条 -> %s" % (len(rows), a.out))
    for t, n in sorted(Counter(r["type"] for r in rows).items()):
        print("  %-12s %d" % (t, n))
    sub = Counter(r["antibot_subclass"] for r in rows if r["antibot_subclass"])
    print("  反爬小类 %s" % dict(sub))
    missing = sorted({r["host"] for r in rows
                      if r["type"] == "antibot" and not r["antibot_subclass"]})
    if missing:
        # 留 None 悄悄过去，报告里那几页就没有小类可切 —— 必须喊出来
        print("  !! 反爬小类查不到的 host（补进 fetch_spec 三张清单）: %s" % missing)
    print("  带 probe 的页 %d 条" % sum(1 for r in rows if r["probes"]))


if __name__ == "__main__":
    main()
