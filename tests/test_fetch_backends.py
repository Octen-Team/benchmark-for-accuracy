"""Field mapping and failure attribution for the fetch adapters. Fully mocked; no
network access.

Three rules are enforced here:
  A missing key fails hard.  Skipping silently yields a round with zero coverage and
                             a report showing zero faults.
  Cache freshness is pinned. Unpinned, the latency column ranks cache hit rates.
  Our own caps are harness   A size cap we imposed must not be charged to the
  faults.                    provider.
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
    """Stub requests.post/get and hand back the request that was sent, for assertions."""
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
    """Every provider's live-fetch knob must be set explicitly.

    Unset, the round measures **index coverage** rather than **fetch capability**: a
    provider whose live-crawl parameter defaults to consulting its own index first
    returns cached content on a hit, and is measurably faster for it.
    """

    def test_exa_forces_a_live_crawl(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={"results": [{"text": "x"}]}))
        r = B.get_fetcher("exa").fetch("https://example.com/")
        assert sent["json"]["livecrawl"] == "always", \
            "unset it defaults to a fallback mode, and the column then measures index hits"
        assert r.cache_pinned == "pinned"

    def test_octen_asks_for_zero_age(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={
            "data": {"results": [{"status": "success", "full_content": "x"}]}}))
        r = B.get_fetcher("octen").fetch("https://example.com/")
        assert sent["json"]["max_age_seconds"] == 0, "any nonzero value leaves a cache window"
        assert r.cache_pinned == "pinned"

    def test_firecrawl_pins_maxage_zero(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(payload={"data": {"markdown": "x"}}))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert sent["json"]["maxAge"] == 0
        assert r.cache_pinned == "pinned"

    def test_a_provider_without_the_knob_says_so_rather_than_looking_unpinned(self,
                                                                             monkeypatch):
        """Three states, not a boolean: "this API has no such parameter" and "we forgot
        to set it" mean entirely different things.

        Some APIs do not validate unknown parameters, so a guessed name is silently
        ignored while we believe the cache was disabled.
        """
        _capture(monkeypatch, _Resp(payload={"results": [{"raw_content": "x"}]}))
        r = B.get_fetcher("tavily").fetch("https://example.com/")
        assert r.cache_pinned == "no_knob"

    @pytest.mark.skipif(importlib.util.find_spec("readability") is None,
                        reason="readability-lxml is not installed (optional dependency)")
    def test_local_libraries_send_no_cache_headers(self, monkeypatch):
        sent = _capture(monkeypatch, _Resp(text="<html><body>hi there</body></html>"))
        B.get_fetcher("readability").fetch("https://example.com/")
        assert "no-cache" in sent["headers"]["Cache-Control"]

    def test_every_state_is_one_of_the_four(self):
        for name, cls in B.FETCHERS.items():
            assert cls.cache_pinned in ("pinned", "no_knob", "unknown", "unpinned"), name

    def test_unwired_providers_are_unpinned_not_no_knob(self):
        """An unwired provider must not claim "no such knob" — we simply have not looked."""
        for n in ("context", "cloudflare", "apify"):
            assert B.FETCHERS[n].cache_pinned == "unpinned", n


class TestKeyDiscipline:
    def test_missing_key_hard_fails_with_the_env_var_name(self, monkeypatch):
        monkeypatch.delenv("OCTEN_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OCTEN_API_KEY"):
            B.get_fetcher("octen").fetch("https://example.com/")

    def test_unkeyed_providers_say_endpoint_unverified_too(self, monkeypatch):
        """An unwired provider says the endpoint shape is unverified alongside the missing
        key, so nobody assumes a key alone makes it runnable."""
        monkeypatch.delenv("CONTEXT_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            B.get_fetcher("context").fetch("https://example.com/")
        assert "CONTEXT_API_KEY" in str(e.value)
        assert "unverified" in str(e.value)

    def test_a_wired_provider_only_complains_about_the_key(self, monkeypatch):
        monkeypatch.delenv("ZYTE_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            B.get_fetcher("zyte").fetch("https://example.com/")
        assert "ZYTE_API_KEY" in str(e.value)
        assert "unverified" not in str(e.value), "this shape was verified; stop saying otherwise"


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
        assert r.fault == "harness", "charging our own cap to the provider scores our bug as theirs"
        assert r.text, "truncation must keep what was already retrieved"


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
        """Providers that can run; those blocked on account state are listed separately
        rather than folded into "unavailable"."""
        assert set(B.RUNNABLE_TODAY) == {
            "octen", "exa", "tavily", "trafilatura", "readability",
            "zyte", "you", "linkup", "parallel", "firecrawl", "brightdata"}

        assert all(not B.FETCHERS[n].endpoint_verified
                   for n in ("context", "cloudflare", "apify"))

class TestAccountVsCapability:
    def test_402_is_our_account_not_their_capability(self, monkeypatch):
        """Charging an exhausted balance to the provider scores our unpaid invoice as
        their weakness."""
        _capture(monkeypatch, _Resp(status=402, text="Insufficient credits"))
        r = B.get_fetcher("firecrawl").fetch("https://example.com/")
        assert r.failure_reason == "other"
        assert r.fault == "harness"

class TestNewlyWiredProviders:
    """Adapters whose endpoint and parameters were verified by observed behaviour."""

    def test_zyte_uses_basic_auth_and_browser_render(self, monkeypatch):
        monkeypatch.setenv("ZYTE_API_KEY", "zk")
        sent = {}

        def fake(url, **kw):
            sent.update(url=url, json=kw.get("json"), auth=kw.get("auth"))
            return _Resp(payload={"browserHtml": "<h1>Title</h1><p>body text</p>"})
        monkeypatch.setattr(requests, "post", fake)
        r = B.get_fetcher("zyte").fetch("https://example.com/")
        assert sent["auth"] == ("zk", ""), "key goes in the HTTP Basic username, empty password"
        assert sent["json"]["browserHtml"] is True
        assert r.status == "ok" and "body text" in r.text
        assert "<h1>" not in r.text, "HTML must be normalised, or it is judged a raw payload"
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
        """Objective mode is query-driven and conflates "how much was retrieved" with
        "how well it answered"."""
        monkeypatch.setenv("PARALLEL_API_KEY", "pk")
        sent = _capture(monkeypatch, _Resp(payload={"results": [
            {"full_content": "the whole page", "excerpts": ["a snippet"]}]}))
        r = B.get_fetcher("parallel").fetch("https://example.com/")
        assert sent["json"]["full_content"] is True
        assert "objective" not in sent["json"], "fetch capability needs the whole page"
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
        """This API does not validate unknown parameters either: a guessed name is
        silently ignored while we believe the cache was disabled. Report unknown."""
        assert B.FETCHERS["linkup"].cache_pinned == "unknown"
        assert B.FETCHERS["you"].cache_pinned == "unknown"
        assert B.FETCHERS["parallel"].cache_pinned == "unknown"
        assert B.FETCHERS["zyte"].cache_pinned == "no_knob", "renders in a real browser on every call"


class TestBrightDataDatasetsV3:
    """Uses the Datasets v3 scrape endpoint, not the Web Unlocker request endpoint —
    different products, and an account without an unlocker zone cannot use the latter."""

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
        """A guessed dataset_id buys a whole column of 400s that looks like a fetch failure."""
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
    """"I will not fetch this domain for you" and "the target site blocked me" are
    different things.

    The first is a policy choice, the second a capability gap, and they mean opposite
    things to anyone choosing a provider. Folding both into anti_bot_blocked makes
    "we do not do this business" look like "we cannot get through".
    """

    def test_policy_wording_becomes_blocklisted_domain(self):
        assert B.classify_body(
            403, '{"error":"We apologize but we do not support this site."}'
        ) == ("blocklisted_domain", "provider")

    def test_a_real_block_stays_anti_bot(self):
        assert B.classify_body(403, "Access denied. Cloudflare Ray ID: abc") is None, \
            "with no stated reason, the status code decides"
        assert B.classify_body(403, "") is None

    def test_unreachable_target_is_named_not_left_unexplained(self):
        """A provider that says it could not reach the target has stated its reason.
        Leaving that in `other` hides a whole class of failure from the attribution
        table."""
        body = ('{"error":{"code":"FETCH_TARGET_UNREACHABLE","message":"The target URL '
                'could not be reached (connection failed or timed out)"}}')
        assert B.classify_body(400, body) == ("target_unreachable", "provider")

    def test_our_own_transport_failure_is_a_different_reason(self):
        """Whose failure it is decides whether retrying is honest: our connection to the
        provider dropping is a delivery problem, the provider failing to reach the target
        is its answer."""
        import requests
        assert B._classify_exc(requests.Timeout())[0] == "timeout_upstream"
        assert B._classify_exc(requests.ConnectionError())[0] == "timeout_upstream"

    def test_unsupported_content_type_is_named(self):
        assert B.classify_body(
            400, '{"error":{"code":"FETCH_UNSUPPORTED_CONTENT_TYPE"}}'
        ) == ("content_type_or_404", "provider")

    def test_a_body_with_no_stated_reason_defers_to_the_status_code(self):
        assert B.classify_body(500, "internal server error") is None

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
