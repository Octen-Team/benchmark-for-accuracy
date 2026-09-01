"""抓取能力评测报告。

**本轮只评抓取能力**，主口径是单一的**抓取成功率**，所有页共用一个单位 ——
所以**总分成立**，五个 type 从"各有各的主指标"降级成切片轴。
正文纯度 / 结构保真 / 截断完整度三项属于解析质量，已从代码里删除。

**缺档一律写"未标注"，不静默归零**（playbook §7.3）。
"""
from __future__ import annotations

import argparse
import json
import statistics as stat
from collections import Counter, defaultdict
from pathlib import Path

from src.fetch_spec import TH

WEIGHT = {"pass": 1.0, "partial": 0.5, "lost": 0.0}
UNLABELLED = "未标注"
TYPE_LABEL = {"baseline": "静态文档", "render": "渲染/SPA", "docfmt": "文档文件",
              "antibot": "反爬", "reliability": "健壮性"}
SUBCLASS_LABEL = {"waf": "WAF", "login_wall": "登录墙", "paywall": "付费墙"}
STRENGTH_LABEL = {"soft": "软", "medium": "中", "hard": "硬", "unknown": "未测"}


def load_jsonl(p: Path) -> list[dict]:
    # **按 "\n" 切，不能用 splitlines()**：后者还会在 U+2028 / U+2029 / U+0085 上切，
    # 而那些字符在网页正文里合法出现、json.dumps 也不转义 —— 一条完整记录会被从
    # 中间劈开，报出一个看不懂的 "Unterminated string"。实测 500 条抓取里就有。
    return [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]


def weighted(verdicts) -> dict:
    """抓取成功率 = (成功 ×1.0 + 部分 ×0.5) / N。
    **判不了的单独计数，不进分母也不当 0。**"""
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
    """每个桶另记 `_gap_share` —— 这一桶里有多少判定落在 GT 缺口页上。

    **不按桶报置信度会误导人**：实测「硬」档 100% 的格都在缺口页上（连真 Chrome 都
    拿不到参考），面板只能凭抓取内容自己判，而那种情况下它偏松。于是出现了
    「硬档 88% > 软档 60%」的倒挂 —— 那是测量置信度的假象，不是能力差异。
    """
    gapped = gapped or set()
    buckets: dict = {}
    for bucket, rs in _by([r for r in rows if keyfn(r) is not None], keyfn).items():
        b = {p: weighted([x["verdict"] for x in rs if x["provider"] == p])
             for p in providers}
        # **人工核过的格不算低置信** —— 它们本来是全场最弱的，核完反而是最硬的。
        # 不排掉的话，人工标了半天 ⚠ 也不会消失。
        weak = [r for r in rs if r["pid"] in gapped and r.get("reason") != "human_gold"]
        b["_gap_share"] = (len(weak) / len(rs)) if rs else 0.0
        b["_human_verified"] = sum(1 for r in rs if r.get("reason") == "human_gold")
        b["_n_pages"] = len({r["pid"] for r in rs})
        buckets[bucket] = b
    return buckets


# 每一列的方向。**没有方向的列不排名** —— 硬排会把"仅供参考"读成"越大越好"。
#   True  = 越大越好      False = 越小越好      None = 不排名，并说明为什么
DIAG_DIRECTION = {
    # 延迟**不排名**：本轮对 octen 设了 2.5 秒/请求的节流，它的耗时里含我们主动等待的
    # 时间。报告的陷阱条已经写着"延迟不能横向比"，再给它排个名就是自相矛盾。
    # 想比速度得另跑一轮不设节流的。
    "latency_p50": None, "latency_p90": None, "latency_n": True,
    "slow_losses": False, "dishonest": False, "wrong_page": False,
    "mojibake": False, "suspicious_bypass": False,
    "len_norm_median": None,   # 各家正文剥离口径不同，长不等于好
    "panel_split": None,       # 量的是格子有多难判，不是厂商好坏
}
NO_RANK_WHY = {
    "latency_p50": "本轮有厂商被我们主动节流，跨家不可比",
    "latency_p90": "同上",
    "len_norm_median": "各家正文剥离口径不同，长不等于好",
    "panel_split": "量的是这些格子有多难判，不是厂商好坏",
}


