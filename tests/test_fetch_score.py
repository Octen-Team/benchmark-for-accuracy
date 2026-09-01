"""抓取成败判定 + 面板。**本轮只评抓取能力** —— 主口径只回答"这一页抓到了没有"。

判定是这条流水线里最贵也最容易错的一步，测试要厚。
"""
from src import fetch_score as S
from src.fetch_spec import TH


def page(**kw):
    base = {"pid": "p001", "url": "https://e.com/a", "type": "baseline",
            "expect": "content", "probes": [], "doc_type": "html",
            "antibot_subclass": None, "lang": None,
            "gt": {"vocab": ["alpha", "beta", "gamma", "delta", "epsilon"],
                   "boiler_terms": ["home", "about", "careers"],
                   "anchors": ["alpha", "beta"],
                   "render_anchors": ["price", "stock"],
                   "title": "A Page", "degenerate": False}}
    gt = {**base["gt"], **kw.pop("gt", {})}
    return {**base, **kw, "gt": gt}


def antibot_page(subclass="waf", **kw):
    """反爬页在真实数据里是**弱 GT**：只有 shape 标签，没有词表也没有锚点。"""
    return page(type="antibot", antibot_subclass=subclass,
                gt={"vocab": [], "boiler_terms": [], "anchors": [],
                    "render_anchors": [], "shape": "bot_wall"}, **kw)


def resp(text="alpha beta gamma delta epsilon", **kw):
    return {"provider": "octen", "status": "ok", "text": text,
            "latency_ms": 100.0, "failure_reason": None, "fault": None, **kw}


def verdict(p, r):
    ch = S.run_checks(p, r)
    return S.mechanical_verdict(p, r, ch), ch


class TestScopeIsFetchOnly:
    def test_parsing_quality_checks_are_gone(self):
        """留着不评的指标躺在代码里，读的人会以为它们进了评价。"""
        ch = S.run_checks(page(), resp())
        for dead in ("noise_ratio", "structure_score", "tail_hit"):
            assert dead not in ch, "%s 还在 run_checks 的输出里" % dead

    def test_a_messy_but_complete_fetch_still_passes(self):
        """正文里混进导航词不再扣分 —— 那是解析质量，本轮不评。"""
        dirty = "home about careers alpha beta gamma delta epsilon"
        v, _ = verdict(page(), resp(dirty))
        assert v["verdict"] == "pass"

    def test_a_partial_fetch_of_a_long_page_still_passes(self):
        """拿到 5 个词里的 2 个（0.4 > 0.3 闸门）算抓到了 —— 本轮不评完整度。"""
        v, ch = verdict(page(), resp("alpha beta"))
        assert ch["coverage"] == 0.4
        assert v["verdict"] == "pass"


class TestHardVetoes:
    def test_wrong_page_is_lost(self):
        """退回父页在覆盖率口径下能蒙过去 —— 同一性是专门抓它的。"""
        for typ in ("baseline", "docfmt", "reliability"):
            v, _ = verdict(page(type=typ), resp("zeta eta theta iota kappa"))
            assert v["verdict"] == "lost" and v["reason"] == "wrong_page", typ
            assert v["dishonest"] is True

    def test_mojibake_is_lost(self):
        v, _ = verdict(page(), resp("alpha beta " + "\ufffd" * 5))
        assert v["verdict"] == "lost" and v["reason"] == "mojibake"

    def test_transport_failure_is_lost(self):
        v, _ = verdict(page(), resp("", status="error",
                                    failure_reason="anti_bot_blocked"))
        assert v["verdict"] == "lost" and "anti_bot_blocked" in v["reason"]


