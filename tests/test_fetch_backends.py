"""13 家 fetch adapter 的字段映射与失败归因。全部 mock，不打真网络。

三条纪律在这里落地：
  缺 key 硬失败    静默跳过会跑出"整轮零覆盖而报告显示零故障"（commit 69b778f）。
  缓存新鲜度钉死   不钉的话延迟列量的是缓存命中率的排名（playbook 5.8）。
  自家上限记 harness  参考报告的头条发现正是"21 条失败是我们自己的代码"。
"""
import json

import importlib.util

import pytest
import requests

from src import fetch_backends as B


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for k in ("OCTEN_API_KEY", "FIRECRAWL_API_KEY", "EXA_API_KEY",
              "BRIGHTDATA_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(k, "test-key")
    monkeypatch.setenv("BRIGHTDATA_ZONE", "test-zone")


def _capture(monkeypatch, resp):
    """打桩 requests.post/get，回传被发出的请求体供断言。"""
    sent = {}

    def fake(url, **kw):
        sent["url"] = url
        sent["json"] = kw.get("json")
        sent["headers"] = kw.get("headers")
        return resp

    monkeypatch.setattr(requests, "post", fake)
    monkeypatch.setattr(requests, "get", fake)
    return sent


class TestFieldMapping:
    def test_octen_maps_full_content(self, monkeypatch):
        _capture(monkeypatch, _Resp(payload={
            "data": {"results": [{"url": "u", "status": "success", "full_content": "BODY TEXT"}]}}))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.status == "ok" and r.text == "BODY TEXT"
        assert r.len_norm == 2 and r.provider == "octen"

    def test_firecrawl_maps_markdown(self, monkeypatch):
        _capture(monkeypatch, _Resp(payload={"success": True, "data": {"markdown": "# Hi"}}))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert r.status == "ok" and r.text == "# Hi"

    def test_exa_maps_first_result_text(self, monkeypatch):
        _capture(monkeypatch, _Resp(payload={"results": [{"text": "exa body"}]}))
        r = B.get_fetcher("exa").fetch("https://example.com/")
        assert r.status == "ok" and r.text == "exa body"

class TestLiveFetchIsForced:
    """各家的"实时抓、不走缓存"开关必须显式设上。

    不设的话量到的是**索引覆盖率**而不是**抓取能力** —— exa 的 `livecrawl` 不传时默认
    `fallback`（先查自家索引，命中就直接返回），2026-09-01 实测 never/fallback 0.7s、
    always 1.9s，差三倍。
    """

    def test_exa_forces_a_live_crawl(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={"results": [{"text": "x"}]}))
        r = B.get_fetcher("exa").fetch("https://example.com/")
        assert sent["json"]["livecrawl"] == "always", \
            "不传时默认 fallback，那一列量的就是索引命中率"
        assert r.cache_pinned == "pinned"

    def test_octen_asks_for_zero_age(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={
            "data": {"results": [{"status": "success", "full_content": "x"}]}}))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert sent["json"]["max_age_seconds"] == 0, "留 300 就是留了 5 分钟的缓存窗口"
        assert r.cache_pinned == "pinned"

    def test_firecrawl_pins_maxage_zero(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={"data": {"markdown": "x"}}))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert sent["json"]["maxAge"] == 0
        assert r.cache_pinned == "pinned"

    def test_a_provider_without_the_knob_says_so_rather_than_looking_unpinned(self,
                                                                             monkeypatch):
        """三态而不是布尔：'这家没有这个参数' 和 '我们漏设了' 含义完全不同。

        tavily 的 /extract 官方参数表里没有缓存控制项，而且它**不校验未知参数** ——
        猜一个名字塞进去会被静默忽略，我们却会以为缓存关掉了。
        """
        _capture(monkeypatch, _Resp(payload={"results": [{"raw_content": "x"}]}))
        r = B.get_fetcher("tavily").fetch("https://example.com/")
        assert r.cache_pinned == "no_knob"

    @pytest.mark.skipif(importlib.util.find_spec("readability") is None,
                        reason="readability-lxml 未安装（requirements-fetch.txt 可选依赖）")
    def test_local_libraries_send_no_cache_headers(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(text="<html><body>hi there</body></html>"))
        B.get_fetcher("readability").fetch("https://example.com/")
        assert "no-cache" in sent["headers"]["Cache-Control"]

    def test_every_state_is_one_of_the_four(self):
        for name, cls in B.FETCHERS.items():
            assert cls.cache_pinned in ("pinned", "no_knob", "unknown", "unpinned"), name

    def test_unwired_providers_are_unpinned_not_no_knob(self):
        """还没接线的家不能声称"没有这个旋钮" —— 我们只是还没查。"""
        for n in ("context", "cloudflare", "apify"):
            assert B.FETCHERS[n].cache_pinned == "unpinned", n