def rank_of(values: dict, higher_is_better: bool) -> dict:
    """{key: 名次}。**并列同名次**；值为 None 的不参与排名。"""
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


# 诊断表的列定义：**markdown 与 HTML 共用一份** —— 两处各写一份迟早分叉。
DIAG_COLS_SPEC = [
    ("P50", "latency_p50", lambda d: "%.0f ms" % d["latency_p50"] if d["latency_p50"] else None),
    ("P90", "latency_p90", lambda d: "%.0f ms" % d["latency_p90"] if d["latency_p90"] else None),
    ("计入延迟", "latency_n", lambda d: str(d["latency_n"])),
    ("长度中位", "len_norm_median",
     lambda d: _fmt(d["len_norm_median"], pct=False) if d["len_norm_median"] else None),
    ("慢失败", "slow_losses", lambda d: str(d["slow_losses"])),
    ("dishonest", "dishonest", lambda d: str(d["dishonest"])),
    ("抓错页", "wrong_page", lambda d: str(d["wrong_page"])),
    ("乱码", "mojibake", lambda d: str(d["mojibake"])),
    ("疑似绕墙", "suspicious_bypass", lambda d: str(d["suspicious_bypass"])),
    ("三方分歧", "panel_split", lambda d: str(d["panel_split"])),
]


def _cache_states(providers) -> dict:
    """各家的实时抓开关状态。**从 adapter 读，不手抄** —— 手抄的表会和代码分叉。"""
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


def aggregate(verdicts: list[dict], pages: list[dict]) -> dict:
    """第一轮（run_seq 0）进主口径，重复轮只用于抖动。"""
    pmap = {p["pid"]: p for p in pages}
    base = [v for v in verdicts if v.get("run_seq", 0) == 0]
    providers = sorted({v["provider"] for v in base})

    # ── 主口径：单一抓取成功率（跨型可比，所以总分成立）──────────────────
    overall = {p: weighted([r["verdict"] for r in base if r["provider"] == p])
               for p in providers}
    ranking = sorted([(p, overall[p]["weighted"]) for p in providers
                      if overall[p]["weighted"] is not None], key=lambda x: -x[1])

    # ── 切片 ────────────────────────────────────────────────────────────
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

    # ── 按域去重的副口径（同域先取域内均值再进总分）──────────────────────
    host_dedup = {}
    for prov in providers:
        rs = [r for r in base if r["provider"] == prov and r["verdict"] in WEIGHT]
        per_host = _by(rs, lambda r: pmap[r["pid"]]["host"])
        vals = [stat.mean(WEIGHT[r["verdict"]] for r in g) for g in per_host.values()]
        host_dedup[prov] = {"weighted": stat.mean(vals) if vals else None,
                            "hosts": len(vals)}

    # ── 诊断列（不进分数）────────────────────────────────────────────────
    diag = {}
    for prov in providers:
        rs = [v for v in base if v["provider"] == prov]
        usable = [r["latency_ms"] for r in rs
                  if r["verdict"] in ("pass", "partial") and r.get("latency_ms")]
        diag[prov] = {
            # **延迟只统计抓到了内容的调用**，否则量的是超时的快慢
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

    # ── 失败归因：指标只看成败时，"为什么没抓到"是报告的第二主角 ─────────────
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
        "scope": "只评抓取能力：主口径是抓取成功率，不评解析质量",
        "type_counts": {t: n for t, n in Counter(p["type"] for p in pages).items()},
        "providers": providers,
        # 所有表的行序统一按总分名次 —— 每张表各排各的会让读者对不上号
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
        },
    }


# ── 口径速查（跟报告放在一起，产品直接读）────────────────────────────────

MAIN_COLS = [
    ("抓取成功率", "(成功×1.0 + 部分×0.5) ÷ n。所有页共用一个口径，跨类型、跨厂商都可比"),
    ("成功", "拿到了这一页的实质内容：非空、确实是这条 URL 的内容、不是乱码、"
             "不是把验证页当正文；有参考词表时命中 ≥ 30%"),
    ("部分", "拿到一部分：付费墙前的摘要、片段、只渲染出一半的动态页"),
    ("失败", "被拦 / 空 / 报错 / 抓成了别的页 / 乱码"),
    ("n", "判得动的格数，也是成功率的分母"),
    ("判不了", "我们没能做出判定。**不当 0 分** —— 既不进分子也不进分母，单独报"),
    ("按域去重", "同一网站的多页先在域内取平均再进总分（100 页来自 87 个站，13 行同域）"),
]

