"""抓取成败判定 + 三模型面板。

**本轮只评抓取能力。** 主口径只回答一个问题：**这一页抓到了没有。**
不评正文纯度、不评结构保真、不评截断完整度 —— 那三项是解析质量，已从代码里删除
（留着不评的指标躺在报告里，读的人会以为它们进了评价）。

因为只剩一个单位，跨型可比，**总分成立**；五个 type 从"各有各的主指标"降级成切片轴。

判定分两层，严格分开（playbook §3.1）：
  机械层  纯函数算出来的档位。可复现、可核到具体证据。
  面板层  只处理**非 clean-pass** 的格子。三 family 三模型盲判，多数决，
          三方分歧保留机械判定。裁决按指纹缓存，重跑不重复烧 token（§3.2）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock

from . import fetch_checks as C
from .fetch_spec import TH

RUBRIC_VERSION = "fetch-v3-20260901"   # v3：只评抓取成败
PANEL_TEXT_BUDGET = 6000

_PASS, _PARTIAL, _LOST = "pass", "partial", "lost"
WEIGHT = {_PASS: 1.0, _PARTIAL: 0.5, _LOST: 0.0}


def _v(verdict, reason, **kw) -> dict:
    """`final=True` 表示机械层的判定是终局的，不送面板复判。"""
    return {"verdict": verdict, "reason": reason, "needs_panel": False, "final": False,
            "dishonest": False, "suspicious_bypass": False, **kw}


# 缺口页上仍可用的字段：**只有身份类的**。内容词表是被拦时抓到的验证页文案，
# 拿它当参考会把真的抓到了内容的家判成 lost（w3.org 实测）。
_WEAK_KEYS = ("anchors", "anchor_source", "serp_title", "serp_snippet", "title")


def effective_gt(page: dict) -> dict:
    """能用于判定的 GT。判定器与面板都必须走这一个口子。

    缺口页分两种：
      有中立 SERP 的弱锚点  只留身份类字段（`anchors` 等），**不留内容词表** ——
                            身份能判（"返回的是不是这一页"），完整度判不了。
      连弱锚点也没有        返回空，只能交给跨家交叉判。
    """
    gt = page.get("gt") or {}
    if not gt.get("gt_gap"):
        return gt
    if gt.get("anchor_source") == "serp" and gt.get("anchors"):
        return {k: gt[k] for k in _WEAK_KEYS if k in gt}
    return {}


def run_checks(page: dict, resp: dict) -> dict:
    """这一格能算的机械检查。GT 缺的项返回 None（"无定义"），不是 0。"""
    gt = effective_gt(page)
    text = resp.get("text") or ""
    return {
        "coverage": C.coverage(text, gt.get("vocab")),
        "render_hit": C.render_hit(text, gt.get("render_anchors")),
        "identity_ok": C.identity_ok(text, gt.get("anchors")),
        "encoding_ok": C.encoding_ok(text),
        "len_norm": C.len_norm(text),
        "wall_hit": C.wall_hit(text),
        "degenerate": bool(gt.get("degenerate")),
    }


def _band(value: float | None, ok: float, lost: float) -> str | None:
    if value is None:
        return None
    if value >= ok:
        return _PASS
    if value < lost:
        return _LOST
    return _PARTIAL


def _verdict_expect_error(page: dict, resp: dict, ch: dict) -> dict:
    """`expect == "error"` 的页（404 / 503）：**正确行为是干净报错。**

    把错误页的页面体当正文返回是错的 —— 下游拿到一段 "Not Found" 当内容用，而且
    不知道自己拿错了。指标只看成败时，这类"没抓到却声称抓到了"是最该被看见的一档。
    """
    if resp.get("status") == "error":
        return _v(_PASS, "clean_error", final=True)
    text = (resp.get("text") or "").lower()
    if any(k in text for k in ("404", "not found", "503", "unavailable", "error")):
        return _v(_LOST, "error_page_returned_as_content", dishonest=True)
    return _v(_PARTIAL, "content_returned_for_an_error_url", needs_panel=True)


def _verdict_expect_redirect(page: dict, resp: dict, ch: dict) -> dict:
    if resp.get("status") == "error":
        return _v(_LOST, "did_not_follow_redirect", final=True)
    if ch["coverage"] is None:
        return _v(None, "no_gt_for_redirect_target", needs_panel=True)
    return _v(_band(ch["coverage"], TH["fetch_ok"], TH["fetch_lost"]), "redirect_target")


def mechanical_verdict(page: dict, resp: dict, ch: dict) -> dict:
    """抓到了没有。返回 `verdict=None` 表示机械层判不了，交面板。"""
    expect = page.get("expect", "content")
    if expect == "error":
        return _verdict_expect_error(page, resp, ch)
    if expect == "redirect_final":
        return _verdict_expect_redirect(page, resp, ch)

    if resp.get("status") == "error":
        # 传输失败是**终局**：没有文本，面板无从判起，送过去只是烧钱。
        return _v(_LOST, "fetch_failed:%s" % (resp.get("failure_reason") or "error"),
                  final=True)

    # ── 两条硬否决：在"抓到了没有"这个问题下，它们都等于没抓到 ──────────────
    # 这两条是**终局**（`final=True`）：证据是机械的、确定的，不送面板复判。
    # 送了会被推翻 —— 实测 readability 返回 PDF 原始字节（encoding_ok=False），
    # 面板只看到 6000 字符预览里的 `%PDF-1.4 ... obj ... stream`，判成"内容来了"。
    if ch["identity_ok"] is False:
        # 返回的不是这条 URL 的内容。它在覆盖率口径下能蒙过去，比失败更坏 ——
        # 下游发现不了。
        #
        # **弱锚点（来自 SERP 标题/摘要）判出来的不同一不是终局**：搜索引擎的标题
        # 可能已经过时，证据强度不足以支撑一个不可申诉的判定，交面板复核。
        weak = (page.get("gt") or {}).get("anchor_source") == "serp"
        return _v(_LOST, "wrong_page" + ("_weak" if weak else ""),
                  dishonest=not weak, final=not weak, needs_panel=weak)
    if ch["encoding_ok"] is False:
        return _v(_LOST, "mojibake", final=True)

    # ── 反爬页：三小类"过"的定义不同，机械层给不出，交面板 ──────────────────
    if page["type"] == "antibot":
        sus = (page.get("antibot_subclass") == "paywall" and ch["len_norm"] > 1500)
        return _v(None, "needs_wall_judgement", needs_panel=True, suspicious_bypass=sus)

    # ── SPA：拿到服务端骨架不算抓到这一页 ────────────────────────────────
    if page["type"] == "render":
        if ch["render_hit"] is None:
            # 没有渲染锚点就分不出"骨架"和"内容" —— coverage 会把壳判成 pass，
            # 所以这里必须交面板，不能退回通用闸门。
            return _v(None, "no_render_anchors", needs_panel=True)
        return _v(_band(ch["render_hit"], TH["render_ok"], TH["fetch_lost"]),
                  "render_anchors")

    # ── 其余：coverage 作成功闸门（低阈值，只区分"真内容 / 空壳"）──────────
    if ch["degenerate"] or ch["coverage"] is None:
        return _v(None, "no_mechanical_basis", needs_panel=True)
    out = _v(_band(ch["coverage"], TH["fetch_ok"], TH["fetch_lost"]), "content_present")
    if out["verdict"] is None:
        out["needs_panel"] = True
    return out


# ── 面板层 ────────────────────────────────────────────────────────────────

_SHARED_ANCHOR = """The only question is whether this fetch GOT THE PAGE. Do not grade
formatting, tidiness, or how completely the page was captured.

  pass     the substantive content of this URL came through
  partial  some of it came through: a wall-front excerpt, a fragment, a partial render
  lost     nothing usable came through: blocked, empty, an error, the wrong page, or garbled