class TestKeyDiscipline:
    def test_missing_key_hard_fails_with_the_env_var_name(self, monkeypatch):
        monkeypatch.delenv("OCTEN_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OCTEN_API_KEY"):
            B.get_fetcher("octen").fetch("https://example.com/")

    def test_unkeyed_providers_say_endpoint_unverified_too(self, monkeypatch):
        """还没接线的家：缺 key 时同时说明"口径也待核"，免得以为填个 key 就能跑。"""
        monkeypatch.delenv("CONTEXT_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            B.get_fetcher("context").fetch("https://example.com/")
        assert "CONTEXT_API_KEY" in str(e.value)
        assert "待核" in str(e.value)

    def test_a_wired_provider_only_complains_about_the_key(self, monkeypatch):
        monkeypatch.delenv("ZYTE_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            B.get_fetcher("zyte").fetch("https://example.com/")
        assert "ZYTE_API_KEY" in str(e.value)
        assert "待核" not in str(e.value), "口径已核实过，别再说待核"


class TestFailureClassification:
    def test_429_is_rate_limited_and_provider_fault(self, monkeypatch):
        _capture(monkeypatch, _Resp(status=429, text="slow down"))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.status == "error"
        assert r.failure_reason == "rate_limited" and r.fault == "provider"
        assert r.http_status == 429

    def test_503_is_timeout_upstream(self, monkeypatch):
        _capture(monkeypatch, _Resp(status=503, text="down"))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.failure_reason == "timeout_upstream" and r.fault == "provider"

    def test_404_is_content_type_or_404(self, monkeypatch):
        _capture(monkeypatch, _Resp(status=404, text="nope"))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.failure_reason == "content_type_or_404"

    def test_timeout_exception_maps_to_timeout_upstream(self, monkeypatch):
        def boom(url, **kw):
            raise requests.Timeout("timed out")
        monkeypatch.setattr(requests, "post", boom)
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.failure_reason == "timeout_upstream" and r.fault == "provider"

    def test_empty_body_on_200_is_nothing_extractable_page_fault(self, monkeypatch):
        _capture(monkeypatch, _Resp(payload={"data": {"results": [
            {"status": "success", "full_content": ""}]}}))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.status == "error"
        assert r.failure_reason == "nothing_extractable" and r.fault == "page"

    def test_our_size_cap_is_harness_fault_and_keeps_the_text(self, monkeypatch):
        big = "word " * 10
        monkeypatch.setattr(B, "MAX_TEXT_CHARS", 10)
        _capture(monkeypatch, _Resp(payload={"data": {"results": [
            {"status": "success", "full_content": big}]}}))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert r.failure_reason == "our_size_cap"
        assert r.fault == "harness", "自家上限记到厂商头上就是把我们的 bug 算成它的分"
        assert r.text, "截断也要留住已取到的文本，不能丢"


class TestLocalLibs:
    def test_missing_local_dep_is_unavailable_not_zero(self, monkeypatch):
        monkeypatch.setattr(B, "_import_trafilatura", lambda: None)
        r = B.get_fetcher("trafilatura").fetch("https://example.com/")
        assert r.status == "error"
        assert r.failure_reason == "normalizer_crashed" and r.fault == "harness"


class TestRoster:
    def test_roster_is_the_thirteen_from_the_reference_report(self):
        assert len(B.ROSTER_13) == 13
        assert set(B.ROSTER_13) == {
            "firecrawl", "context", "octen", "parallel", "exa", "you", "linkup",
            "zyte", "cloudflare", "brightdata", "readability", "apify", "trafilatura"}

    def test_every_roster_name_resolves(self):
        for n in B.ROSTER_13:
            assert isinstance(B.get_fetcher(n), B.FetchProvider)

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            B.get_fetcher("nope")

    def test_runnable_today_is_declared(self):
        """2026-09-01 实测能跑的家；卡在账户上的另列，不混进"不可用"。"""
        assert set(B.RUNNABLE_TODAY) == {
            "octen", "exa", "tavily", "trafilatura", "readability",
            "zyte", "you", "linkup", "parallel", "firecrawl", "brightdata"}

        assert all(not B.FETCHERS[n].endpoint_verified
                   for n in ("context", "cloudflare", "apify"))

class TestAccountVsCapability:
    def test_402_is_our_account_not_their_capability(self, monkeypatch):
        """余额不足记成 provider 会让那家拿 0% 而显得很差 —— 那是把我们没充值算成它的分。"""
        _capture(monkeypatch, _Resp(status=402, text="Insufficient credits"))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert r.failure_reason == "other"
        assert r.fault == "harness"

class TestNewlyWiredProviders:
    """2026-09-01 接线的四家。endpoint 与参数都是**行为验证过**的，不是按印象填的。"""

    def test_zyte_uses_basic_auth_and_browser_render(self, monkeypatch):
        monkeypatch.setenv("ZYTE_API_KEY", "zk")
        sent = {}

        def fake(url, **kw):
            sent.update(url=url, json=kw.get("json"), auth=kw.get("auth"))
            return _Resp(payload={"browserHtml": "<h1>Title</h1><p>body text</p>"})
        monkeypatch.setattr(requests, "post", fake)
        r = B.get_fetcher("zyte").fetch("https://example.com/")
        assert sent["auth"] == ("zk", ""), "key 走 HTTP Basic 的用户名位，密码留空"
        assert sent["json"]["browserHtml"] is True
        assert r.status == "ok" and "body text" in r.text
        assert "<h1>" not in r.text, "HTML 要归一化成文本，否则会被判成返回了原始载荷"
        assert r.raw_meta["output_form"] == "html"

    def test_you_uses_the_plural_urls_field(self, monkeypatch):
        monkeypatch.setenv("YOU_API_KEY", "yk")
        sent = _capture(monkeypatch, _Resp(payload=[{"url": "u", "html": "<p>hello</p>"}]))
        r = B.get_fetcher("you").fetch("https://example.com/")
        assert sent["json"] == {"urls": ["https://example.com/"]}
        assert sent["headers"]["X-API-Key"] == "yk"
        assert r.text.strip() == "hello"

    def test_linkup_reads_markdown(self, monkeypatch):
        monkeypatch.setenv("LINKUP_API_KEY", "lk")
        sent = _capture(monkeypatch, _Resp(payload={"markdown": "# Hi", "favicon": "x"}))
        r = B.get_fetcher("linkup").fetch("https://example.com/")
        assert sent["json"] == {"url": "https://example.com/"}
        assert r.text == "# Hi"

    def test_parallel_asks_for_full_content_not_excerpts(self, monkeypatch):
        """`objective` 模式是查询驱动的，会把"抓到了多少"混成"回答得准不准"。"""
        monkeypatch.setenv("PARALLEL_API_KEY", "pk")
        sent = _capture(monkeypatch, _Resp(payload={"results": [
            {"full_content": "the whole page", "excerpts": ["a snippet"]}]}))
        r = B.get_fetcher("parallel").fetch("https://example.com/")
        assert sent["json"]["full_content"] is True
        assert "objective" not in sent["json"], "抓取能力评测要整页，不要按问题收窄的片段"
        assert r.text == "the whole page"

    def test_parallel_falls_back_to_excerpts_when_full_content_is_empty(self, monkeypatch):
        monkeypatch.setenv("PARALLEL_API_KEY", "pk")
        _capture(monkeypatch, _Resp(payload={"results": [
            {"full_content": "", "excerpts": ["part one", "part two"]}]}))
        r = B.get_fetcher("parallel").fetch("https://example.com/")
        assert "part one" in r.text and "part two" in r.text

    def test_html_normalisation_drops_script_and_style(self):
        out = B.html_to_text(
            "<style>body{color:red}</style><script>var x=1</script>"
            "<h1>Real</h1><p>content &amp; more</p>")
        assert out == "Real content & more"

    def test_the_four_are_no_longer_unverified(self):
        for n in ("zyte", "you", "linkup", "parallel"):
            assert B.FETCHERS[n].endpoint_verified is True, n
            assert n in B.RUNNABLE_TODAY, n

    def test_cache_state_says_unknown_when_we_could_not_verify(self):
        """linkup 和 tavily 一样不校验未知参数 —— 猜一个名字会被静默忽略，
        而我们会以为缓存关掉了。查不到就标 unknown，别装作钉住了。"""
        assert B.FETCHERS["linkup"].cache_pinned == "unknown"
        assert B.FETCHERS["you"].cache_pinned == "unknown"
        assert B.FETCHERS["parallel"].cache_pinned == "unknown"
        assert B.FETCHERS["zyte"].cache_pinned == "no_knob", "每次真开浏览器渲染"


class TestBrightDataDatasetsV3:
    """走 Datasets v3 scrape，不是 Web Unlocker 的 /request —— 两者是不同产品，
    这个账户没有 unlocker 型 zone，那条路一直走不通。"""

    def test_uses_the_dataset_endpoint_and_jsonl_body(self, monkeypatch):
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "bk")
        monkeypatch.setenv("BRIGHTDATA_SCRAPE_DATASET", "gd_test123")
        sent = _capture(monkeypatch, _Resp(
            text='{"markdown":"# Hi","url":"u","page_html":"<p>x</p>"}'))
        r = B.get_fetcher("brightdata").fetch("https://example.com/")
        assert "datasets/v3/scrape" in sent["url"]
        assert "dataset_id=gd_test123" in sent["url"]
        assert sent["json"] == {"input": [{"url": "https://example.com/"}],
                                "limit_per_input": None}
        assert r.status == "ok" and r.text == "# Hi"

    def test_jsonl_with_several_lines_takes_the_first(self, monkeypatch):
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "bk")
        monkeypatch.setenv("BRIGHTDATA_SCRAPE_DATASET", "gd_x")
        _capture(monkeypatch, _Resp(text='{"markdown":"first"}\n{"markdown":"second"}\n'))
        assert B.get_fetcher("brightdata").fetch("https://example.com/").text == "first"

    def test_missing_dataset_id_hard_fails_rather_than_guessing(self, monkeypatch):
        """猜一个 dataset_id 换来的是一整列 400，看起来像"这家抓不到"。"""
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "bk")
        monkeypatch.delenv("BRIGHTDATA_SCRAPE_DATASET", raising=False)
        with pytest.raises(RuntimeError, match="BRIGHTDATA_SCRAPE_DATASET"):
            B.get_fetcher("brightdata").fetch("https://example.com/")

    def test_falls_back_to_html2text_when_markdown_is_empty(self, monkeypatch):
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "bk")
        monkeypatch.setenv("BRIGHTDATA_SCRAPE_DATASET", "gd_x")
        _capture(monkeypatch, _Resp(text='{"markdown":"","html2text":"plain text"}'))
        assert B.get_fetcher("brightdata").fetch("https://example.com/").text == "plain text"

    def test_a_broken_jsonl_line_is_our_fault_not_theirs(self, monkeypatch):
        monkeypatch.setenv("BRIGHTDATA_API_KEY", "bk")
        monkeypatch.setenv("BRIGHTDATA_SCRAPE_DATASET", "gd_x")
        _capture(monkeypatch, _Resp(text='{"markdown": "unterminated'))
        r = B.get_fetcher("brightdata").fetch("https://example.com/")
        assert r.failure_reason == "normalizer_crashed" and r.fault == "harness"