DIAG_COLS = [
    ("dishonest", "**把验证页 / 错误页 / 别的页当正文返回的次数。**诚实的失败下游知道要重试，"
                  "脏数据下游不知道 —— 本轮最值得单独看的一个数"),
    ("P50 / P90", "一次调用的中位与 90 分位耗时，**只统计抓到了内容的调用**。"
                  "**跨家不可比** —— 本轮给 octen 设了 2.5 秒/请求的节流"),
    ("抓错页 / 乱码", "两类硬失败：独有关键词一个都对不上 / 文本是乱码或二进制。"
                     "直接判失败，不送面板复议"),
    ("疑似绕墙", "付费墙页上拿到疑似完整正文的次数。**标记但不加分** —— "
               "把拿到墙后内容算作更好，等于奖励绕墙"),
    ("慢失败", "失败且耗时 ≥ 10 秒 —— 又慢又没拿到"),
    ("长度中位", "抓回内容的中位长度。同家跨轮可比，**跨家仅作参考**："
               "各家正文剥离口径不同，长不等于好"),
    ("三方分歧", "面板三家各执一词的格数。这个数大说明那些格本身就难判"),
]

FAULTS_DESC = [
    ("provider", "厂商侧：被拦、超时、限速、返回错误 —— 真实能力差异"),
    ("page", "页面侧：这一页本身就没有可提取的内容"),
    ("harness", "**我们自己的锅**：账户欠费、我们的长度上限、我们的解析器崩了。"
                "单列一栏，不记到厂商头上"),
]


def _traps(agg: dict) -> list[tuple[str, str]]:
    m = agg["meta"]
    low = [k for sl in agg["slices"].values() for k, b in sl.items()
           if isinstance(b, dict) and b.get("_gap_share", 0) >= LOW_CONFIDENCE]
    out = []
    if low:
        out.append(("带 ⚠ 的桶：高分不等于能力强",
                    "那些页我们自己的浏览器也拿不到参考内容，面板只能凭抓取结果自己判，"
                    "而那种情况下它偏松。于是会出现「越难的档分越高」的倒挂 —— "
                    "**那是测量置信度的假象，不是能力差异**。"))
    out.append(("延迟不能横向比",
                "本轮对 octen 设了 2.5 秒/请求的节流（它在更快的节奏下会拒绝请求），"
                "它的延迟里含我们主动等待的时间。要比速度需要另跑一轮不设节流的测试。"))
    out.append(("成功率高 + dishonest 高，比成功率略低更该警惕",
                "成功率只看「拿到没有」。一家成功率不错但 dishonest 明显偏高，"
                "意味着它失败时倾向于返回一段**看起来像内容的东西**，而下游分辨不出来。"
                "接入前把这两个数放在一起看。"))
    if m["verdicts_on_gt_gap_pages"]:
        out.append(("GT 缺口页上的判定，证据强度更低",
                    "%d 个页面我们自己也抓不到、无法建立参考答案；本轮 %d 格判定落在这些页上，"
                    "完全依赖面板的主观判断。方法学声明里列出了这些页面的名字，"
                    "结论对它们敏感时应逐条人工核。"
                    % (len(m["gt_gaps"]), m["verdicts_on_gt_gap_pages"])))
    return out