Raw unrendered payload returned verbatim -- HTML source, a PDF byte stream
(`%PDF-... obj ... stream`), base64 blobs -- is NOT content. That is `lost`, however
faithfully the bytes were transferred: the caller asked for the page, not the file.

A long, tidy extraction of the WRONG thing is `lost`, not `pass`. A short extraction of
the right thing is `pass` if the page itself is short. Missing sections, lost tables or
stripped formatting do NOT reduce the verdict -- we are not grading parsing this round."""

_TYPE_RUBRIC = {
    "content": """This is an ordinary page (static doc, document file, or an awkward input
like a huge body / odd content type / very thin page). Did its actual content come through?""",
    "render": """This page builds its content with JavaScript. An extraction holding only
the server-rendered shell (nav, footer, boilerplate, a loading state) did NOT get the page,
however long it is -- that is `lost`.""",
    "antibot": """This page is defended. What counts as getting through depends on the wall:
  waf         pass = the actual page body came through
  login_wall  pass = the publicly visible portion came through AND the extraction makes it
              evident that a wall exists. Content that could only come from behind a login
              is NOT a better result -- flag it.
  paywall     pass = the free portion came through. Full article text from behind a paywall
              is NOT a better result -- flag it.
An extraction that returns the wall page itself (challenge screen, login prompt, subscribe
prompt) AS IF it were the content is `lost`, and is worse than an honest failure:
downstream cannot tell it went wrong.""",
}

_RUBRIC_FOR = {"antibot": "antibot", "render": "render"}


def rubric_key(page_type: str) -> str:
    return _RUBRIC_FOR.get(page_type, "content")


def panel_system(page_type: str) -> str:
    """判定标准。共享锚点逐字一致；只按"过的定义真的不同"分岔（playbook §4.1）。

    分三份而不是五份：静态文档、文档文件、健壮性边缘 case 问的是同一个问题
    （内容来了没有），没有必要各写一份 —— 分岔多一份就多一处会漂移的措辞。
    """
    return ("You are grading one web fetch. We are measuring FETCH CAPABILITY only.\n\n"
            + _SHARED_ANCHOR + "\n\n" + _TYPE_RUBRIC[rubric_key(page_type)]
            + '\n\nOutput JSON only: {"verdict": "pass"|"partial"|"lost", '
              '"dishonest": true|false, "why": "<one short clause>"}\n'
              '"dishonest" is true only when the extraction presents a wall page, an error '
              'page, or a different page AS IF it were the requested content.')


def panel_user(page: dict, resp: dict, ch: dict) -> str:
    gt = effective_gt(page)
    text = (resp.get("text") or "")[:PANEL_TEXT_BUDGET]
    parts = [
        "URL: %s" % page.get("url", ""),
        "Page title (from our own render): %s" % (gt.get("title") or "unknown"),
    ]
    if page.get("antibot_subclass"):
        parts.append("Wall type: %s" % page["antibot_subclass"])
    if gt.get("vocab"):
        parts.append("Terms our reference render found in the page body: %s"
                     % ", ".join(gt["vocab"][:40]))
    elif gt.get("serp_title"):
        # 没有全文参考时，中立搜索引擎的标题/摘要是唯一的身份线索 —— 必须给面板，
        # 否则它只能凭"看起来像不像网页内容"判，而那偏松。
        parts.append("We could not fetch this page ourselves, so there is no reference "
                     "body text. What a neutral search engine has indexed for this exact "
                     "URL:\n  title:   %s\n  snippet: %s"
                     % (gt["serp_title"], gt.get("serp_snippet") or "(none)"))
        parts.append("Use that to decide whether this is the right page. It does not tell "
                     "you how complete the extraction is, and completeness is not graded.")
    else:
        parts.append("We have NO reference for this page (our own browser was blocked and "
                     "search engines have not indexed it). Judge the extraction on its own "
                     "terms: does it look like the substantive content of this URL?")
    parts.append("\n--- FETCH UNDER TEST (%d chars, truncated for review) ---\n%s"
                 % (len(resp.get("text") or ""), text))
    return "\n".join(parts)


def judge_cache_key(page: dict, resp: dict, model: str, user: str = "") -> str:
    """指纹 = 页 + 家 + 抓取正文 + **面板实际看到的 user 内容** + 标准版本 + 模型。

    `user` 必须进指纹：GT 修好之后重跑，键不含它的话会把旧的错判从缓存里取回来。
    """
    h = hashlib.sha256()
    for part in (page.get("pid", ""), resp.get("provider", ""),
                 resp.get("text") or "", user, RUBRIC_VERSION, model):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class PanelCache:
    """裁决缓存。逐条落盘，重跑不重复烧 token（playbook §3.2）。**并发安全**。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = Lock()
        self.mem: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").split("\n"):
                try:
                    r = json.loads(line)
                    self.mem[r["key"]] = r["value"]
                except Exception:                # noqa: BLE001
                    continue

    def get(self, key: str):
        return self.mem.get(key)

    def put(self, key: str, value: dict) -> None:
        with self.lock:
            self.mem[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "value": value},
                                   ensure_ascii=False) + "\n")


