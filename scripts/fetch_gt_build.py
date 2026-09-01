"""GT 建库：把页面集跑成带 `gt` 的冻结文件。

**分层建**（设计文档 §二）—— 强 GT 只建在主指标确实需要词表的那批上：

  docfmt 22     解析通道。解析结果即 GT，全场质量最好的一批，不需要浏览器也不需要 key
  baseline 8    headless。干净 HTML，秒取
  render 18     headless。渲染本身就是要考的能力
  antibot 42    真实 Chrome（要逐域授权，本脚本跑不了）。**主指标是过墙率，不需要文本 GT**
  reliability 10  不需要文本 GT，只要 expect + probes

逐条落盘、按 pid 断点续跑。中途被杀不丢已建好的部分。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from src import fetch_gt as G
from src.fetch_io import append, progress

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _load(path: Path) -> list[dict]:
    # **按 "\n" 切，不能用 splitlines()**：后者还会在 U+2028 / U+2029 / U+0085 上切，
    # 而那些字符在网页正文里合法出现、json.dumps 也不转义 —— 一条完整记录会被从
    # 中间劈开，报出一个看不懂的 "Unterminated string"。实测 500 条抓取里就有。
    return [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]


def _done_keys(path: Path) -> set[tuple[str, str]]:
    """已建的 (pid, 通道)。**键必须含通道** —— 防护强度档是两条通道的比对结果，
    只按 pid 去重的话第二条通道会被整轮跳过，强度就永远算不出来。"""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        try:
            r = json.loads(line)
            out.add((r["pid"], (r.get("gt") or {}).get("channel", "?")))
        except Exception:                        # noqa: BLE001  坏行跳过，不让整轮起不来
            continue
    return out


def consolidate(path: Path) -> int:
    """同一 pid 的多条通道记录合并成一条：内容取**成功那条**（真 Chrome 优先），
    两条通道的成败标记都保留下来给强度档用。"""
    rows = _load(path)
    merged: dict[str, dict] = {}
    for r in rows:
        pid = r["pid"]
        g = r.get("gt") or {}
        if pid not in merged:
            merged[pid] = r
            continue
        prev = merged[pid]
        pg = prev.get("gt") or {}
        flags = {k: v for k, v in list(pg.items()) + list(g.items())
                 if k in ("headless_ok", "chrome_ok")}
        # 真 Chrome 拿到内容就用它的；否则保留原先那条有内容的
        better = r if (g.get("channel") == "chrome_real" and g.get("vocab_n")) else (
            r if (g.get("vocab_n") and not pg.get("vocab_n")) else prev)
        better = json.loads(json.dumps(better))          # 不改原对象
        better["gt"].update(flags)
        merged[pid] = better
    with path.open("w", encoding="utf-8") as f:
        for r in merged.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows) - len(merged)


def _channel_of(page: dict, browser_channel: str) -> str:
    """这一页会走哪条通道 —— 非 HTML 一律解析通道，与浏览器通道无关。"""
    return "parse" if page["doc_type"] != "html" else browser_channel


def _raw_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))


def build_parse(page: dict, timeout: int, sidecar: Path | None = None) -> dict:
    """解析通道：下原始字节 -> 嗅探真实类型 -> 类型化解析。"""
    try:
        r = requests.get(page["url"], timeout=timeout, headers={"User-Agent": UA})
    except Exception as e:                       # noqa: BLE001
        return {"channel": "parse", "rule": "fetch_failed",
                "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
    dt, rule = G.sniff_doc_type(r.content, r.headers.get("content-type", ""),
                                page.get("doc_type", "unknown"))
    parsed = G.parse_document(r.content, dt, page["url"])
    _save_raw(sidecar, page["pid"], parsed["text"], "")
    v = G.derive_vocab(parsed["text"], "")       # 文档没有 nav/footer，样板词表天然为空
    return {"channel": "parse", "http_status": r.status_code,
            "doc_type_observed": dt, "doc_type_rule": rule,
            "struct": parsed["struct"], "rule": parsed["rule"],
            "title": page["url"].rsplit("/", 1)[-1],
            "text_len": len(parsed["text"]), **v}


def _save_raw(sidecar: Path | None, pid: str, main: str, boiler: str) -> None:
    """把原始正文与样板文本另存一份。**派生口径一改就要重跑整轮浏览器**，代价太大 ——
    今晚为了给锚点换个来源就白跑了一遍 58 页。存下来之后 `--rederive` 可以离线重算。"""
    if not sidecar:
        return
    append(sidecar, {"pid": pid, "main_text": main, "boiler_text": boiler})


def build_browser(page: dict, timeout: int, shots: str | None,
                  sidecar: Path | None = None,
                  channel_name: str = "playwright_headless") -> dict:
    """浏览器通道：渲染后取正文与样板两侧，另算"仅渲染后可见"的锚点。"""
    got = G.render_page(page["url"], channel=channel_name, timeout=timeout,
                        screenshot_dir=shots, pid=page["pid"])
    if got.get("rule") != "rendered":
        flag = "chrome_ok" if channel_name == "chrome_real" else "headless_ok"
        return {"channel": channel_name, flag: False,
                "rule": got.get("rule", "render_failed"),
                "error": got.get("error"), "http_status": got.get("http_status")}
    _save_raw(sidecar, page["pid"], got["main_text"], got["boiler_text"])
    v = G.derive_vocab(got["main_text"], got["boiler_text"])
    walled = G.gt_is_walled(got["main_text"], got.get("title", ""))
    render_anchors: list[str] = []
    if page["type"] == "render":
        # 渲染型的主指标要的是"JS 执行后才出现"的词：渲染结果减去原始 HTTP 响应体
        try:
            raw = requests.get(page["url"], timeout=timeout,
                               headers={"User-Agent": UA}).text
            before = set(G.tokenize(_raw_text(raw)))
            render_anchors = [t for t in v["vocab"] if t not in before][:12]
        except Exception:                        # noqa: BLE001
            render_anchors = []
    ok = bool(v.get("vocab_n")) and not walled
    flag = "chrome_ok" if channel_name == "chrome_real" else "headless_ok"
    return {"channel": channel_name, flag: ok,
            # 墙页渲染"成功"且有词表 —— 只看 vocab_n 兜不住，必须显式标出来
            "rule": "rendered_wall" if walled else "rendered",
            "gt_wall_hit": walled,
            "http_status": got.get("http_status"), "title": got.get("title", ""),
            "struct": got.get("struct", {}), "screenshot_path": got.get("screenshot_path"),
            "render_anchors": render_anchors,
            "text_len": len(got["main_text"]), **v}


def backfill_gaps(path: Path) -> list[dict]:
    """标 GT 缺口。**两类都要标**：

      建不出来   `expect == content` 的页却没有词表
      建成了墙   渲染"成功"、有词表，但拿到的是验证页 —— w3.org 实测就是这样，
                 只看 vocab_n 兜不住，而不标的话全场都在跟一张验证页比对

    缺口页的词表照旧留在文件里供排查，但判定器会跳过它们（见 fetch_score.run_checks）。
    """
    rows = _load(path)
    gaps = []
    for r in rows:
        g = r["gt"] or {}
        walled = bool(g.get("gt_wall_hit"))
        empty = r.get("expect") == "content" and not g.get("vocab_n")
        g["gt_gap"] = walled or empty
        if g["gt_gap"]:
            gaps.append({"pid": r["pid"], "host": r.get("host"),
                         "why": "walled" if walled else "no_vocab",
                         "rule": g.get("rule")})
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return gaps


def backfill_strength(path: Path) -> dict:
    """防护强度 = 两条 GT 通道的比对结果（设计文档 一.4）。

    **真 Chrome 没跑过时必须是 unknown，不能默认 hard** —— 那会把"没测"伪装成
    "测出来最难"，凭空给这一列添一档不存在的结论。
    """
    rows = _load(path)
    counts: Counter = Counter()
    for r in rows:
        g = r["gt"] or {}
        # 旧版本建的行没有显式标记，从通道与结果反推一次（成功 = 有词表且不是墙页）
        if "headless_ok" not in g and g.get("channel") == "playwright_headless":
            g["headless_ok"] = bool(g.get("vocab_n")) and not g.get("gt_wall_hit")
        if "chrome_ok" not in g and g.get("channel") == "chrome_real":
            g["chrome_ok"] = bool(g.get("vocab_n")) and not g.get("gt_wall_hit")
        g["strength"] = G.derive_strength(
            g.get("headless_ok") if "headless_ok" in g else None,
            g.get("chrome_ok") if "chrome_ok" in g else None)
        counts[g["strength"]] += 1
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return dict(counts)


def backfill_anchors(path: Path) -> int:
    """**第二遍扫**：先收集全场词表算文档频率，再给每页定独有锚点。

    必须是第二遍 —— "独有"是相对全集说的，建第一页时还不知道别的页有什么。
    漏掉这一遍的后果是静默的：`gt.anchors` 缺失 -> `identity_ok` 永远返回 None ->
    "返回错页"那条全型硬否决**看着实现了但从来不生效**。而它抓的正是最坏的一类
    静默失败（退回父页 / 索引页 / 搜索结果页），下游发现不了。
    """
    rows = _load(path)
    n = len(rows)
    df: Counter = Counter()
    for r in rows:
        g = r["gt"] or {}
        # **标题词也要进 df**：解析通道把文件名当标题，pdf / txt 这类词只出现在文件名里，
        # 只统计 vocab 的话它们的 df 是 0，一路混进锚点。
        terms = set(g.get("vocab") or []) | set(G.tokenize(g.get("title") or ""))
        df.update(terms)
    filled = 0
    for r in rows:
        g = r["gt"] or {}
        anchors = G.derive_anchors(g.get("title") or "",
                                   g.get("vocab_head") or g.get("vocab") or [],
                                   df, n, url=r.get("url", ""))
        g["anchors"] = anchors
        g["anchors_df_pages"] = n          # 锚点是相对这 n 页算的，报告里要能说清
        if anchors:
            filled += 1
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return filled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--types", nargs="+", default=["docfmt", "baseline", "render"])
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=6, help="仅用于解析通道")
    ap.add_argument("--shots", default=None, help="截图目录（浏览器通道）")
    ap.add_argument("--channel", default="playwright_headless",
                    choices=["playwright_headless", "chrome_real"],
                    help="浏览器通道。chrome_real 启动本机真实 Chrome（有头、干净 profile、不登录）")
    ap.add_argument("--sidecar", help="另存原始正文，供 --rederive 离线重算")
    ap.add_argument("--anchors-only", action="store_true",
                    help="只跑锚点回填（第二遍扫），不重新抓页")
    a = ap.parse_args()

    sidecar = Path(a.sidecar) if a.sidecar else None
    if a.anchors_only:
        gg = backfill_gaps(Path(a.out))
        print("GT 缺口 %d 条: %s" % (len(gg), [(g["pid"], g["why"]) for g in gg]))
        print("锚点回填：%d 条有独有锚点" % backfill_anchors(Path(a.out)))
        print("防护强度档：%s" % backfill_strength(Path(a.out)))
        return
    pages = [p for p in _load(Path(a.pageset)) if p["type"] in set(a.types)]
    pages.sort(key=lambda p: p["pid"])
    if a.limit:
        pages = pages[:a.limit]
    out = Path(a.out)
    done = _done_keys(out)
    todo = [p for p in pages if (p["pid"], _channel_of(p, a.channel)) not in done]
    print("目标 %d 条，已建 %d 条，待建 %d 条" % (len(pages), len(pages) - len(todo), len(todo)))

    # **按 doc_type 路由，不按 type。** 无后缀的 arxiv 那条属于 reliability 型但它是
    # PDF —— 送进浏览器只会拿到 PDF 阅读器外壳。unknown 也走解析通道，嗅探会定下来。
    parse_jobs = [p for p in todo if p["doc_type"] != "html"]
    browser_jobs = [p for p in todo if p["doc_type"] == "html"]
    t0, n = time.time(), 0
    total = len(todo)

    # 解析通道：纯 HTTP，可并发
    if parse_jobs:
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = {ex.submit(build_parse, p, a.timeout, sidecar): p for p in parse_jobs}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    gt = fut.result()
                except Exception as e:           # noqa: BLE001
                    gt = {"channel": "parse", "rule": "builder_crashed",
                          "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
                append(out, {**p, "gt": gt})
                n += 1
                if (s := progress(n, total, t0)):
                    print(s)

    # 浏览器通道：playwright 同步 API 顺序跑，一次一个 driver
    for p in browser_jobs:
        try:
            gt = build_browser(p, a.timeout, a.shots, sidecar, a.channel)
        except Exception as e:                   # noqa: BLE001
            gt = {"channel": "playwright_headless", "rule": "builder_crashed",
                  "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
        append(out, {**p, "gt": gt})
        n += 1
        if (s := progress(n, total, t0)):
            print(s)

    dropped = consolidate(out)
    if dropped:
        print("多通道记录合并：%d 条重复行并入" % dropped)
    walled = [r["pid"] for r in _load(out) if r["gt"].get("gt_wall_hit")]
    if walled:
        print("!! GT 自己被拦的 %d 条（必须标缺口，否则全场跟一张验证页比对）: %s"
              % (len(walled), walled))
    gaps = backfill_gaps(out)
    if gaps:
        print("GT 缺口 %d 条（判定器会跳过它们的词表）: %s"
              % (len(gaps), [(g["pid"], g["why"]) for g in gaps]))
    filled = backfill_anchors(out)
    print("锚点回填：%d 条有独有锚点" % filled)
    print("防护强度档：%s（真 Chrome 没跑过的一律 unknown，不默认 hard）"
          % backfill_strength(out))
    rows = _load(out)
    ok = [r for r in rows if r["gt"].get("vocab_n")]
    degen = [r["pid"] for r in rows if r["gt"].get("degenerate")]
    failed = [(r["pid"], r["gt"].get("rule")) for r in rows if not r["gt"].get("vocab_n")]
    print("\n落盘 %d 条；有词表 %d 条" % (len(rows), len(ok)))
    print("退化页（vocab_n < 12，走面板不走机械阈值）%d 条: %s" % (len(degen), degen))
    if failed:
        # 建不出来的要喊出来，不能留个空 gt 让下游当成"这页没内容"
        print("!! 没建出词表的 %d 条: %s" % (len(failed), failed))


if __name__ == "__main__":
    main()