def render_glossary_md(agg: dict) -> list[str]:
    L = ["", "## 口径速查", "",
         "**范围**：本轮只回答「这一页抓到了没有」。不评抓得干不干净、结构全不全 —— "
         "那属于解析质量，不在范围内。", "",
         "### 主表各列", "", "| 列 | 口径 |", "|---|---|"]
    L += ["| %s | %s |" % kv for kv in MAIN_COLS]
    L += ["", "### 判定怎么做出来的", "",
          "两层。**先机械判**：内容命中率、是不是这条 URL 的内容、有没有乱码、"
          "动态页里 JS 执行后才出现的内容来了没有 —— 参考词表来自我们自己把这 100 页"
          "各渲染一遍（导航栏、页脚这些骨架词已剔除）。**拿不准的交面板**：三个不同厂商的"
          "模型盲判、多数决，各自看不到机械层结论、也看不到别家和别的模型的判定；"
          "三方各执一词时不硬选。机械层**确定**的结论（抓错页 / 乱码 / 传输失败）不送复议。",
          "", "### 诊断列（不进分数）", "", "| 列 | 口径 |", "|---|---|"]
    L += ["| %s | %s |" % kv for kv in DIAG_COLS]
    L += ["", "### 失败归因的责任方", "", "| 责任方 | 意思 |", "|---|---|"]
    L += ["| `%s` | %s |" % kv for kv in FAULTS_DESC]
    L += ["", "### 别读错的几个地方", ""]
    for i, (h, body) in enumerate(_traps(agg), 1):
        L.append("%d. **%s** —— %s" % (i, h, body))
    return L


def _fmt(x, pct=True):
    if x is None:
        return UNLABELLED
    return "%.0f%%" % (x * 100) if pct else "%.0f" % x


LOW_CONFIDENCE = 0.5      # 一桶里过半判定落在 GT 缺口页上 -> 标低置信


def _slice_table(title: str, buckets: dict, providers: list, labels: dict | None = None):
    if not buckets:
        return []
    keys = sorted(buckets)
    heads = []
    for k in keys:
        mark = " ⚠" if buckets[k]["_gap_share"] >= LOW_CONFIDENCE else ""
        heads.append("%s(%d)%s" % ((labels or {}).get(k, k), buckets[k]["_n_pages"], mark))
    # 每一列**列内**排名（并列同名次）。行序按总分，所以名次要标在格子里 ——
    # 换成按列排行序的话，五张表的行序各不相同，读者对不上号。
    rk = {k: rank_of({p: buckets[k][p]["weighted"] for p in providers}, True) for k in keys}
    L = ["", "### %s" % title, "",
         "| provider | " + " | ".join(heads) + " |",
         "|---" * (len(keys) + 1) + "|"]
    for p in providers:
        L.append("| %s | %s |" % (p, " | ".join(
            _fmt(buckets[k][p]["weighted"]) + _rk_md(rk[k].get(p)) for k in keys)))
    L += ["", "*上标是该列的名次（并列同名次）。*"]
    low = [((labels or {}).get(k, k), buckets[k]["_gap_share"]) for k in keys
           if buckets[k]["_gap_share"] >= LOW_CONFIDENCE]
    if low:
        L += ["", "> ⚠ **低置信**：%s —— 这些桶里的判定大多落在 GT 缺口页上"
              "（我们自己的浏览器也拿不到参考），面板只能凭抓取内容自己判，"
              "而那种情况下它偏松。**不要把这些格子的高分读成能力更强。**"
              % "、".join("%s %.0f%%" % (n, v * 100) for n, v in low)]
    return L