def panel_verdict(page: dict, resp: dict, ch: dict, panel: list[str], *,
                  cache: PanelCache | None = None, max_tokens: int = 500) -> dict:
    """三 family 三模型盲判。**不给机械判定、不给别家的判定。**"""
    from .llm import call_llm_json
    system = panel_system(page["type"])
    user = panel_user(page, resp, ch)
    votes: dict[str, dict] = {}
    for model in panel:
        key = judge_cache_key(page, resp, model, user)
        hit = cache.get(key) if cache else None
        if hit is not None:
            votes[model] = hit
            continue
        try:
            v = call_llm_json(system, user, model=model, max_tokens=max_tokens,
                              temperature=0.0)
            v = {"verdict": (v.get("verdict") or "").strip(),
                 "dishonest": bool(v.get("dishonest")),
                 "why": (v.get("why") or "")[:200]}
        except Exception as e:                   # noqa: BLE001
            v = {"verdict": "error", "dishonest": False,
                 "why": "%s: %s" % (type(e).__name__, str(e)[:120])}
        votes[model] = v
        if cache and v["verdict"] != "error":
            cache.put(key, v)
    return aggregate_votes(votes)


def aggregate_votes(votes: dict[str, dict]) -> dict:
    """多数决。**三方各执一词时不硬选**，返回 split=True 由调用方保留机械判定。"""
    valid = [v["verdict"] for v in votes.values() if v.get("verdict") in WEIGHT]
    if not valid:
        return {"verdict": None, "panel_split": True, "dishonest": False, "votes": votes}
    counts = {v: valid.count(v) for v in set(valid)}
    top = max(counts, key=lambda k: counts[k])
    if counts[top] < 2:
        return {"verdict": None, "panel_split": True, "dishonest": False, "votes": votes}
    dis = sum(1 for v in votes.values() if v.get("dishonest")) >= 2
    return {"verdict": top, "panel_split": False, "dishonest": dis, "votes": votes}


