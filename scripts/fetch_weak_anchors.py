"""给 GT 缺口页补**弱锚点**：从中立 SERP 取这条 URL 的 title + snippet。

18 个页面我们自己的浏览器也被拦了，建不出参考词表，于是那些格只能交面板"裸判"——
而裸判系统性偏松。弱锚点补不回"内容全不全"，但足以激活**同一性**这条硬否决，
而"返回了首页 / 别的页"恰恰是裸判最容易放过的一类。

**为什么可以用 SERP**：google/bing 是评测体系里的中立第三方，不在被测名单上
（`src/serp.py` 的既有定位）。**绝不能用**某家 provider 的 unlocker 去建参考 ——
那是让参赛选手出考题，而 brightdata 本身就是候选厂商之一。

锚点只取 **title**（几乎必然出现在页面里，最稳）+ snippet 里的实词，并记
`anchor_source: "serp"`，报告要能说清有多少判定是靠弱锚点做出来的。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from src import fetch_gt as G
from src.serp import serp_fetch


def _norm(u: str) -> str:
    p = urlparse(u.lower())
    return (p.netloc + p.path).rstrip("/")


def serp_anchors(url: str, k: int = 8) -> dict:
    """查这条 URL 在 SERP 上的 title + snippet。命中判据是**归一化后的 URL 相等**，
    不是包含 —— 包含会把同域的别的页当成它自己。"""
    target = _norm(url)
    parsed = urlparse(url)
    host = parsed.netloc
    # 路径转成词：site:bestbuy.com apple macbook air 13 inch
    # 直接把带斜杠和连字符的原始路径当查询词，Google 基本查不到。
    slug = re.sub(r"[^a-z0-9]+", " ", parsed.path.lower()).strip()
    slug = " ".join(w for w in slug.split() if len(w) > 1)[:80]
    hits = []
    queries = ['"%s"' % url, url]
    if slug:
        queries.append("site:%s %s" % (host, slug))
    for query in queries:
        try:
            items = serp_fetch(query, engine="google", k=k)
        except Exception as e:                   # noqa: BLE001
            return {"anchor_source": "serp_failed", "error": str(e)[:150]}
        hits = [i for i in items if _norm(i.url) == target]
        if hits:
            break
        time.sleep(1)
    if not hits:
        return {"anchor_source": "serp_no_hit"}
    it = hits[0]
    title_terms = G._content_terms(it.title, set(), 6)
    snip_terms = [t for t in G._content_terms(it.snippet, set(title_terms), 6)]
    return {"anchor_source": "serp", "serp_title": it.title,
            "serp_snippet": it.snippet[:300],
            # 标题词最稳（几乎必然出现在页面里），snippet 补两个
            "anchors": (title_terms + snip_terms[:2])[:6]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True, help="带 gt 的页面集，原地回写")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    path = Path(a.pageset)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
    gaps = [r for r in rows if (r.get("gt") or {}).get("gt_gap")]
    if a.limit:
        gaps = gaps[:a.limit]
    print("GT 缺口页 %d 条，逐条查中立 SERP" % len(gaps))

    got = 0
    for i, r in enumerate(gaps, 1):
        res = serp_anchors(r["url"])
        g = r["gt"]
        g["anchor_source"] = res["anchor_source"]
        for k in ("serp_title", "serp_snippet", "error"):
            if k in res:
                g[k] = res[k]
        if res.get("anchors"):
            g["anchors"] = res["anchors"]
            got += 1
        print("  %-6s %-26s %-14s %s" % (r["pid"], r["host"][:26], res["anchor_source"],
                                         res.get("anchors") or res.get("error", "")))
        time.sleep(1)

    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n拿到弱锚点 %d / %d 条（其余仍只能裸判）" % (got, len(gaps)))


if __name__ == "__main__":
    main()
