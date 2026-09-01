"""13 家 fetch/extract provider 的 adapter。

**不放进 `src/backends.py`**：那边的抽象是 `search(query, k)`，这边是 `fetch(url)`，
焊在一起两边都会变形。

三条纪律：

  缺 key 硬失败      静默跳过会跑出"整轮零覆盖而报告显示零故障" —— 正是 69b778f 修过的坑。
  缓存新鲜度钉死     octen `max_age_seconds=300`（接口下限）、firecrawl `maxAge=0`。
                     不钉的话延迟那一列量的是各家缓存命中率的排名，不是抓取速度（playbook 5.8）。
  自家上限记 harness  参考报告的头条发现是"21 条失败是我们自己的代码"。`fault` 三分就是为
                     这件事加的：harness 的锅不能记到厂商头上。

**endpoint 未核实的 7 家**（parallel / context / you / linkup / zyte / cloudflare / apify）
只写骨架。凭印象编 endpoint 跑出来的是一整列假失败，比没有这一列更糟 —— 所以它们在缺 key 时
抛错并同时说明"口径待核"，拿到 key 后按官方文档填 `endpoint` / `_body` / `_pluck` 三处即可。
"""
from __future__ import annotations

import json
import os
import re
import time
from html import unescape as _html_unescape
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

from .backends import _load_dotenv
from .fetch_checks import len_norm as _len_norm

_load_dotenv()

# 远高于集合里最长的页（Gutenberg 全本约 70 万字符）。触发即记 harness 故障，不静默截断。
MAX_TEXT_CHARS = 5_000_000
DEFAULT_TIMEOUT = 60


@dataclass
class FetchResponse:
    """一次抓取的全部结果。字段名在 fetch_run 落盘、fetch_score 消费、fetch_report 聚合
    四处共用，改名要四处一起改。"""
    url: str
    provider: str
    status: str                      # "ok" | "error"
    text: str = ""
    len_norm: int = 0
    latency_ms: float = 0.0
    http_status: int | None = None
    error: str | None = None
    failure_reason: str | None = None    # 9 类之一；status=ok 时也可能有（如 our_size_cap）
    fault: str | None = None             # harness | provider | page
    cache_pinned: str = "unpinned"        # pinned | no_knob | unpinned
    raw_meta: dict = field(default_factory=dict)


# 厂商**主动拒绝服务某个域**的措辞。这和"目标站把它拦了"是两回事：
#   blocklisted_domain  它不给你抓这个域 —— **政策选择**
#   anti_bot_blocked    它试了，被目标站拦下 —— **能力差距**
# 采购时含义完全不同，都塞进 anti_bot_blocked 会让"不做这门生意"看起来像"打不过"。
# 2026-09-01 实测：firecrawl 全部 11 条 403 都是政策拒绝（"We do not support this
# site"），一条真实反爬失败都没有；换 proxy=stealth 也一样拒。
_POLICY_REFUSAL = re.compile(
    r"do not support this site|not supported|unsupported (site|domain|url)"
    r"|blocked by (our )?policy|domain is (not allowed|blocked|blacklisted)"
    r"|we (don't|do not) (allow|scrape)", re.I)


# 有的家把"被站点封了"包在自定义状态码里。Zyte 用 520 + `"title":"Website Ban"`，
# 落进 `other` 会让"被拦"看起来像"不明错误"——而这两个在归因表里的含义完全不同。
_SITE_BAN = re.compile(r'"title"\s*:\s*"Website Ban"|website ban|banned by (the )?(site|target)',
                       re.I)


def classify_body(code: int, body: str) -> tuple[str, str] | None:
    """响应体里**明说**的原因，没明说返回 None 交给状态码兜底。

    这两类在归因表里的含义完全不同：
      blocklisted_domain  它不给你抓这个域 —— 政策选择
      anti_bot_blocked    它试了、被目标站封了 —— 能力差距
    """
    if not body:
        return None
    if _POLICY_REFUSAL.search(body):
        return "blocklisted_domain", "provider"
    if _SITE_BAN.search(body):
        return "anti_bot_blocked", "provider"
    return None


