"""Fetch 评测的声明式配置。**配比表只转写一次，且 import 期跑断言**（playbook §1.1）。

口径与运行手册：`docs/benchmarks/fetch_eval.md`。

两条容易被绕开的规矩写在这里：
  阈值集中在 `TH`  判定 prompt 里写死的数字必须与它一致，`fetch_score` 有双向断言（§4.3）。
  横切轴不是 type  反爬贯穿社交墙 + WAF 墙，多语言贯穿电商/防守/健壮性。把横切轴当 type
                   会把格子切碎（§1.3），所以它们是独立的切片轴而不是 TYPE_COUNTS 的成员。
"""
from __future__ import annotations

# ── 5 个 type（互斥，决定主指标）──────────────────────────────────────────
CATEGORY_TO_TYPE = {
    "Static Docs": "baseline",
    "E-commerce/SPA": "render",
    "Documents": "docfmt",
    "Social/Login": "antibot",
    "Defended (WAF/Reviews/Paywall)": "antibot",
    "Robustness": "reliability",
}
TYPE_COUNTS = {"baseline": 8, "render": 18, "docfmt": 22, "antibot": 42, "reliability": 10}

# ── 反爬型的三小类 ────────────────────────────────────────────────────────
# "过"的定义三者不同，混在一起平均没有意义（设计文档 §1.2）：
#   waf         过 = 拿到正文
#   login_wall  过 = 拿到墙前可见内容**并诚实标明这是墙**；不该期待墙后内容
#   paywall     过 = 拿到免费可见部分；拿到完整正文反而要标 suspicious_bypass
_LOGIN_WALL = ["x.com", "www.instagram.com", "www.linkedin.com", "www.tiktok.com",
               "www.facebook.com", "www.threads.com", "www.pinterest.com", "bsky.app"]
_PAYWALL = ["www.wsj.com", "www.bloomberg.com", "www.barrons.com", "www.nytimes.com",
            "www.sciencedirect.com", "medium.com"]
_WAF = ["stackoverflow.com", "math.stackexchange.com", "www.reddit.com", "www.quora.com",
        "www.g2.com", "www.trustpilot.com", "www.yelp.com", "www.tripadvisor.com",
        "www.glassdoor.com", "www.indeed.com", "www.crunchbase.com", "www.capterra.com",
        "www.goodreads.com", "www.zillow.com", "www.bbb.org", "www.sitejabber.com",
        "www.ticketmaster.com", "seatgeek.com", "www.producthunt.com", "www.wikihow.com",
        "www.reuters.com", "habr.com", "qiita.com", "www.leboncoin.fr", "www.idealo.de"]
ANTIBOT_SUBCLASS = {h: "login_wall" for h in _LOGIN_WALL}
ANTIBOT_SUBCLASS.update({h: "paywall" for h in _PAYWALL})
ANTIBOT_SUBCLASS.update({h: "waf" for h in _WAF})


# ── 横切轴：防护强度（仅反爬型内）。由 GT 双通道导出，不预先标 ──────────────
#   soft    headless 就过
#   medium  headless 被拦，真实 Chrome 过
#   hard    真实 Chrome 也被拦
#   unknown 真实 Chrome 通道没跑 —— **不能默认 hard**，那会把"没测"伪装成"最难"
STRENGTHS = frozenset({"soft", "medium", "hard", "unknown"})

# ── 逐页标签 ──────────────────────────────────────────────────────────────
EXPECT = frozenset({"content", "error", "redirect_final"})
PROBES = frozenset({"oversize", "url_quirk", "raw_direct", "redirect",
                    "empty_thin", "encoding", "plain_http"})

# ── 失败分类（9 类照参考报告）+ 归因三分（我们加的）──────────────────────────
# fault 三分是我们比参考报告多出来的一列：那份报告的头条发现正是"21 条失败是我们
# 自己的代码"，harness 那一档必须能单独报出来，否则会记到厂商头上。
FAILURE_REASONS = ("nothing_extractable", "anti_bot_blocked", "other", "our_size_cap",
                   "timeout_upstream", "rate_limited", "normalizer_crashed",
                   "content_type_or_404", "blocklisted_domain")
FAULTS = ("harness", "provider", "page")


# ── 阈值（唯一定义处）──────────────────────────────────────────────────────
# 本轮只评**抓取能力**，不评解析质量。所以阈值只回答一个问题：
# 拿到的是不是这一页的实质内容 —— 而不是"拿到了多少 / 拿得干不干净"。
TH = {
    # coverage 在这里是**成功闸门**，不是完整度分数：低阈值只用来区分
    # "拿到了真内容" 与 "拿到了空壳 / 墙页 / 别的页"。
    "fetch_ok": 0.3,
    "fetch_lost": 0.05,
    "render_ok": 0.4,       # SPA 只拿到服务端骨架 = 没拿到内容
    "vocab_min": 12,        # 低于此值不用机械阈值、直接走面板（退化页）
    "slow_loss_ms": 10_000,
}

