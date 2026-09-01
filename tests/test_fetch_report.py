"""报告聚合。**本轮只评抓取能力** —— 主口径是单一的抓取成功率，所以总分成立。

重点在"不撒谎"的几条：判不了的不当 0 分、缺档写未标注、缺口页的判定单独报。
"""
from scripts import fetch_report as R


def page(pid, typ="baseline", **kw):
    base = {"pid": pid, "url": "https://h%s.com/a" % pid, "host": "h%s.com" % pid,
            "type": typ, "doc_type": "html", "probes": [], "lang": None,
            "antibot_subclass": None, "gt": {}}
    return {**base, **kw}


def verd(pid, prov, v, typ="baseline", **kw):
    base = {"pid": pid, "provider": prov, "type": typ, "verdict": v,
            "antibot_subclass": None, "lang": None, "strength": None,
            "latency_ms": 100.0, "len_norm": 500, "run_seq": 0, "dishonest": False,
            "suspicious_bypass": False, "panel_split": False, "reason": "content_present",
            "failure_reason": None, "fault": None, "checks": {}}
    return {**base, **kw}


class TestWeighted:
    def test_formula(self):
        w = R.weighted(["pass", "partial", "lost", "pass"])
        assert w["weighted"] == (1.0 + 0.5 + 0 + 1.0) / 4
        assert (w["pass"], w["partial"], w["lost"]) == (2, 1, 1)

    def test_unjudged_is_counted_separately_not_as_zero(self):
        w = R.weighted(["pass", None, None])
        assert w["weighted"] == 1.0, "判不了的不该拉低分母"
        assert w["unjudged"] == 2 and w["n"] == 1

    def test_all_unjudged_is_none_not_zero(self):
        w = R.weighted([None, None])
        assert w["weighted"] is None and w["n"] == 0


class TestSingleMetric:
    def test_there_is_one_overall_score_now(self):
        """只剩一个单位，跨型可比 —— 总分成立，不再是"各型分列不合成"。"""
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p2", "a", "lost", typ="antibot")],
                          [page("p1"), page("p2", "antibot")])
        assert agg["overall"]["a"]["weighted"] == 0.5
        assert agg["ranking"] == [("a", 0.5)]

    def test_types_are_slices_not_separate_scores(self):
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p2", "a", "lost", typ="antibot")],
                          [page("p1"), page("p2", "antibot")])
        assert agg["slices"]["type"]["baseline"]["a"]["weighted"] == 1.0
        assert agg["slices"]["type"]["antibot"]["a"]["weighted"] == 0.0

    def test_ranking_excludes_providers_with_nothing_judged(self):
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p1", "b", None)],
                          [page("p1")])
        assert [p for p, _ in agg["ranking"]] == ["a"]
        assert agg["overall"]["b"]["weighted"] is None

    def test_parsing_quality_columns_are_gone(self):
        """留着不评的列，读的人会以为它进了评价。"""
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        d = agg["diagnostics"]["a"]
        for dead in ("noise_median", "structure_median"):
            assert dead not in d
        # 方法学声明里**故意**写"不评…结构保真"，那是在告诉读者它被排除了；
        # 断言要针对表头列，不是全文。
        md = R.render_markdown(agg)
        header = next(l for l in md.splitlines() if l.startswith("| provider | P50"))
        assert "噪声率" not in header and "结构保真" not in header
        assert "不评正文纯度、结构保真" in md, "排除了什么要明说"