def _classify_http(code: int, body: str = "") -> tuple[str, str]:
    """HTTP 状态 -> (failure_reason, fault)。**响应体里明说的原因优先于状态码**。"""
    told = classify_body(code, body)
    if told:
        return told
    if code == 429:
        return "rate_limited", "provider"
    if code == 402:
        # 余额不足是**我们账户的状态**，不是这家的抓取能力。记成 provider 会让它拿 0%
        # 而看起来很差 —— 那是把我们没充值算成了它的分。
        return "other", "harness"
    if code in (408, 425, 500, 502, 503, 504):
        return "timeout_upstream", "provider"
    if code in (404, 410, 415):
        return "content_type_or_404", "provider"
    if code == 451:
        return "blocklisted_domain", "provider"
    if code in (401, 403):
        return "anti_bot_blocked", "provider"
    return "other", "provider"


def _classify_exc(e: Exception) -> tuple[str, str]:
    if isinstance(e, requests.Timeout):
        return "timeout_upstream", "provider"
    if isinstance(e, (requests.ConnectionError, requests.exceptions.SSLError)):
        return "timeout_upstream", "provider"
    return "other", "provider"


class FetchProvider(ABC):
    """一家 provider。`fetch` 永远返回 FetchResponse，**只有缺 key 才抛**。"""
    name: str = ""
    env_key: str | None = None
    # **四态，不是布尔**：
    #   pinned    有这个参数，且我们设成了"实时抓、不走缓存"
    #   no_knob   已核实这家 API 没有这个参数（或它本来就每次实抓）
    #   unknown   查不到 —— 它不校验未知参数，猜一个名字塞进去会被静默忽略，
    #             而我们会以为缓存关掉了。查不到就如实说查不到，别装作钉住了
    #   unpinned  有参数但我们没设（不该出现在已接线的家上）
    # 混成布尔会让"这家没这个能力"和"我们漏设了"长得一样。
    cache_pinned: str = "unpinned"
    endpoint_verified: bool = True

    def _key(self) -> str:
        if not self.env_key:
            return ""
        v = os.environ.get(self.env_key, "").strip()
        if not v:
            hint = "" if self.endpoint_verified else "；另：该家 endpoint 口径待核，拿到 key 后按官方文档填"
            raise RuntimeError(
                f"{self.name}: 缺少环境变量 {self.env_key}{hint}")
        return v

    def _finish(self, url: str, text: str, t0: float, **kw) -> FetchResponse:
        """成品收口：算 len_norm、套自家上限、空文本归 page 故障。"""
        ms = (time.time() - t0) * 1000
        meta = kw.pop("raw_meta", {})
        if not (text or "").strip():
            return FetchResponse(url=url, provider=self.name, status="error", text="",
                                 latency_ms=ms, failure_reason="nothing_extractable",
                                 fault="page", cache_pinned=self.cache_pinned,
                                 raw_meta=meta, **kw)
        reason = fault = None
        if len(text) > MAX_TEXT_CHARS:
            # 留住已取到的部分。丢掉等于把我们的上限伪装成"这家取不到"。
            text = text[:MAX_TEXT_CHARS]
            reason, fault = "our_size_cap", "harness"
            meta = {**meta, "truncated": True}
        return FetchResponse(url=url, provider=self.name, status="ok", text=text,
                             len_norm=_len_norm(text), latency_ms=ms,
                             failure_reason=reason, fault=fault,
                             cache_pinned=self.cache_pinned, raw_meta=meta, **kw)

    def _fail(self, url: str, t0: float, *, error: str, reason: str, fault: str,
              http_status: int | None = None) -> FetchResponse:
        return FetchResponse(url=url, provider=self.name, status="error",
                             latency_ms=(time.time() - t0) * 1000,
                             http_status=http_status, error=error[:300],
                             failure_reason=reason, fault=fault,
                             cache_pinned=self.cache_pinned)

    @abstractmethod
    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        ...


