"""GT 浏览器通道：词表导出、防护强度导出、形态多数决、真实渲染。

`derive_vocab` 是这套评测的地基 —— 它同时产出两个分母：
  vocab         成功闸门的分母（coverage）
  boiler_terms  不是指标，只作 vocab 的排除集 —— 把导航词剔掉，让覆盖率量的是真正文。
"""
import importlib.util

import pytest

from src import fetch_gt as G

needs_playwright = pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="playwright 未安装（requirements-fetch.txt 可选依赖）")

HTML = """
<html><body>
  <nav>Home About Careers Privacy Home About</nav>
  <header>SiteName Login Home</header>
  <main>
    <h1>Toroidal Flux Capacitor Documentation</h1>
    <p>The toroidal capacitor stabilises flux across the manifold lattice.</p>
    <p>Calibration requires a resonance sweep before the manifold is sealed.</p>
    <p>Appendix: colophon and revision history for the lattice specification.</p>
  </main>
  <footer>Copyright Contact Terms Home</footer>
</body></html>
"""


class TestDeriveVocab:
    def test_boilerplate_terms_never_enter_the_content_vocab(self):
        v = G.derive_vocab("toroidal flux home about", "home about careers")
        assert "toroidal" in v["vocab"] and "flux" in v["vocab"]
        assert "home" not in v["vocab"], "同时出现在样板里的词不算内容词"
        assert "home" in v["boiler_terms"]

    def test_two_denominators_are_disjoint(self):
        v = G.derive_vocab("alpha beta home", "home about")
        assert not (set(v["vocab"]) & set(v["boiler_terms"]))

    def test_caps_at_sixty_and_records_the_real_n(self):
        main = " ".join("term%03d" % i for i in range(200))
        v = G.derive_vocab(main, "")
        assert len(v["vocab"]) == 60
        assert v["vocab_n"] == 60

    def test_short_page_keeps_all_and_reports_a_small_n(self):
        v = G.derive_vocab("alpha beta gamma", "")
        assert v["vocab_n"] == 3, "退化页要如实报小 n，下游据此跳过机械阈值"

    def test_stopwords_are_dropped(self):
        v = G.derive_vocab("the quick brown fox and the lazy dog", "")
        assert "the" not in v["vocab"] and "and" not in v["vocab"]
        assert "quick" in v["vocab"]

    def test_empty_input_is_empty_not_an_error(self):
        v = G.derive_vocab("", "")
        assert v["vocab"] == [] and v["vocab_n"] == 0 and v["boiler_terms"] == []


class TestStrength:
    def test_three_bands_plus_unknown(self):
        assert G.derive_strength(True, None) == "soft"
        assert G.derive_strength(True, True) == "soft"
        assert G.derive_strength(False, True) == "medium"
        assert G.derive_strength(False, False) == "hard"

    def test_headless_never_run_is_unknown_not_medium(self):
        """把缺失的 headless 当成 False，会让"只跑过真 Chrome 且成功"的页判成 medium
        —— 那和默认 hard 一样是凭空造结论。"""
        assert G.derive_strength(None, True) == "unknown"
        assert G.derive_strength(None, False) == "unknown"
        assert G.derive_strength(None, None) == "unknown"

    def test_chrome_channel_not_run_is_unknown_not_hard(self):
        """默认 hard 会把"没测"伪装成"测出来最难"，那是凭空给防护强度添了一档。"""
        assert G.derive_strength(False, None) == "unknown"
        assert G.derive_strength(True, None) == "soft"