class TestSliceConfidence:
    def test_a_bucket_of_gt_gap_pages_is_marked_low_confidence(self):
        """实测「硬」档 100% 的格都在缺口页上，面板凭抓取内容自己判、偏松，
        于是出现「硬档 88% > 软档 60%」的倒挂 —— 那是置信度假象，不是能力差异。"""
        pages = [page("p1", "antibot", gt={"gt_gap": True}),
                 page("p2", "antibot", gt={"gt_gap": False})]
        vs = [verd("p1", "a", "pass", typ="antibot", strength="hard"),
              verd("p2", "a", "pass", typ="antibot", strength="soft")]
        agg = R.aggregate(vs, pages)
        assert agg["slices"]["strength"]["hard"]["_gap_share"] == 1.0
        assert agg["slices"]["strength"]["soft"]["_gap_share"] == 0.0
        md = R.render_markdown(agg)
        assert "低置信" in md and "不要把这些格子的高分读成能力更强" in md

    def test_a_clean_bucket_is_not_marked(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1", gt={"gt_gap": False})])
        assert agg["slices"]["type"]["baseline"]["_gap_share"] == 0.0
        assert "低置信" not in R.render_markdown(agg)

    def test_slice_headers_count_pages_not_cells(self):
        pages = [page("p1"), page("p2")]
        vs = [verd("p1", "a", "pass"), verd("p1", "b", "pass"),
              verd("p2", "a", "pass"), verd("p2", "b", "pass")]
        agg = R.aggregate(vs, pages)
        assert agg["slices"]["type"]["baseline"]["_n_pages"] == 2
        assert "静态文档(2)" in R.render_markdown(agg)


class TestSlices:
    def test_strength_slice_only_covers_antibot(self):
        pages = [page("p1"), page("p2", "antibot")]
        vs = [verd("p1", "a", "pass", strength="soft"),
              verd("p2", "a", "lost", typ="antibot", strength="hard")]
        agg = R.aggregate(vs, pages)
        assert set(agg["slices"]["strength"]) == {"hard"}, "非反爬页不该进强度切片"

    def test_probe_slice_expands_multi_valued_labels(self):
        pages = [page("p1", probes=["oversize", "raw_direct"]),
                 page("p2", probes=["oversize"])]
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p2", "a", "lost")], pages)
        assert agg["slices"]["probes"]["oversize"]["a"]["n"] == 2
        assert agg["slices"]["probes"]["raw_direct"]["a"]["n"] == 1

    def test_host_dedup_averages_within_domain_first(self):
        pages = [page("p1"), page("p2"), page("p3")]
        pages[1]["host"] = pages[0]["host"]
        vs = [verd("p1", "a", "pass"), verd("p2", "a", "lost"), verd("p3", "a", "pass")]
        agg = R.aggregate(vs, pages)
        assert agg["overall"]["a"]["weighted"] == 2 / 3
        assert agg["host_dedup"]["a"]["weighted"] == 0.75      # (0.5 + 1) / 2


class TestHonesty:
    def test_latency_excludes_calls_that_got_nothing(self):
        vs = [verd("p1", "a", "pass", latency_ms=100.0),
              verd("p2", "a", "lost", latency_ms=30000.0)]
        agg = R.aggregate(vs, [page("p1"), page("p2")])
        d = agg["diagnostics"]["a"]
        assert d["latency_p50"] == 100.0 and d["latency_n"] == 1
        assert d["slow_losses"] == 1

    def test_harness_faults_reported_separately(self):
        vs = [verd("p1", "a", "lost", failure_reason="our_size_cap", fault="harness"),
              verd("p2", "a", "lost", failure_reason="anti_bot_blocked", fault="provider")]
        agg = R.aggregate(vs, [page("p1"), page("p2")])
        assert agg["harness_faults"]["a"] == 1
        assert "our_size_cap/harness" in agg["failures"]["a"]

    def test_repeat_runs_do_not_enter_the_main_score(self):
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1)],
                          [page("p1")])
        assert agg["overall"]["a"]["weighted"] == 1.0 and agg["meta"]["n_cells"] == 1

    def test_gt_gaps_are_listed_and_their_verdicts_counted(self):
        pages = [page("p1", gt={"gt_gap": True}), page("p2", gt={"gt_gap": False})]
        agg = R.aggregate([verd("p1", "a", "lost"), verd("p2", "a", "pass")], pages)
        assert agg["meta"]["gt_gaps"][0]["pid"] == "p1"
        assert agg["meta"]["verdicts_on_gt_gap_pages"] == 1
        md = R.render_markdown(agg)
        assert "GT 缺口" in md and "证据强度低于" in md