class _HttpFetcher(FetchProvider):
    """走 HTTP 的家共用的壳。子类只需给 endpoint / _headers / _body / _pluck。"""
    endpoint: str = ""
    output_form: str = "text"        # text | html —— html 的会被归一化，报告里要声明
    auth_basic: bool = False         # True 表示 key 走 HTTP Basic 而不是 header
    jsonl_body: bool = False         # True 表示响应是 JSONL（每行一个对象）而不是 JSON

    def _headers(self, key: str) -> dict:
        raise NotImplementedError

    def _body(self, url: str, timeout: int) -> dict:
        raise NotImplementedError

    def _pluck(self, data: dict) -> str:
        raise NotImplementedError

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        key = self._key()                       # 缺 key 在这里抛，不吞
        if not self.endpoint:
            raise RuntimeError(f"{self.name}: endpoint 口径待核，按官方文档填 endpoint/_body/_pluck")
        # 请求体在 try 之外构造：配置错误（缺 zone 之类）该硬失败，
        # 落进 try 会被当成传输故障记成一行 error，把配置问题伪装成"这家抓不到"。
        headers, body = self._headers(key), self._body(url, timeout)
        t0 = time.time()
        try:
            r = requests.post(self.endpoint, headers=headers, json=body,
                              auth=(key, "") if self.auth_basic else None,
                              timeout=timeout + 10)
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        if r.status_code >= 400:
            reason, fault = _classify_http(r.status_code, r.text or "")
            return self._fail(url, t0, error=(r.text or "")[:300], reason=reason,
                              fault=fault, http_status=r.status_code)
        try:
            if self.jsonl_body:
                # JSONL：每行一个对象。整体 json.loads 会直接抛，而那会被记成
                # "normalizer_crashed"，把接口形态问题伪装成我们的解析器崩了。
                rows = [json.loads(l) for l in (r.text or "").split("\n") if l.strip()]
                data = rows[0] if rows else {}
            else:
                data = r.json()
        except Exception as e:                  # noqa: BLE001
            return self._fail(url, t0, error=f"响应不是 JSON: {e}",
                              reason="normalizer_crashed", fault="harness",
                              http_status=r.status_code)
        try:
            text = self._pluck(data) or ""
        except Exception as e:                  # noqa: BLE001
            # 字段映射错了要喊出来，不能伪装成"这家取不到"
            return self._fail(url, t0, error=f"字段映射失败: {type(e).__name__}: {e}",
                              reason="normalizer_crashed", fault="harness",
                              http_status=r.status_code)
        meta = {}
        if self.output_form == "html" and text:
            meta = {"output_form": "html", "raw_len": len(text)}
            text = html_to_text(text)
        return self._finish(url, text, t0, http_status=r.status_code, raw_meta=meta)


# ── 口径已核实的家 ─────────────────────────────────────────────────────────

class OctenFetcher(_HttpFetcher):
    name = "octen"
    env_key = "OCTEN_API_KEY"
    endpoint = "https://api.octen.ai/extract"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"X-Api-Key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        # 默认 86400。skill 文档写下限是 300，但 2026-09-01 实测 API 接受 0 且更慢
        # （3.6s vs 2.5s），说明 0 确实绕开了缓存 —— 用 0，别留 5 分钟的窗口。
        return {"urls": [url], "format": "markdown", "max_age_seconds": 0,
                "timeout": min(timeout, 60)}

    def _pluck(self, data):
        res = (data.get("data") or {}).get("results") or []
        return (res[0].get("full_content") or res[0].get("text") or "") if res else ""


class FirecrawlFetcher(_HttpFetcher):
    name = "firecrawl"
    env_key = "FIRECRAWL_API_KEY"
    endpoint = "https://api.firecrawl.dev/v1/scrape"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url, "formats": ["markdown"], "maxAge": 0,
                "timeout": min(timeout, 60) * 1000}

    def _pluck(self, data):
        return (data.get("data") or {}).get("markdown") or ""


class ExaFetcher(_HttpFetcher):
    name = "exa"
    env_key = "EXA_API_KEY"
    endpoint = "https://api.exa.ai/contents"
    cache_pinned = "pinned"

    def _headers(self, key):
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        # **livecrawl 必须显式设成 always。** 不传时默认 `fallback` —— 先查自家索引，
        # 命中就直接返回。2026-09-01 实测 never/fallback 0.7s、always 1.9s，差三倍，
        # 说明 always 确实去实抓了。不设的话这一列量的是"索引覆盖率"而不是"抓取能力"。
        return {"urls": [url], "text": True, "livecrawl": "always"}

    def _pluck(self, data):
        res = data.get("results") or []
        return (res[0].get("text") or "") if res else ""