class GoldStore:
    """人工核过的结论。**优先级最高**，覆盖机械层与面板。

    带 `text_sha` 守卫：抓取内容变了（换了一轮实跑），这一条金标自动失效 ——
    人工当时看的是那一份内容，换一份就不算数了。
    """

    def __init__(self, path: str | Path | None):
        self.by_key: dict[tuple[str, str], dict] = {}
        if not path:
            return
        p = Path(path)
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:                    # noqa: BLE001
                continue
            if r.get("verdict") in WEIGHT:
                self.by_key[(r["pid"], r["provider"])] = r

    def lookup(self, page: dict, resp: dict, provider: str | None = None) -> str | None:
        """`provider` 显式传入时以它为准 —— 交叉判是按字典键分厂商的，
        payload 里的 provider 字段万一对不上，就会取到别人的金标。"""
        r = self.by_key.get((page.get("pid"), provider or resp.get("provider")))
        if not r:
            return None
        want = r.get("text_sha")
        if want:
            got = hashlib.sha256((resp.get("text") or "")
                                 .encode("utf-8", "replace")).hexdigest()[:16]
            if got != want:
                return None                      # 内容变了，金标不再适用
        return r["verdict"]

    def __len__(self) -> int:
        return len(self.by_key)


def _apply_gold(rec: dict, page: dict, resp: dict, gold, provider: str | None = None) -> bool:
    v = gold.lookup(page, resp, provider) if gold else None
    if v is None:
        return False
    rec["verdict"] = v
    rec["reason"] = "human_gold"
    rec["dishonest"] = rec["dishonest"] and v == _LOST
    rec["panel"] = None
    rec["panel_split"] = False
    return True


def score_one(page: dict, resp: dict, panel: list[str] | None = None, *,
              cache: PanelCache | None = None, gold: "GoldStore | None" = None) -> dict:
    """一格的完整判定：机械层 -> 需要时面板 -> 合并。"""
    ch = run_checks(page, resp)
    mech = mechanical_verdict(page, resp, ch)
    out = {"pid": page["pid"], "provider": resp.get("provider"), "type": page["type"],
           "antibot_subclass": page.get("antibot_subclass"),
           "strength": (page.get("gt") or {}).get("strength"),
           "checks": ch, "mechanical": mech, "verdict": mech["verdict"],
           "dishonest": mech["dishonest"], "suspicious_bypass": mech["suspicious_bypass"],
           "reason": mech["reason"], "panel": None, "panel_split": False,
           "latency_ms": resp.get("latency_ms"), "len_norm": ch["len_norm"],
           "failure_reason": resp.get("failure_reason"), "fault": resp.get("fault"),
           "run_seq": resp.get("run_seq", 0)}
    if _apply_gold(out, page, resp, gold):
        return out                               # 人工结论优先，不再跑面板
    clean_pass = mech["verdict"] == _PASS and not mech["needs_panel"]
    if clean_pass or mech.get("final") or not panel:
        if out["verdict"] is None:
            # 没有面板又判不了：如实留空，**不当 0 分**（playbook §5.4）
            out["reason"] = mech["reason"] + "|unjudged"
        return out
    pv = panel_verdict(page, resp, ch, panel, cache=cache)
    out["panel"] = pv
    out["panel_split"] = pv["panel_split"]
    if pv["verdict"] is not None:
        out["verdict"] = pv["verdict"]
        out["dishonest"] = out["dishonest"] or pv["dishonest"]
        out["reason"] = "panel"
    return out