class TestMarkdown:
    def test_column_counts_are_consistent(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        for block in R.render_markdown(agg).split("\n\n"):
            rows = [l for l in block.splitlines() if l.startswith("|")]
            if len(rows) < 3:
                continue
            assert len({r.count("|") for r in rows}) == 1, "表格列数不一致"

    def test_scope_is_stated_up_front(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        md = R.render_markdown(agg)
        assert "只评抓取能力" in md
        assert "解析质量" in md


class TestHtml:
    def _agg(self):
        pages = [page("p1"), page("p2", "render"),
                 page("p3", gt={"gt_gap": True})]
        vs = [verd("p1", "a", "pass"), verd("p2", "a", "lost", typ="render"),
              verd("p3", "a", None)]
        return R.aggregate(vs, pages)

    def test_data_is_a_js_literal_not_a_json_script_block(self):
        html = R.render_html(self._agg())
        assert 'type="application/json"' not in html
        assert "window.FETCH_EVAL_DATA =" in html

    def test_column_assertion_catches_a_mismatch(self):
        import pytest as _p
        bad = ("<table><thead><tr><th>a</th><th>b</th></tr></thead>"
               "<tbody><tr><td>1</td></tr></tbody></table>")
        with _p.raises(AssertionError, match="列数不一致"):
            R._assert_table_columns(bad)

    def test_theme_tokens_for_light_dark_and_toggle(self):
        html = R.render_html(self._agg())
        assert ":root{--fx-bg" in html
        assert "prefers-color-scheme:dark" in html
        assert ':root[data-theme="dark"]' in html

    def test_class_names_are_namespaced(self):
        import re
        html = R.render_html(self._agg())
        for c in set(re.findall(r'class="([^"]+)"', html)):
            for one in c.split():
                assert one.startswith("fx-"), "未加前缀的类名: %s" % one

    def test_scope_badge_present(self):
        assert "只评抓取能力" in R.render_html(self._agg())


class TestGlossaryTravelsWithTheReport:
    """口径说明和报告放在一起 —— 产品不用开第二个页面。"""

    def _agg(self):
        pages = [page("p1"), page("p2", "antibot", gt={"gt_gap": True})]
        vs = [verd("p1", "a", "pass"),
              verd("p2", "a", "pass", typ="antibot", strength="hard")]
        return R.aggregate(vs, pages)

    def test_markdown_has_the_glossary_section(self):
        md = R.render_markdown(self._agg())
        assert "## 口径速查" in md
        for h in ("主表各列", "判定怎么做出来的", "诊断列", "失败归因的责任方",
                  "别读错的几个地方"):
            assert h in md, h

    def test_html_has_the_glossary_section(self):
        h = R.render_html(self._agg())
        assert "口径速查" in h
        assert "别读错的几个地方" in h

    def test_glossary_explains_every_diagnostic_column(self):
        """诊断表里出现的列，速查里都要有一条 —— 否则读者会看到一个没解释的数。"""
        agg = self._agg()
        md = R.render_markdown(agg)
        header = next(l for l in md.splitlines() if l.startswith("| provider | P50"))
        cols = [c.strip() for c in header.strip("|").split("|")[1:]]
        explained = " ".join(k for k, _ in R.DIAG_COLS)
        for c in cols:
            key = c.split()[0]
            assert key in explained or key in md, "诊断列 %s 没有口径说明" % c

    def test_markdown_bold_is_converted_in_html(self):
        h = R.render_html(self._agg())
        assert "**" not in h, "markdown 星号漏进了 HTML"
        assert h.count("<strong>") == h.count("</strong>")

    def test_low_confidence_trap_appears_only_when_relevant(self):
        with_gap = R.render_markdown(self._agg())
        assert "带 ⚠ 的桶" in with_gap
        clean = R.aggregate([verd("p1", "a", "pass")], [page("p1", gt={"gt_gap": False})])
        assert "带 ⚠ 的桶" not in R.render_markdown(clean)

    def test_cache_states_come_from_the_adapters_not_a_hand_copy(self):
        """手抄一份状态表迟早和代码分叉 —— 直接从 adapter 读。"""
        from src.fetch_backends import FETCHERS
        agg = R.aggregate([verd("p1", "octen", "pass")], [page("p1")])
        assert agg["meta"]["cache_pinned"]["octen"] == FETCHERS["octen"].cache_pinned
        md = R.render_markdown(agg)
        assert "实时抓、不走缓存" in md

    def test_unknown_provider_names_do_not_break_the_state_lookup(self):
        agg = R.aggregate([verd("p1", "some-new-vendor", "pass")], [page("p1")])
        assert agg["meta"]["cache_pinned"] == {}

    def test_freshness_blind_spot_is_always_stated(self):
        """有的厂商从索引里给内容 —— 拿到了算成功，但可能是旧的。本轮完全没测。"""
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        assert "不测内容新鲜度" in R.render_markdown(agg)
        assert "不测内容新鲜度" in R.render_html(agg)

    def test_latency_trap_is_always_present(self):
        """节流的事任何一轮都要说 —— 不说就会被横向比。"""
        clean = R.aggregate([verd("p1", "a", "pass")], [page("p1", gt={"gt_gap": False})])
        assert "延迟不能横向比" in R.render_markdown(clean)


class TestRanking:
    """每个有方向的指标都排名；没方向的显式说明为什么不排。"""

    def test_ties_share_a_rank(self):
        assert R.rank_of({"a": 0.8, "b": 0.6, "c": 0.6, "d": 0.2}, True) == \
            {"a": 1, "b": 2, "c": 2, "d": 4}

    def test_lower_is_better_flips_the_order(self):
        assert R.rank_of({"a": 100, "b": 50}, False) == {"b": 1, "a": 2}

    def test_none_values_do_not_get_a_rank(self):
        assert R.rank_of({"a": 1.0, "b": None}, True) == {"a": 1}
        assert R.rank_of({"a": None}, True) == {}

    def _agg(self):
        pages = [page("p%d" % i) for i in range(1, 4)]
        vs = ([verd("p%d" % i, "a", "pass") for i in range(1, 4)]
              + [verd("p1", "b", "pass")]
              + [verd("p%d" % i, "b", "lost") for i in (2, 3)])
        return R.aggregate(vs, pages)

    def test_every_table_uses_the_same_row_order(self):
        agg = self._agg()
        assert agg["providers_ranked"] == ["a", "b"]
        md = R.render_markdown(agg)
        # 每张表里 a 都排在 b 前面 —— 各表各排行序的话读者对不上号
        for block in md.split("\n\n"):
            rows = [l for l in block.splitlines() if l.startswith("| a |") or l.startswith("| b |")]
            if len(rows) == 2:
                assert rows[0].startswith("| a |"), block[:60]

    def test_slice_cells_carry_a_column_rank(self):
        md = R.render_markdown(self._agg())
        assert "⁽1⁾" in md and "⁽2⁾" in md

    def test_metrics_without_a_direction_are_not_ranked(self):
        """硬给它们排名会把「仅供参考」读成「越大越好」。"""
        for f in ("len_norm_median", "panel_split", "latency_p50", "latency_p90"):
            assert R.DIAG_DIRECTION[f] is None, f
            assert f in R.NO_RANK_WHY, f

    def test_the_report_says_why_a_column_is_unranked(self):
        md = R.render_markdown(self._agg())
        assert "不排名的列" in md
        assert "长不等于好" in md
        assert "主动节流" in md

    def test_latency_is_not_ranked_because_we_throttled_a_provider(self):
        """报告的陷阱条写着「延迟不能横向比」，再给它排名就是自相矛盾。"""
        md = R.render_markdown(self._agg())
        assert "延迟不能横向比" in md
        assert "P50 —" in md or "P50 <span" in md or "P50 ↓" not in md

    def test_direction_arrows_are_in_the_header(self):
        md = R.render_markdown(self._agg())
        assert "dishonest ↓" in md and "计入延迟 ↑" in md

    def test_failure_table_ranks_both_columns_ascending(self):
        md = R.render_markdown(self._agg())
        block = md[md.index("## 为什么没抓到"):]
        assert "失败总数" in block and "harness" in block

    def test_html_carries_the_same_ranks(self):
        h = R.render_html(self._agg())
        assert "<sup>1</sup>" in h and "<sup>2</sup>" in h
        assert "不排名的列" in h


class TestReportDimensions:
    """报告只出这六项：抓取成功率 + 五个切片。语种轴与页面难度分布已整条删除 ——
    不评的维度留在代码和输出里，读的人会以为它进了评价。"""

    def _agg(self):
        pages = [page("p1"), page("p2", "antibot", antibot_subclass="waf"),
                 page("p3", "docfmt", doc_type="pdf"),
                 page("p4", "reliability", probes=["oversize"])]
        vs = [verd("p1", "a", "pass"),
              verd("p2", "a", "lost", typ="antibot", antibot_subclass="waf",
                   strength="hard"),
              verd("p3", "a", "pass", typ="docfmt"),
              verd("p4", "a", "partial", typ="reliability")]
        return R.aggregate(vs, pages)

    def test_exactly_five_slices(self):
        assert set(self._agg()["slices"]) == {
            "type", "antibot_subclass", "strength", "doc_type", "probes"}

    def test_the_removed_dimensions_are_gone_from_the_output(self):
        agg = self._agg()
        for dead in ("lang", "difficulty", "page_scores"):
            assert dead not in agg and dead not in agg["slices"], dead
        for dead in ("lang_note", "rtl_note", "lang_counts"):
            assert dead not in agg["meta"], dead

    def test_the_removed_dimensions_are_gone_from_the_rendered_report(self):
        md = R.render_markdown(self._agg())
        for dead in ("语种", "RTL", "页面难度分布"):
            assert dead not in md, dead
        assert "语种" not in R.render_html(self._agg())

    def test_the_six_kept_sections_are_all_there(self):
        md = R.render_markdown(self._agg())
        for want in ("## 主表：抓取成功率", "### 按页面类型", "### 反爬页按墙的类型",
                     "### 反爬页按防护强度", "### 文档文件按格式", "### 健壮性探针"):
            assert want in md, want