class BrightDataFetcher(_HttpFetcher):
    """走 **Datasets v3 scrape**（`dataset_id` 那个口），不是 Web Unlocker 的 `/request`。

    两者是不同产品：`/request` 需要账户下有 unlocker 型 zone；只有 res_static /
    serp 型 zone 时那条路走不通，得走 Datasets v3。
    Datasets v3 是同步的（实测 4.1s），返回 **JSONL**，字段有 markdown / html2text /
    page_html —— 取 `markdown`，形态与别家一致。
    """
    name = "brightdata"
    env_key = "BRIGHTDATA_API_KEY"
    endpoint = ("https://api.brightdata.com/datasets/v3/scrape"
                "?dataset_id=%s&notify=false&include_errors=true")
    jsonl_body = True
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"input": [{"url": url}], "limit_per_input": None}

    def _pluck(self, data):
        return data.get("markdown") or data.get("html2text") or ""

    def fetch(self, url, timeout=DEFAULT_TIMEOUT):
        ds = os.environ.get("BRIGHTDATA_SCRAPE_DATASET", "").strip()
        if not ds:
            # 不给假默认值：猜一个 dataset_id 换来的是一整列 400，看起来像"这家抓不到"
            raise RuntimeError("brightdata: 需要 BRIGHTDATA_SCRAPE_DATASET（Datasets v3 的 dataset_id）")
        self.endpoint = type(self).endpoint % ds
        return super().fetch(url, timeout)


class TavilyFetcher(_HttpFetcher):
    """不在参考报告的 13 家里，但我们有 key 且它确有 /extract 口 —— 作为可选第 14 家。"""
    name = "tavily"
    env_key = "TAVILY_API_KEY"
    endpoint = "https://api.tavily.com/extract"
    # 官方文档的 /extract 参数表（urls / query / chunks_per_source / extract_depth /
    # include_images / include_favicon / format / timeout / include_usage）里**没有**
    # 缓存控制项。注意它不校验未知参数 —— 猜一个名字塞进去它会静默忽略，
    # 而我们会以为缓存关掉了。所以只能如实标"没有这个旋钮"。
    cache_pinned = "no_knob"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url], "extract_depth": "advanced"}

    def _pluck(self, data):
        res = data.get("results") or []
        return (res[0].get("raw_content") or "") if res else ""


# ── 本地库（无需 key）───────────────────────────────────────────────────────

def _import_trafilatura():
    try:
        import trafilatura
        return trafilatura
    except ImportError:
        return None


def _import_readability():
    try:
        import html2text
        from readability import Document
        return Document, html2text
    except ImportError:
        return None


NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}

_TAG = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """把 HTML 归一化成文本。

    有的家（zyte 的 browserHtml、you 的 contents）返回的就是 HTML，而别家返回
    markdown/文本。**本轮只评抓取能力**，HTML->文本是解析步骤、不在评价范围 ——
    不归一化的话它们会被判定器当成"返回了原始载荷"全判 lost，那量到的是输出格式
    不是抓取能力。做了这一步要在报告里声明（`output_form` 字段）。
    """
    if not html:
        return ""
    s = _TAG.sub(" ", html)
    s = _ANY_TAG.sub(" ", s)
    s = _html_unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n\s*\n+", "\n\n", s)).strip()


class TrafilaturaFetcher(FetchProvider):
    name = "trafilatura"
    cache_pinned = "pinned"

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        t0 = time.time()
        mod = _import_trafilatura()
        if mod is None:
            # 依赖缺失是我们的锅，不是它抓不到 —— 故障不是 0 分（playbook 5.4）
            return self._fail(url, t0, error="trafilatura 未安装（requirements-fetch.txt）",
                              reason="normalizer_crashed", fault="harness")
        try:
            raw = mod.fetch_url(url)
            text = mod.extract(raw, include_tables=True, include_links=True) if raw else ""
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        return self._finish(url, text or "", t0)