def render_markdown(agg: dict) -> str:
    m = agg["meta"]
    L = ["# Fetch Provider 抓取能力评测", "",
         "**%s**" % agg["scope"], "",
         "页面 %d · 判定格 %d · %d 家" % (m["n_pages"], m["n_cells"], len(agg["providers"])),
         "", "## 主表：抓取成功率", "",
         "成功计 1.0、部分计 0.5。`n` 是**判得动的格数** —— 判不了的不进分母也不当 0 分。",
         "", "| # | provider | 抓取成功率 | 成功 | 部分 | 失败 | n | 判不了 | 按域去重 |",
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

    L += ["", "## 切片"]
    pr = agg["providers_ranked"]
    L += _slice_table("按页面类型", agg["slices"]["type"], pr, TYPE_LABEL)
    L += _slice_table("反爬页按墙的类型", agg["slices"]["antibot_subclass"], pr, SUBCLASS_LABEL)
    L += _slice_table("反爬页按防护强度", agg["slices"]["strength"], pr, STRENGTH_LABEL)
    L += _slice_table("文档文件按格式", agg["slices"]["doc_type"], pr)
    L += _slice_table("健壮性探针", agg["slices"]["probes"], pr)

    fail_n = {p: sum(agg["failures"][p].values()) for p in agg["providers"]}
    fr = rank_of(fail_n, False)                  # 失败越少越好
    hr = rank_of(agg["harness_faults"], False)   # 我们自己的锅也越少越好
    L += ["", "## 为什么没抓到", "",
          "指标只看成败时，失败归因是第二主角。`harness` = 我们自己的锅，单独列。"
          "**两列都是越少越好**，上标是名次。", "",
          "| provider | 失败总数 | harness | 明细 |", "|---|--:|--:|---|"]
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
    L += ["", "## 诊断列（不进分数）", "",
          "上标是该列名次。**↑ = 越大越好 · ↓ = 越小越好 · — = 这一列不排名**（见表下说明）。",
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
    L += ["", "**不排名的列**：" + "；".join(
        "`%s` —— %s" % (n, NO_RANK_WHY[f]) for n, f, _ in DIAG_COLS_SPEC
        if DIAG_DIRECTION.get(f) is None) + "。硬给它们排名会把「仅供参考」读成「越大越好」。"]

    L += [""]
    L += render_glossary_md(agg)
    L += ["", "## 方法学声明", "",
          "- **只评抓取能力**：主口径是「这一页抓到了没有」。不评正文纯度、结构保真、"
          "截断完整度 —— 那三项是解析质量，已从代码里删除。",
          "- 反爬页的「过」按墙的类型定义：WAF = 拿到正文；登录墙 = 拿到墙前内容**且标明是墙**；"
          "付费墙 = 拿到免费可见部分。**拿到墙后内容不加分**，标 `suspicious_bypass`。",
          "- 防护强度档（两条 GT 通道比对得出）：%s" % (m["strength_counts"] or UNLABELLED)]
    if m["gt_gaps"]:
        L.append("- **GT 缺口 %d 条** —— 判定器跳过其词表，面板改为就抓取内容本身判断：%s"
                 % (len(m["gt_gaps"]),
                    "、".join("%s/%s(%s)" % (g["pid"], g["host"], g["why"])
                              for g in m["gt_gaps"])))
    L.append("- 判不了的格共 %d（如实留空，不当 0 分）" % m["unjudged_total"])
    if m["verdicts_on_gt_gap_pages"]:
        L.append("- **%d 格的判定落在 GT 缺口页上** —— 面板没有参考渲染、凭抓取内容自己判，"
                 "证据强度低于有 GT 的页" % m["verdicts_on_gt_gap_pages"])
    L.append("- 延迟只统计抓到了内容的调用；长度中位数同家跨轮可比，跨家仅供参考")
    cp = agg["meta"].get("cache_pinned") or {}
    if cp:
        L.append("- **各家的「实时抓、不走缓存」开关已显式设上**：%s。"
                 "标 `no_knob` 的家官方 API 没有这个参数；标 `unpinned` 的是我们还没接线的家。"
                 % " · ".join("%s=%s" % kv for kv in sorted(cp.items())))
    L.append("- **本轮仍不测内容新鲜度。** 开关只保证这一次去实抓了，不保证各家抓到的是"
             "同一时刻的页面。需要实时性的场景要另做一轮时效测试。")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════
# artifact 页
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
    warn = ('<p class="fx-note">&#9888; <strong>低置信</strong>：%s —— 这些桶里的判定大多'
            '落在 GT 缺口页上（我们自己的浏览器也拿不到参考），面板只能凭抓取内容自己判，'
            '而那种情况下它偏松。<strong>不要把这些格子的高分读成能力更强。</strong></p>'
            % "、".join("%s %.0f%%" % (n, v * 100) for n, v in low)) if low else ""
    return ('<div class="fx-sec">%s</div>%s<div class="fx-scroll"><table class="fx-t">'
            '<thead><tr><th>provider</th>%s</tr></thead><tbody>%s</tbody></table></div>'
            % (title, warn, head, "".join(rows)))


def _kv_table(pairs) -> str:
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, _md_bold(v)) for k, v in pairs)
    return ('<div class="fx-scroll"><table class="fx-t fx-kv"><thead><tr><th>列</th>'
            '<th>口径</th></tr></thead><tbody>%s</tbody></table></div>' % rows)