assert set(_TYPE_RUBRIC) == {"content", "render", "antibot"}
assert set(_RUBRIC_FOR.values()) <= set(_TYPE_RUBRIC)


# ══════════════════════════════════════════════════════════════════════════
# 跨家交叉判：给"没有参考答案"的页用
# ══════════════════════════════════════════════════════════════════════════

CROSS_TEXT_BUDGET = 2200          # 每家给面板看多少字符（要放得下 5 份）

CROSS_SYSTEM = """Several different services each tried to fetch the SAME web page.
You are shown what each returned. We could not fetch this page ourselves, so there is no
reference answer -- but you have the returns side by side, and that is the point: if any of
them got the real page, the ones that did not will stand out against it.

For each labelled return, decide:

  pass     this is the substantive content of that URL
  partial  part of it: a wall-front excerpt, a fragment, a partial render
  lost     not the page: a bot-check or login or subscribe screen, an error page, an empty
           shell, a DIFFERENT page (the site's home page, a listing, an onboarding screen),
           raw unrendered payload (HTML source, a `%PDF ... stream` byte dump), or garbled text

**Default to `lost`.** Only move a return up when there is positive evidence it is this
specific page's own content -- matching topic, matching entity, detail that could only come
from this URL. Length is not evidence. Tidiness is not evidence. Looking generally
web-like is not evidence.

We are grading FETCH CAPABILITY, not parsing: missing sections, lost tables and stripped
formatting do NOT lower a verdict.

Output JSON only, one entry per label you were shown:
{"verdicts": {"A": {"verdict": "pass|partial|lost", "dishonest": true|false,
                    "why": "<one short clause>"}, "B": {...}}}
"dishonest" is true when the return presents a wall, an error, or a different page AS IF it
were the requested content."""


def _cross_order(pid: str, providers: list[str]) -> list[str]:
    """标签顺序按 (pid, provider) 的散列定 —— **稳定但逐页不同**。

    固定顺序会让位置偏置一直落在同一家头上；随机顺序又不可复现。
    """
    return sorted(providers,
                  key=lambda p: hashlib.sha256((pid + "|" + p).encode()).hexdigest())


def cross_user(page: dict, resps: dict[str, dict]) -> tuple[str, dict[str, str]]:
    """返回 (user 文本, 标签 -> provider)。**厂商名字隐掉** —— 否则模型的品牌先验
    会漏进判定，而这些页恰恰是最需要它只看内容的地方。"""
    gt = page.get("gt") or {}
    order = _cross_order(page["pid"], sorted(resps))
    label_of = {}
    parts = ["URL: %s" % page.get("url", "")]
    if gt.get("serp_title"):
        parts.append("Title, from a neutral search engine's index: %s" % gt["serp_title"])
    if gt.get("serp_snippet"):
        parts.append("Search-engine snippet: %s" % gt["serp_snippet"])
    if page.get("antibot_subclass"):
        parts.append("This page is behind a %s." % page["antibot_subclass"])
    for i, prov in enumerate(order):
        label = chr(ord("A") + i)
        label_of[label] = prov
        r = resps[prov]
        if r.get("status") == "error":
            body = "(the service reported an error: %s)" % (r.get("failure_reason") or "error")
        else:
            body = (r.get("text") or "")[:CROSS_TEXT_BUDGET] or "(empty)"
        parts.append("\n--- RETURN %s (%d chars total) ---\n%s"
                     % (label, len(r.get("text") or ""), body))
    return "\n".join(parts), label_of


