"""把人工核对页导出的标注收进金标文件。

金标**优先级最高**：判定时直接采用，覆盖机械层与面板。这批页本来是全场证据最弱的
（我们自己的浏览器也抓不到，没有参考答案），人工核过之后反而成了最硬的一批，
而且以后每一轮都复用 —— 只要那一格的抓取内容没变。

`unsure` 不写进金标：拿不准就让它继续走面板，人工的"我也说不准"不该固化成结论。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID = {"pass", "partial", "lost"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--marks", help="人工核对页导出的 JSONL 文件；不给则从 stdin 读")
    ap.add_argument("--out", default="data/fetch_gold_gap.jsonl")
    ap.add_argument("--extractions", required=True,
                    help="用来记抓取内容的指纹 —— 内容变了金标要失效")
    a = ap.parse_args()

    raw = (Path(a.marks).read_text(encoding="utf-8") if a.marks else sys.stdin.read())
    marks = [json.loads(l) for l in raw.split("\n") if l.strip()]

    import hashlib
    fp = {}
    for line in Path(a.extractions).read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("run_seq", 0) == 0:
            fp[(r["pid"], r["provider"])] = hashlib.sha256(
                (r.get("text") or "").encode("utf-8", "replace")).hexdigest()[:16]

    out, skipped = [], 0
    for m in marks:
        v = m.get("human_verdict")
        if v not in VALID:
            skipped += 1              # unsure / 空 —— 让它继续走面板
            continue
        key = (m["pid"], m["provider"])
        out.append({"pid": m["pid"], "provider": m["provider"], "verdict": v,
                    "text_sha": fp.get(key, "")})

    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("金标写出 %d 条 -> %s（跳过 %d 条拿不准的，它们继续走面板）"
          % (len(out), path, skipped))
    missing = sum(1 for r in out if not r["text_sha"])
    if missing:
        print("!! %d 条在抓取结果里找不到对应内容 —— 它们的金标不会生效" % missing)


if __name__ == "__main__":
    main()