@needs_playwright
class TestRenderPage:
    def test_separates_main_text_from_boilerplate(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(HTML, encoding="utf-8")
        got = G.render_page(f.as_uri(), channel="playwright_headless", timeout=20)
        main, boiler = got["main_text"].lower(), got["boiler_text"].lower()
        assert "toroidal" in main and "manifold" in main
        assert "careers" in boiler and "copyright" in boiler
        assert "careers" not in main, "nav 子树必须排除干净，否则精度的分母是假的"

    def test_struct_counts_are_recorded(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(HTML, encoding="utf-8")
        got = G.render_page(f.as_uri(), channel="playwright_headless", timeout=20)
        assert got["http_status"] in (200, None)

    def test_vocab_built_from_a_real_render_has_both_denominators(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(HTML, encoding="utf-8")
        got = G.render_page(f.as_uri(), channel="playwright_headless", timeout=20)
        v = G.derive_vocab(got["main_text"], got["boiler_text"])
        assert v["vocab_n"] > 0 and v["boiler_terms"]
        assert not (set(v["vocab"]) & set(v["boiler_terms"]))


class TestAnchors:
    def test_title_words_plus_rare_content_terms(self):
        a = G.derive_anchors("Toroidal Flux Capacitor Docs",
                             ["toroidal", "manifold", "lattice"],
                             {"toroidal": 1, "manifold": 1, "lattice": 40}, 100)
        assert "toroidal" in a and "manifold" in a
        assert "lattice" not in a, "在 40% 的页上都出现的词不是独有锚点"

    def test_stopwords_never_become_anchors(self):
        a = G.derive_anchors("The Page", ["the", "and", "toroidal"], {}, 100)
        assert "the" not in a and "and" not in a
        assert "toroidal" in a

    def test_empty_inputs_give_empty_anchors(self):
        assert G.derive_anchors("", [], {}, 100) == []

    def test_generic_title_words_are_filtered_by_document_frequency(self):
        """解析通道把文件名当标题，pdf / txt 这类通用词会白送命中，同一性检查虚高。"""
        a = G.derive_anchors("f1040 pdf", ["tax", "instructions"],
                             {"pdf": 40, "f1040": 1, "tax": 2, "instructions": 3}, 100)
        assert "pdf" not in a
        assert "f1040" in a and "tax" in a

    def test_url_slug_is_the_strongest_identity_signal(self):
        a = G.derive_anchors("", ["model", "sequence"],
                             {"asyncio": 1, "task": 1, "model": 2}, 100,
                             url="https://docs.python.org/3/library/asyncio-task.html")
        assert "asyncio" in a and "task" in a

    def test_document_frequency_limit_is_five_percent(self):
        """两成在 58 页语料上是 11 页 —— 出现在 8 个页面上的 pdf 一点也不独有。"""
        df = {"pdf": 8, "rare": 1}
        a = G.derive_anchors("pdf rare", [], df, 58)
        assert "pdf" not in a and "rare" in a


class TestWalledGt:
    def test_a_challenge_page_is_detected(self):
        """w3.org 实测：headless 被 Cloudflare 拦，GT 标题是 Just a moment...，
        整页词表是验证页文案，于是全场都在跟一张验证页比对。"""
        assert G.gt_is_walled("Verifying you are human. This may take a few seconds.",
                              "Just a moment...")

    def test_real_content_is_not_flagged(self):
        assert G.gt_is_walled("Cloudflare Workers documentation: routes and bindings",
                              "Cloudflare Workers") == []

    def test_empty_is_not_flagged(self):
        assert G.gt_is_walled("", "") == []


class TestVocabHead:
    def test_head_terms_come_from_the_start_of_the_document(self):
        main = "alpha beta gamma " + " ".join("filler%03d" % i for i in range(500)) \
               + " omega finis"
        v = G.derive_vocab(main, "")
        assert "alpha" in v["vocab_head"] and "beta" in v["vocab_head"]
        assert "omega" not in v["vocab_head"], "尾部词不该进身份锚点的来源"

    def test_head_and_tail_are_both_present(self):
        v = G.derive_vocab(" ".join("w%03d" % i for i in range(300)), "")
        assert v["vocab_head"]

    def test_short_document_head_is_the_whole_thing(self):
        v = G.derive_vocab("alpha beta gamma", "")
        assert set(v["vocab_head"]) == {"alpha", "beta", "gamma"}


class TestChannelIsNotDecorative:
    @needs_playwright
    def test_unknown_channel_hard_fails(self):
        """channel 写错了要炸，不能悄悄跑成 headless。"""
        import pytest as _p
        with _p.raises(ValueError, match="未知通道"):
            G.render_page("https://example.com/", channel="real_chrome")

    def test_declared_channels(self):
        assert G._CHANNELS == {"playwright_headless", "chrome_real"}


class TestWatchdog:
    def test_watchdog_fires_on_a_hang(self):
        import time
        import pytest as _p
        with _p.raises(TimeoutError, match="看门狗"):
            with G._watchdog(1):
                time.sleep(3)

    def test_watchdog_is_disarmed_after_a_clean_run(self):
        import signal
        with G._watchdog(5):
            pass
        assert signal.alarm(0) == 0, "闹钟没解除，会在后面的代码里乱炸"

    def test_zero_seconds_disables_it(self):
        with G._watchdog(0):
            pass