class ReadabilityFetcher(FetchProvider):
    name = "readability"
    cache_pinned = "pinned"

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> FetchResponse:
        t0 = time.time()
        mods = _import_readability()
        if mods is None:
            return self._fail(url, t0, error="readability-lxml / html2text 未安装",
                              reason="normalizer_crashed", fault="harness")
        Document, html2text = mods
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; search-eval/1.0)",
                                      **NO_CACHE_HEADERS})
            if r.status_code >= 400:
                reason, fault = _classify_http(r.status_code, r.text or "")
                return self._fail(url, t0, error=(r.text or "")[:300], reason=reason,
                                  fault=fault, http_status=r.status_code)
            h = html2text.HTML2Text()
            h.body_width = 0
            text = h.handle(Document(r.text).summary())
        except Exception as e:                  # noqa: BLE001
            reason, fault = _classify_exc(e)
            return self._fail(url, t0, error=f"{type(e).__name__}: {e}",
                              reason=reason, fault=fault)
        return self._finish(url, text or "", t0)


# ── endpoint 口径待核的 7 家：骨架 + 缺 key 硬失败 ───────────────────────────

# ── 2026-09-01 新接线的四家（endpoint 与参数均**行为验证过**，不是按印象填）──────

class ZyteFetcher(_HttpFetcher):
    """`browserHtml` = 每次真开一个浏览器渲染，所以本来就没有缓存概念。
    另两个模式：`httpResponseBody`（base64 原始响应，不跑 JS）、`article`（它自己的
    结构化抽取，对非文章页几乎为空）。抓取能力口径下 browserHtml 才是可比的那个。"""
    name = "zyte"
    env_key = "ZYTE_API_KEY"
    endpoint = "https://api.zyte.com/v1/extract"
    auth_basic = True                # key 作用户名、密码留空
    output_form = "html"
    cache_pinned = "no_knob"

    def _headers(self, key):
        return {"Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url, "browserHtml": True}

    def _pluck(self, data):
        return data.get("browserHtml") or ""


class YouFetcher(_HttpFetcher):
    """字段是 `urls`（复数数组）—— 422 的校验错误直接把 schema 说出来了。返回 HTML。"""
    name = "you"
    env_key = "YOU_API_KEY"
    endpoint = "https://api.you.com/v1/contents"
    output_form = "html"
    cache_pinned = "unknown"         # 没查到新鲜度参数，如实标查不到

    def _headers(self, key):
        return {"X-API-Key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url]}

    def _pluck(self, data):
        items = data if isinstance(data, list) else (data.get("results") or [])
        return (items[0].get("html") or items[0].get("text") or "") if items else ""


class LinkupFetcher(_HttpFetcher):
    """`/v1/fetch` 直接返回干净 markdown。**它不校验未知参数**（塞 `__bogus__` 照样 200），
    所以缓存开关不能靠试名字 —— 猜错会被静默忽略而我们以为钉住了。"""
    name = "linkup"
    env_key = "LINKUP_API_KEY"
    endpoint = "https://api.linkup.so/v1/fetch"
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url}

    def _pluck(self, data):
        return data.get("markdown") or data.get("rawHtml") or ""


class ParallelFetcher(_HttpFetcher):
    """用 `v1beta/extract` + `full_content: True`，**不用 `v1/extract` 的 objective 模式**。

    2026-09-01 在同一个 URL 上实测：
      v1  + objective        excerpts 1639 字符（按问题收窄）
      v1  裸                 excerpts 3521 字符
      v1beta + full_content  full_content 3521 字符（全文）
    `objective` 是查询驱动的抽取 —— 抓取能力评测要的是**整页**，不是按某个问题收窄的
    片段，那会把"抓到了多少"混成"回答得准不准"。`v1` 不接受 `full_content` 参数
    （extra_forbidden），所以全文只能走 v1beta。"""
    name = "parallel"
    env_key = "PARALLEL_API_KEY"
    endpoint = "https://api.parallel.ai/v1beta/extract"
    cache_pinned = "unknown"

    def _headers(self, key):
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"urls": [url], "full_content": True}

    def _pluck(self, data):
        res = data.get("results") or []
        if not res:
            return ""
        r = res[0]
        return r.get("full_content") or "\n".join(r.get("excerpts") or [])