def _md_bold(t: str) -> str:
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def render_glossary_html(agg: dict) -> str:
    H = ['<div class="fx-sec">口径速查</div>',
         '<p class="fx-note"><strong>范围</strong>：本轮只回答「这一页抓到了没有」。'
         '不评抓得干不干净、结构全不全 —— 那属于解析质量，不在范围内。</p>',
         '<div class="fx-sub2">主表各列</div>', _kv_table(MAIN_COLS),
         '<div class="fx-sub2">判定怎么做出来的</div>',
         '<p class="fx-note">两层。<strong>先机械判</strong>：内容命中率、是不是这条 URL 的内容、'
         '有没有乱码、动态页里 JS 执行后才出现的内容来了没有 —— 参考词表来自我们自己把这 100 页'
         '各渲染一遍（导航栏、页脚这些骨架词已剔除）。<strong>拿不准的交面板</strong>：'
         '三个不同厂商的模型盲判、多数决，各自看不到机械层结论、也看不到别家和别的模型的判定；'
         '三方各执一词时不硬选。机械层<strong>确定</strong>的结论（抓错页 / 乱码 / 传输失败）'
         '不送复议。</p>',
         '<div class="fx-sub2">诊断列（不进分数）</div>', _kv_table(DIAG_COLS),
         '<div class="fx-sub2">失败归因的责任方</div>',
         _kv_table([("<code>%s</code>" % k, v) for k, v in FAULTS_DESC]),
         '<div class="fx-sub2">别读错的几个地方</div>']
    items = "".join("<li><strong>%s</strong> —— %s</li>" % (h, _md_bold(b))
                    for h, b in _traps(agg))
    H.append('<div class="fx-card"><ol class="fx-traps">%s</ol></div>' % items)
    return "".join(H)