class TestRosterAfterTheNewKeys:
    def test_eleven_runnable(self):
        assert len(B.RUNNABLE_TODAY) == 11

    def test_only_three_remain_unwired(self):
        left = [n for n in B.ROSTER_13 if not B.FETCHERS[n].endpoint_verified]
        assert set(left) == {"context", "cloudflare", "apify"}


class TestPolicyRefusalVsRealBlock:
    """「我不给你抓这个域」和「目标站把我拦了」是两回事。

    前者是政策选择、后者是能力差距，采购时含义完全不同。都塞进 anti_bot_blocked
    会让「不做这门生意」看起来像「打不过」。2026-09-01 实测：firecrawl 全部 11 条
    403 都是政策拒绝（"We do not support this site"），一条真实反爬失败都没有。
    """

    def test_policy_wording_becomes_blocklisted_domain(self):
        assert B.classify_body(
            403, '{"error":"We apologize but we do not support this site."}'
        ) == ("blocklisted_domain", "provider")

    def test_a_real_block_stays_anti_bot(self):
        assert B.classify_body(403, "Access denied. Cloudflare Ray ID: abc") is None, \
            "没明说原因就交给状态码兜底"
        assert B.classify_body(403, "") is None

    def test_403_is_split_at_the_http_classifier(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fk")
        _capture(monkeypatch, _Resp(status=403, text='{"error":"we do not support this site"}'))
        r = B.get_fetcher("firecrawl").fetch("https://reddit.com/")
        assert r.failure_reason == "blocklisted_domain"

    def test_401_is_not_treated_as_a_policy_refusal(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fk")
        _capture(monkeypatch, _Resp(status=401, text="unauthorized"))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert r.failure_reason == "anti_bot_blocked"