class _UnverifiedFetcher(_HttpFetcher):
    # 拿到 key 接线时，**顺手把各家的实时抓开关一并查清并设上** —— 不设的话
    # 那一列量的是索引覆盖率而不是抓取能力（exa 就栽在这儿）。
    """拿到 key 之后，按官方文档填 endpoint / _headers / _body / _pluck 四处即可。
    在此之前不编 endpoint —— 编错跑出来的是一整列假失败。"""
    endpoint = ""
    endpoint_verified = False
    cache_pinned = "unpinned"

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _body(self, url, timeout):
        return {"url": url}

    def _pluck(self, data):
        raise NotImplementedError(f"{self.name}: 取文本路径待核")



class ContextFetcher(_UnverifiedFetcher):
    name = "context"
    env_key = "CONTEXT_API_KEY"





class CloudflareFetcher(_UnverifiedFetcher):
    name = "cloudflare"
    env_key = "CLOUDFLARE_API_TOKEN"


class ApifyFetcher(_UnverifiedFetcher):
    name = "apify"
    env_key = "APIFY_API_TOKEN"


FETCHERS: dict[str, type[FetchProvider]] = {
    c.name: c for c in (
        OctenFetcher, FirecrawlFetcher, ExaFetcher, BrightDataFetcher, TavilyFetcher,
        TrafilaturaFetcher, ReadabilityFetcher,
        ParallelFetcher, ContextFetcher, YouFetcher, LinkupFetcher,
        ZyteFetcher, CloudflareFetcher, ApifyFetcher,
    )
}

# 参考报告的 13 家（tavily 不在其中，是我们额外的可选一家）
ROSTER_13 = ("firecrawl", "context", "octen", "parallel", "exa", "you", "linkup",
             "zyte", "cloudflare", "brightdata", "readability", "apify", "trafilatura")

# 今天能跑的家：有 key、且 endpoint 与实时抓参数都**行为验证过**。
# 接新家时顺手把它的「实时抓、不走缓存」开关一并查清设上 —— 不设的话那一列量的是
# 索引覆盖率而不是抓取能力（exa 的 livecrawl 就栽过这一条）。
RUNNABLE_TODAY = ("octen", "exa", "tavily", "trafilatura", "readability",
                  "zyte", "you", "linkup", "parallel", "firecrawl", "brightdata")



def env_divergence(providers=None) -> dict[str, tuple[str, str]]:
    """`.env` 与 shell 环境不一致的凭据。返回 {变量名: (env里的, shell里的)}（已掩码）。

    **`_load_dotenv` 用的是 `os.environ.setdefault`，不覆盖已存在的变量。** 于是 shell 里
    残留的旧 key 会静默压过 `.env` 里的新 key，而两边看起来都"设好了"。
    实测踩到过：新 key 写进了 .env，跑出来却一直是鉴权/额度类错误 —— 因为 shell 里
    有个旧 key 一直在生效，新 key 一次都没被用过。两边看起来都"设好了"，最难查。

    凭据不进日志：只回掩码后的前后几位，够人辨认是哪一把即可（playbook §9.7）。
    """
    from pathlib import Path
    path = Path(__file__).parent.parent / ".env"
    if not path.exists():
        return {}
    want = None
    if providers:
        want = {FETCHERS[p].env_key for p in providers
                if p in FETCHERS and FETCHERS[p].env_key}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if want is not None and k not in want:
            continue
        shell = os.environ.get(k)
        if shell is not None and shell != v:
            m = lambda x: (x[:6] + "…" + x[-4:]) if x and len(x) > 12 else "(短)"
            out[k] = (m(v), m(shell))
    return out


def get_fetcher(name: str) -> FetchProvider:
    return FETCHERS[name]()


assert set(ROSTER_13) <= set(FETCHERS), "roster 里有名字没有对应 adapter"
assert len(ROSTER_13) == 13
assert set(RUNNABLE_TODAY) <= set(FETCHERS)