def cross_cache_key(page: dict, resps: dict[str, dict], model: str) -> str:
    h = hashlib.sha256()
    h.update((page.get("pid", "") + "|cross|" + RUBRIC_VERSION + "|" + model).encode())
    for prov in sorted(resps):
        h.update(prov.encode())
        h.update((resps[prov].get("text") or "")[:CROSS_TEXT_BUDGET].encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def panel_cross(page: dict, resps: dict[str, dict], panel: list[str], *,
                cache: PanelCache | None = None, max_tokens: int = 1200) -> dict:
    """一次调用判完这一页的所有家。返回 {provider: 合并后的判定}。

    比单家裸判**又准又便宜**：准是因为有横向对比（只要有一家真拿到了，其余的错就现形），
    便宜是因为 N 家从 N 次调用降到 1 次。
    """
    from .llm import call_llm_json
    user, label_of = cross_user(page, resps)
    per_model: dict[str, dict] = {}
    for model in panel:
        key = cross_cache_key(page, resps, model)
        hit = cache.get(key) if cache else None
        if hit is not None:
            per_model[model] = hit
            continue
        try:
            raw = call_llm_json(CROSS_SYSTEM, user, model=model,
                                max_tokens=max_tokens, temperature=0.0)
            got = raw.get("verdicts") or {}
            v = {label_of[k]: {"verdict": (val.get("verdict") or "").strip(),
                               "dishonest": bool(val.get("dishonest")),
                               "why": (val.get("why") or "")[:160]}
                 for k, val in got.items() if k in label_of}
        except Exception as e:                   # noqa: BLE001
            v = {p: {"verdict": "error", "dishonest": False,
                     "why": "%s: %s" % (type(e).__name__, str(e)[:100])} for p in resps}
        per_model[model] = v
        if cache and any(x["verdict"] != "error" for x in v.values()):
            cache.put(key, v)

    out = {}
    for prov in resps:
        votes = {m: per_model[m].get(prov, {"verdict": "error"}) for m in per_model}
        out[prov] = aggregate_votes(votes)
        out[prov]["mode"] = "cross"
    return out


def score_page_cross(page: dict, resps: dict[str, dict], panel: list[str] | None = None,
                     *, cache: PanelCache | None = None,
                     gold: "GoldStore | None" = None) -> dict[str, dict]:
    """整页一起判 —— 给**没有参考答案**的页用。

    机械层照常先跑（硬否决仍然是终局的）；剩下拿不准的格交跨家交叉判，一次调用判完全页。
    """
    out, pending = {}, {}
    for prov, resp in resps.items():
        ch = run_checks(page, resp)
        mech = mechanical_verdict(page, resp, ch)
        rec = {"pid": page["pid"], "provider": prov, "type": page["type"],
               "antibot_subclass": page.get("antibot_subclass"),
               "strength": (page.get("gt") or {}).get("strength"),
               "anchor_source": (page.get("gt") or {}).get("anchor_source"),
               "checks": ch, "mechanical": mech, "verdict": mech["verdict"],
               "dishonest": mech["dishonest"], "suspicious_bypass": mech["suspicious_bypass"],
               "reason": mech["reason"], "panel": None, "panel_split": False,
               "latency_ms": resp.get("latency_ms"), "len_norm": ch["len_norm"],
               "failure_reason": resp.get("failure_reason"), "fault": resp.get("fault"),
               "run_seq": resp.get("run_seq", 0)}
        if _apply_gold(rec, page, resp, gold, prov):
            out[prov] = rec
            continue                             # 人工结论优先，不进交叉判
        clean_pass = mech["verdict"] == _PASS and not mech["needs_panel"]
        if not (clean_pass or mech.get("final")) and panel:
            pending[prov] = resp
        elif rec["verdict"] is None:
            rec["reason"] = mech["reason"] + "|unjudged"
        out[prov] = rec

    if pending and panel:
        cross = panel_cross(page, pending, panel, cache=cache)
        for prov, pv in cross.items():
            rec = out[prov]
            rec["panel"] = pv
            rec["panel_split"] = pv["panel_split"]
            if pv["verdict"] is not None:
                rec["verdict"] = pv["verdict"]
                rec["dishonest"] = rec["dishonest"] or pv["dishonest"]
                rec["reason"] = "panel_cross"
            elif rec["verdict"] is None:
                rec["reason"] = rec["mechanical"]["reason"] + "|unjudged"
    return out