class TestContentGate:
    def test_real_content_passes(self):
        v, _ = verdict(page(), resp())
        assert v["verdict"] == "pass" and v["reason"] == "content_present"

    def test_a_stub_is_lost(self):
        p = page(gt={"anchors": []})          # 关掉同一性否决，单看闸门
        v, ch = verdict(p, resp("unrelated filler words here"))
        assert ch["coverage"] == 0.0 and v["verdict"] == "lost"

    def test_between_the_bands_is_partial(self):
        p = page(gt={"vocab": ["a%d" % i for i in range(20)], "anchors": []})
        v, _ = verdict(p, resp("a0 a1"))       # 2/20 = 0.10，介于 0.05 与 0.3 之间
        assert v["verdict"] == "partial"


class TestRenderIsFetchNotParsing:
    def test_server_shell_only_is_lost(self):
        """SPA 只拿到服务端骨架 = 没把这一页抓下来，只抓到了一个壳。"""
        v, ch = verdict(page(type="render"), resp("alpha beta gamma delta epsilon"))
        assert ch["coverage"] == 1.0 and ch["render_hit"] == 0.0
        assert v["verdict"] == "lost", "coverage 高不该救 SPA"

    def test_render_only_content_passes(self):
        v, _ = verdict(page(type="render"), resp("alpha price stock"))
        assert v["verdict"] == "pass"

    def test_no_render_anchors_goes_to_panel(self):
        v, _ = verdict(page(type="render", gt={"render_anchors": []}), resp())
        assert v["verdict"] is None and v["needs_panel"] is True


class TestAntibot:
    def test_transport_failure_is_lost_without_the_panel(self):
        v, _ = verdict(antibot_page("waf"),
                       resp("", status="error", failure_reason="anti_bot_blocked"))
        assert v["verdict"] == "lost"

    def test_anything_returned_goes_to_the_panel(self):
        """三小类"过"的定义不同，机械层给不出。"""
        v, _ = verdict(antibot_page("login_wall"), resp("some text"))
        assert v["verdict"] is None and v["needs_panel"] is True

    def test_paywall_with_a_full_article_is_flagged_not_rewarded(self):
        v, _ = verdict(antibot_page("paywall"), resp("word " * 2000))
        assert v["suspicious_bypass"] is True
        assert v["verdict"] is None, "绕过付费墙不加分"

    def test_a_wall_page_returned_as_content_is_vetoed_when_anchors_exist(self):
        p = page(type="antibot", antibot_subclass="login_wall",
                 gt={"anchors": ["headline", "byline"], "vocab": ["headline", "byline"]})
        v, _ = verdict(p, resp("Checking your browser before accessing the site"))
        assert v["verdict"] == "lost" and v["dishonest"] is True


class TestExpectedBehaviour:
    def test_error_page_returned_as_content_is_lost_and_dishonest(self):
        """把 404 页面体当正文返回比失败更坏 —— 下游不知道自己拿错了。"""
        p = page(type="reliability", expect="error", probes=["empty_thin"])
        v, _ = verdict(p, resp("404 Not Found - the requested page does not exist"))
        assert v["verdict"] == "lost" and v["dishonest"] is True

    def test_clean_error_on_an_error_url_is_pass(self):
        p = page(type="reliability", expect="error")
        v, _ = verdict(p, resp("", status="error", failure_reason="content_type_or_404"))
        assert v["verdict"] == "pass" and v["reason"] == "clean_error"

    def test_redirect_not_followed_is_lost(self):
        p = page(type="reliability", expect="redirect_final")
        v, _ = verdict(p, resp("", status="error", failure_reason="other"))
        assert v["verdict"] == "lost"

    def test_redirect_followed_is_pass(self):
        p = page(type="reliability", expect="redirect_final")
        v, _ = verdict(p, resp())
        assert v["verdict"] == "pass"