# ── 逐页标签：URL 子串 -> 标签。**只在这里转写一次** ────────────────────────
# probes 是正交标签，可挂在任何 type 上；在 reliability 型里它们是主指标，
# 在其他型里是诊断列（设计文档 §1.5）。
PAGE_LABELS = {
    # expect != content 的三条：正确行为是干净报错 / 跟到终点
    "httpbingo.org/status/404": {"expect": "error", "probes": ["empty_thin"]},
    "httpbingo.org/status/503": {"expect": "error", "probes": ["empty_thin"]},
    "httpbingo.org/redirect/3": {"expect": "redirect_final", "probes": ["redirect"]},
    # 超大 body：静默截断在覆盖率口径下照样是 pass，只要词表落在前半
    "eur-lex.europa.eu/legal-content": {"probes": ["oversize"]},
    "w3.org/TR/html52/": {"probes": ["oversize"]},
    "arxiv.org/pdf/2005.14165.pdf": {"probes": ["oversize"]},
    "gutenberg.org/files/1342": {"probes": ["oversize", "raw_direct"]},
    # URL 特例：无 .pdf 后缀，返回的是 PDF 阅读器外壳而不是文件
    "arxiv.org/pdf/1706.03762": {"probes": ["url_quirk"]},
    # 非 HTML 直链：会不会硬塞进 HTML 渲染器
    "raw.githubusercontent.com": {"probes": ["raw_direct"]},
    "wordpress.org/sitemap.xml": {"probes": ["raw_direct"]},
    "feeds.bbci.co.uk/news/rss.xml": {"probes": ["raw_direct"]},
    "go.dev/blog/feed.atom": {"probes": ["raw_direct"]},
    "jsonplaceholder.typicode.com": {"probes": ["raw_direct"]},
    "api.github.com/repos": {"probes": ["raw_direct"]},
    # 空页 / 极薄页 / 目录列表：返回脏数据还是诚实报空
    "example.com/": {"probes": ["empty_thin"]},
    "textfiles.com/computers/": {"probes": ["empty_thin", "plain_http"]},
    # 编码
    "aozora.gr.jp": {"probes": ["encoding"]},
    # 非 TLS 上古 HTML
    "cs.cmu.edu/~rgs/alice-I.html": {"probes": ["plain_http"]},
}

_SUFFIX_DOC_TYPE = {
    ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".csv": "csv", ".md": "md", ".txt": "txt", ".atom": "atom",
}


def doc_type_from_url(url: str) -> tuple[str, str]:
    """返回 (doc_type, 规则名)。**URL 断不定的一律 unknown，交实测 content-type**。

    `arxiv.org/pdf/1706.03762` 没有后缀，猜成 pdf 就把"考嗅探"这道题做废了 —— 它考的
    恰恰是各家会不会把 PDF 阅读器外壳当成文件本身（设计文档 §1.6）。规则名逐条记录，
    事后能核是哪一级判出来的（playbook §9.3）。
    """
    low = url.lower().split("?")[0].split("#")[0]
    for suf, dt in _SUFFIX_DOC_TYPE.items():
        if low.endswith(suf):
            return dt, "suffix" + suf
    if low.endswith("rss.xml"):
        return "rss", "suffix_rss_xml"
    if low.endswith(".xml"):
        return "xml", "suffix_xml"
    if "api.github.com/" in low or "jsonplaceholder.typicode.com/" in low:
        return "json", "known_json_api"
    if "/pdf/" in low or "go.microsoft.com/fwlink" in low:
        return "unknown", "no_suffix_defer"
    return "html", "default_html"


assert all(set(v) <= {"expect", "probes"} for v in PAGE_LABELS.values())
assert all(p in PROBES for v in PAGE_LABELS.values() for p in v.get("probes", []))
assert all(v["expect"] in EXPECT for v in PAGE_LABELS.values() if "expect" in v)


_TYPES = frozenset(TYPE_COUNTS)


def assert_pageset(rows: list[dict]) -> None:
    """页面集的边际断言。**填格之前跑一次，冻结之前再跑一次**（playbook §3.4）。"""
    assert len(rows) == 100, f"页面集应为 100 条，实为 {len(rows)}"
    counts: dict[str, int] = {}
    for r in rows:
        assert r["type"] in _TYPES, f"未知 type：{r['type']}"
        assert r["expect"] in EXPECT, f"未知 expect：{r['expect']}"
        for p in r.get("probes") or []:
            assert p in PROBES, f"未知 probe：{p}"
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    assert counts == TYPE_COUNTS, f"type 计数不符：{counts} != {TYPE_COUNTS}"
    n_def = sum(1 for r in rows if r.get("defended"))
    assert n_def == 42, f"防守子集应为 42 条，实为 {n_def}"


# ── import 期自检：配置表自身的一致性 ───────────────────────────────────────
assert sum(TYPE_COUNTS.values()) == 100
assert set(CATEGORY_TO_TYPE.values()) <= _TYPES
assert len(_LOGIN_WALL) == 8, "Social/Login 十行分布在 8 个 host（x.com 与 linkedin 各两行）"
assert len(_PAYWALL) == 6
assert len(_WAF) == 25, "Defended 三十二行分布在 31 个 host（reddit 两行），减去 6 个付费墙"
assert len(ANTIBOT_SUBCLASS) == len(_LOGIN_WALL) + len(_PAYWALL) + len(_WAF), "小类清单有重复 host"
assert len(FAILURE_REASONS) == 9 and len(set(FAILURE_REASONS)) == 9