def render_html(agg: dict, title: str = "Fetch 百页抓取实测") -> str:
    """artifact 页。按 playbook §10：数据作 JS 字面量嵌入；类名一律 `fx-` 前缀，
    不撞共享样式；`th,td` 上的属性逐个显式重申。"""
    m = agg["meta"]
    H = ["<title>%s</title>" % title, "<style>%s</style>" % _CSS,
         '<div class="fx-wrap">', '<h1 class="fx-h1">%s</h1>' % title,
         '<p class="fx-sub">%d 页 · %d 判定格 · %d 家</p>'
         % (m["n_pages"], m["n_cells"], len(agg["providers"])),
         '<div class="fx-scope">%s</div>' % agg["scope"]]

    H.append('<div class="fx-sec">抓取成功率</div>')
    H.append('<p class="fx-note">成功计 1.0、部分计 0.5。<code>n</code> 是判得动的格数 —— '
             '判不了的不进分母也不当 0 分。</p>')
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
             '<th>provider</th><th>抓取成功率</th><th>成功</th><th>部分</th><th>失败</th>'
             '<th>n</th><th>判不了</th><th>按域去重</th></tr></thead><tbody>%s</tbody>'
             '</table></div>' % "".join(body))

    pr = agg["providers_ranked"]
    H.append(_sl_html("按页面类型", agg["slices"]["type"], pr, TYPE_LABEL))
    H.append(_sl_html("反爬页按墙的类型", agg["slices"]["antibot_subclass"], pr, SUBCLASS_LABEL))
    H.append(_sl_html("反爬页按防护强度", agg["slices"]["strength"], pr, STRENGTH_LABEL))
    H.append(_sl_html("文档文件按格式", agg["slices"]["doc_type"], pr))
    H.append(_sl_html("健壮性探针", agg["slices"]["probes"], pr))

    H.append('<div class="fx-sec">为什么没抓到</div>')
    H.append('<p class="fx-note">指标只看成败时，失败归因是第二主角。'
             '<code>harness</code> = 我们自己的锅，单独列。<strong>两列都是越少越好</strong>，'
             '上标是名次。</p>')
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
             '<th>失败总数 &darr;</th><th>harness &darr;</th><th>明细</th></tr></thead>'
             '<tbody>%s</tbody></table></div>' % "".join(frows))

    H.append('<div class="fx-sec">诊断列（不进分数）</div>')
    H.append('<p class="fx-note">上标是该列名次。<strong>&uarr; 越大越好 · &darr; 越小越好 · '
             '&mdash; 这一列不排名</strong>（原因见表下）。</p>')
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
    H.append('<p class="fx-note"><strong>不排名的列</strong>：%s。'
             '硬给它们排名会把「仅供参考」读成「越大越好」。</p>'
             % "；".join("<code>%s</code> —— %s" % (n, NO_RANK_WHY[f])
                         for n, f, _ in DIAG_COLS_SPEC if DIAG_DIRECTION.get(f) is None))

    H.append(render_glossary_html(agg))

    H.append('<div class="fx-sec">方法学声明</div>')
    notes = ["<li><strong>只评抓取能力</strong>：主口径是「这一页抓到了没有」。不评正文纯度、"
             "结构保真、截断完整度 —— 那三项是解析质量，已从代码里删除。</li>",
             "<li>反爬页的「过」按墙的类型定义：WAF = 拿到正文；登录墙 = 拿到墙前内容"
             "<strong>且标明是墙</strong>；付费墙 = 拿到免费可见部分。"
             "<strong>拿到墙后内容不加分</strong>。</li>",
             "<li>防护强度档（两条 GT 通道比对得出）：%s</li>"
             % (m["strength_counts"] or UNLABELLED),
            ]
    if m["gt_gaps"]:
        notes.append("<li><strong>GT 缺口 %d 条</strong> —— 判定器跳过其词表，面板改为就"
                     "抓取内容本身判断：%s</li>"
                     % (len(m["gt_gaps"]),
                        "、".join("%s/%s" % (g["pid"], g["host"]) for g in m["gt_gaps"])))
    cp = m.get("cache_pinned") or {}
    if cp:
        notes.append("<li><strong>各家的「实时抓、不走缓存」开关已显式设上</strong>：%s。"
                     "<code>no_knob</code> = 官方 API 没有这个参数；"
                     "<code>unpinned</code> = 我们还没接线的家。</li>"
                     % " · ".join("%s=%s" % kv for kv in sorted(cp.items())))
    notes.append("<li><strong>本轮仍不测内容新鲜度</strong> —— 开关只保证这一次去实抓了，"
                 "不保证各家抓到的是同一时刻的页面。需要实时性的场景要另做一轮时效测试。</li>")
    notes.append("<li>判不了的格共 %d —— 如实留空，不当 0 分</li>" % m["unjudged_total"])
    if m["verdicts_on_gt_gap_pages"]:
        notes.append("<li><strong>%d 格的判定落在 GT 缺口页上</strong> —— 面板没有参考渲染、"
                     "凭抓取内容自己判，证据强度低于有 GT 的页</li>"
                     % m["verdicts_on_gt_gap_pages"])
    H.append('<div class="fx-card"><ul>%s</ul></div>' % "".join(notes))

    # 数据作 JS 字面量嵌入 —— `<script type="application/json">` 发布后不保留（§10.2）
    H.append("<script>window.FETCH_EVAL_DATA = %s;</script>"
             % json.dumps(agg, ensure_ascii=False))
    H.append("</div>")
    html = "\n".join(H)
    _assert_table_columns(html)
    return html


def _assert_table_columns(html: str) -> None:
    """列数四方一致断言（playbook §8）。表头列数 != 数据行列数 = 报告在撒谎。"""
    import re as _re
    for tbl in _re.findall(r"<table.*?</table>", html, _re.S):
        heads = _re.findall(r"<thead>(.*?)</thead>", tbl, _re.S)
        bodies = _re.findall(r"<tbody>(.*?)</tbody>", tbl, _re.S)
        if not heads or not bodies:
            continue
        n_head = heads[0].count("<th")
        for tr in _re.findall(r"<tr>(.*?)</tr>", bodies[0], _re.S):
            n = tr.count("<td")
            assert n == n_head, "表格列数不一致：表头 %d，数据行 %d" % (n_head, n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--out-html")
    a = ap.parse_args()
    agg = aggregate(load_jsonl(Path(a.verdicts)), load_jsonl(Path(a.pageset)))
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
        print("HTML -> %s（%.2f MB，artifact 上限 2.44 MB）"
              % (a.out_html, len(html.encode()) / 1e6))


if __name__ == "__main__":
    main()