class TestGtGap:
    def test_degenerate_page_skips_mechanical_thresholds(self):
        v, _ = verdict(page(gt={"degenerate": True}), resp())
        assert v["verdict"] is None and v["needs_panel"] is True

    def test_missing_gt_goes_to_panel_not_to_zero(self):
        v, _ = verdict(page(gt={"vocab": [], "anchors": []}), resp())
        assert v["verdict"] is None and v["needs_panel"] is True

    def test_a_walled_gt_is_not_used_for_scoring(self):
        """w3.org 实测建出来的 GT 是 Cloudflare 验证页。拿它当参考，全场都被判"抓错页"。"""
        p = page(gt={"gt_gap": True, "vocab": ["just", "moment"],
                     "anchors": ["just", "moment"]})
        v, ch = verdict(p, resp("The real HTML Standard content is here"))
        assert ch["coverage"] is None and ch["identity_ok"] is None
        assert v["verdict"] is None and v["needs_panel"] is True


class TestHardVetoesAreFinal:
    def test_mojibake_never_goes_to_the_panel(self, monkeypatch):
        """实测 readability 返回 PDF 原始字节，面板只看到预览里的 `%PDF ... stream`，
        判成"内容来了"，把确定性的 lost 推翻了。"""
        called = {"n": 0}
        monkeypatch.setattr(S, "panel_verdict",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        out = S.score_one(page(), resp("alpha " + "\ufffd" * 9),
                          panel=["m1", "m2", "m3"])
        assert out["verdict"] == "lost" and out["reason"] == "mojibake"
        assert called["n"] == 0, "硬否决不该送面板复判"

    def test_wrong_page_never_goes_to_the_panel(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(S, "panel_verdict",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        out = S.score_one(page(), resp("zeta eta theta iota kappa"),
                          panel=["m1", "m2", "m3"])
        assert out["verdict"] == "lost" and out["reason"] == "wrong_page"
        assert called["n"] == 0

    def test_band_verdicts_still_go_to_the_panel(self, monkeypatch):
        """coverage 是粗代理，它判出来的 lost/partial 该让面板复核。"""
        monkeypatch.setattr(S, "panel_verdict", lambda *a, **k: {
            "verdict": "pass", "panel_split": False, "dishonest": False, "votes": {}})
        p = page(gt={"vocab": ["a%d" % i for i in range(20)], "anchors": []})
        out = S.score_one(p, resp("a0 a1"), panel=["m1", "m2", "m3"])
        assert out["verdict"] == "pass" and out["reason"] == "panel"


class TestPanel:
    def test_majority_of_three_decides(self):
        agg = S.aggregate_votes({"m1": {"verdict": "pass"}, "m2": {"verdict": "pass"},
                                 "m3": {"verdict": "partial"}})
        assert agg["verdict"] == "pass" and agg["panel_split"] is False

    def test_three_way_split_keeps_the_mechanical_verdict(self):
        agg = S.aggregate_votes({"m1": {"verdict": "pass"}, "m2": {"verdict": "partial"},
                                 "m3": {"verdict": "lost"}})
        assert agg["verdict"] is None and agg["panel_split"] is True

    def test_all_errors_is_a_split_not_a_verdict(self):
        assert S.aggregate_votes({"m%d" % i: {"verdict": "error"}
                                  for i in range(3)})["verdict"] is None

    def test_dishonest_needs_two_votes(self):
        one = {"m1": {"verdict": "lost", "dishonest": True},
               "m2": {"verdict": "lost", "dishonest": False},
               "m3": {"verdict": "lost", "dishonest": False}}
        assert S.aggregate_votes(one)["dishonest"] is False
        assert S.aggregate_votes({**one, "m2": {"verdict": "lost",
                                                "dishonest": True}})["dishonest"] is True

    def test_clean_pass_never_calls_the_panel(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(S, "panel_verdict",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        out = S.score_one(page(), resp(), panel=["m1", "m2", "m3"])
        assert out["verdict"] == "pass" and called["n"] == 0

    def test_non_clean_pass_calls_the_panel(self, monkeypatch):
        monkeypatch.setattr(S, "panel_verdict", lambda *a, **k: {
            "verdict": "partial", "panel_split": False, "dishonest": False, "votes": {}})
        out = S.score_one(antibot_page("waf"), resp("some text"),
                          panel=["m1", "m2", "m3"])
        assert out["verdict"] == "partial" and out["reason"] == "panel"

    def test_unjudged_without_a_panel_is_not_zero(self):
        out = S.score_one(antibot_page("waf"), resp("some text"), panel=None)
        assert out["verdict"] is None and "unjudged" in out["reason"]


class TestPromptAndCache:
    def test_three_rubrics_not_five(self):
        """静态文档 / 文档文件 / 健壮性问的是同一个问题（内容来了没有），
        没必要各写一份 —— 分岔多一份就多一处会漂移的措辞。"""
        assert set(S._TYPE_RUBRIC) == {"content", "render", "antibot"}
        for t in ("baseline", "docfmt", "reliability"):
            assert S.rubric_key(t) == "content"
        assert S.rubric_key("render") == "render"
        assert S.rubric_key("antibot") == "antibot"

    def test_every_rubric_states_we_are_not_grading_parsing(self):
        for t in ("baseline", "render", "antibot"):
            sysmsg = S.panel_system(t)
            assert "not grading parsing" in sysmsg
            assert "FETCH CAPABILITY" in sysmsg

    def test_every_rubric_rejects_raw_payload_as_content(self):
        """调用方要的是页面，不是文件的字节。"""
        for t in ("baseline", "render", "antibot"):
            assert "%PDF" in S.panel_system(t)
            assert "is NOT content" in S.panel_system(t)

    def test_shared_anchor_is_verbatim_in_every_rubric(self):
        for t in ("baseline", "render", "antibot"):
            assert S._SHARED_ANCHOR in S.panel_system(t)

    def test_panel_user_never_leaks_the_mechanical_verdict(self):
        p, r = page(), resp()
        u = S.panel_user(p, r, S.run_checks(p, r))
        assert "coverage" not in u.lower() and "mechanical" not in u.lower()

    def test_the_panel_is_not_fed_a_walled_reference(self):
        p = page(gt={"gt_gap": True, "vocab": ["just", "moment"], "title": "Just a moment"})
        u = S.panel_user(p, resp("real content"), {})
        assert "NO reference" in u
        assert "just, moment" not in u, "被拦时抓到的验证页词表绝不能当参考喂给面板"

    def test_cache_key_changes_with_text_rubric_and_prompt(self, monkeypatch):
        k = lambda txt, user: S.judge_cache_key({"pid": "p1"},
                                                {"provider": "o", "text": txt}, "m", user)
        assert k("x", "A") != k("y", "A")
        assert k("x", "A") != k("x", "B")
        base = k("x", "A")
        monkeypatch.setattr(S, "RUBRIC_VERSION", "fetch-v9")
        assert k("x", "A") != base

    def test_cache_roundtrips_through_disk(self, tmp_path):
        c = S.PanelCache(tmp_path / "cache.jsonl")
        c.put("k1", {"verdict": "pass"})
        assert S.PanelCache(tmp_path / "cache.jsonl").get("k1") == {"verdict": "pass"}


class TestWeakAnchors:
    """GT 缺口页从中立 SERP 补来的弱锚点：身份能判，完整度判不了。"""

    def _weak(self, **gt):
        base = {"gt_gap": True, "anchor_source": "serp",
                "anchors": ["taylor", "swift", "eras", "tour"],
                "serp_title": "Taylor Swift Tickets 2026 Eras Tour",
                "vocab": ["just", "moment", "verifying"]}
        return page(gt={**base, **gt})

    def test_weak_anchors_are_used_but_the_walled_vocab_is_not(self):
        p = self._weak()
        ch = S.run_checks(p, resp("Taylor Swift eras tour tickets on sale"))
        assert ch["identity_ok"] is True
        assert ch["coverage"] is None, "被拦时抓到的验证页文案不能当内容参考"

    def test_a_different_page_fails_identity(self):
        p = self._weak()
        ch = S.run_checks(p, resp("SeatGeek home page browse concerts sports"))
        assert ch["identity_ok"] is False

    def test_weak_identity_failure_is_not_final(self):
        """搜索引擎的标题可能过时，弱证据不该产生不可申诉的判定。"""
        p = self._weak()
        v, _ = verdict(p, resp("SeatGeek home page browse concerts sports"))
        assert v["verdict"] == "lost" and v["reason"] == "wrong_page_weak"
        assert v["final"] is False and v["needs_panel"] is True
        assert v["dishonest"] is False, "弱证据不足以扣 dishonest"

    def test_strong_identity_failure_stays_final(self):
        v, _ = verdict(page(), resp("zeta eta theta iota kappa"))
        assert v["reason"] == "wrong_page" and v["final"] is True
        assert v["dishonest"] is True

    def test_a_gap_page_without_weak_anchors_stays_blank(self):
        p = page(gt={"gt_gap": True, "vocab": ["just", "moment"], "anchors": ["just"]})
        ch = S.run_checks(p, resp("anything"))
        assert ch["coverage"] is None and ch["identity_ok"] is None

    def test_the_panel_sees_the_serp_title(self):
        u = S.panel_user(self._weak(), resp("x"), {})
        assert "Taylor Swift Tickets 2026 Eras Tour" in u
        assert "just" not in u.split("FETCH UNDER TEST")[0].lower()


class TestCrossPanel:
    def _resps(self):
        return {"octen": {"status": "ok", "text": "alpha " * 60},
                "exa": {"status": "ok", "text": "beta " * 60},
                "tavily": {"status": "error", "failure_reason": "anti_bot_blocked",
                           "text": ""}}

    def test_provider_names_never_reach_the_model(self):
        """品牌先验会漏进判定，而这些页恰恰最需要它只看内容。"""
        u, lab = S.cross_user(page(), self._resps())
        assert not any(n in u for n in ("octen", "exa", "tavily"))
        assert set(lab.values()) == {"octen", "exa", "tavily"}

    def test_label_order_is_stable_per_page_but_varies_across_pages(self):
        provs = ["a", "b", "c", "d"]
        assert S._cross_order("p001", provs) == S._cross_order("p001", list(reversed(provs)))
        assert S._cross_order("p001", provs) != S._cross_order("p002", provs)

    def test_serp_context_is_included_when_available(self):
        p = page(gt={"gt_gap": True, "anchor_source": "serp", "anchors": ["x"],
                     "serp_title": "The Real Title", "serp_snippet": "a snippet"})
        u, _ = S.cross_user(p, self._resps())
        assert "The Real Title" in u and "a snippet" in u

    def test_rubric_defaults_to_lost(self):
        """裸判偏松，所以提问方式要求"没有正面证据就判失败"。"""
        assert "Default to `lost`" in S.CROSS_SYSTEM
        assert "Length is not evidence" in S.CROSS_SYSTEM

    def test_cache_key_tracks_every_providers_text(self):
        a = S.cross_cache_key(page(), self._resps(), "m")
        changed = {**self._resps(), "exa": {"status": "ok", "text": "gamma " * 60}}
        assert S.cross_cache_key(page(), changed, "m") != a

    def test_cross_merges_per_provider_majority(self, monkeypatch):
        def fake(system, user, model, **kw):
            _, lab = S.cross_user(page(), self._resps())
            inv = {v: k for k, v in lab.items()}
            return {"verdicts": {inv["octen"]: {"verdict": "pass"},
                                 inv["exa"]: {"verdict": "lost"},
                                 inv["tavily"]: {"verdict": "lost"}}}
        monkeypatch.setattr("src.llm.call_llm_json", fake)
        out = S.panel_cross(page(), self._resps(), ["m1", "m2", "m3"])
        assert out["octen"]["verdict"] == "pass"
        assert out["exa"]["verdict"] == "lost"
        assert all(v["mode"] == "cross" for v in out.values())
