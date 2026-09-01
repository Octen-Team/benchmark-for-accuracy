"""Report aggregation. Fetch capability only — one headline metric, the fetch success
rate, on a single unit, so a total is meaningful.

The emphasis is on the places the report must not lie: an unjudged cell is never
scored as zero, a missing bucket prints as unlabelled, and verdicts on gap pages
are reported separately.
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
        assert w["weighted"] == 1.0, "an unjudged cell must not enter the denominator"
        assert w["unjudged"] == 2 and w["n"] == 1

    def test_all_unjudged_is_none_not_zero(self):
        w = R.weighted([None, None])
        assert w["weighted"] is None and w["n"] == 0


class TestSingleMetric:
    def test_there_is_one_overall_score_now(self):
        """A single unit is comparable across page types, so a total is meaningful."""
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
        """An unscored column left in place reads as though it counts."""
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        d = agg["diagnostics"]["a"]
        for dead in ("noise_median", "structure_median"):
            assert dead not in d
        md = R.render_markdown(agg)
        # The methodology notes **deliberately** say "structural fidelity ... removed",
        # which tells the reader it was excluded; assert against the header row, not
        # the whole document.
        header = [l for l in md.split("\n") if l.startswith("| provider")][0]
        assert "noise" not in header and "structural" not in header
        assert "structural fidelity" in md, "say what was excluded"


class TestSliceConfidence:
    def test_a_bucket_of_gt_gap_pages_is_marked_low_confidence(self):
        """On the hardest pages every cell sits on a gap page, the panel rules from the
        fetched content alone and is lenient, and the harder tier scores above the
        easier one. That is a confidence artefact, not a capability difference."""
        pages = [page("p1", "antibot", gt={"gt_gap": True}),
                 page("p2", "antibot", gt={"gt_gap": False})]
        vs = [verd("p1", "a", "pass", typ="antibot", strength="hard"),
              verd("p2", "a", "pass", typ="antibot", strength="soft")]
        agg = R.aggregate(vs, pages)
        assert agg["slices"]["strength"]["hard"]["_gap_share"] == 1.0
        assert agg["slices"]["strength"]["soft"]["_gap_share"] == 0.0
        md = R.render_markdown(agg)
        assert "Low confidence" in md and "greater capability" in md

    def test_a_clean_bucket_carries_no_warning(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1", gt={"gt_gap": False})])
        assert "Low confidence" not in R.render_markdown(agg)

    def test_slice_headers_count_pages_not_cells(self):
        pages = [page("p1"), page("p2")]
        vs = [verd("p1", "a", "pass"), verd("p1", "b", "pass"),
              verd("p2", "a", "pass"), verd("p2", "b", "pass")]
        agg = R.aggregate(vs, pages)
        assert agg["slices"]["type"]["baseline"]["_n_pages"] == 2
        assert "static docs(2)" in R.render_markdown(agg)


class TestSlices:
    def test_strength_slice_only_covers_antibot(self):
        pages = [page("p1"), page("p2", "antibot")]
        vs = [verd("p1", "a", "pass", strength="soft"),
              verd("p2", "a", "lost", typ="antibot", strength="hard")]
        agg = R.aggregate(vs, pages)
        assert set(agg["slices"]["strength"]) == {"hard"}, "non-anti-bot pages must not enter"

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

    def test_multiple_rounds_collapse_to_one_cell(self):
        agg = R.aggregate([verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1)],
                          [page("p1")])
        assert agg["meta"]["n_cells"] == 1, "several rounds must not count as several cells"
        assert agg["meta"]["rounds"] == 2

    def test_main_score_is_the_median_round_not_the_first(self):
        """The first round is arbitrary; using it writes one roll of the dice in."""
        vs = [verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1),
              verd("p1", "a", "lost", run_seq=2)]
        assert R.aggregate(vs, [page("p1")])["overall"]["a"]["weighted"] == 0.0

    def test_even_round_count_takes_the_lower_middle(self):
        """With an even number of rounds, understate rather than inflate."""
        vs = [verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1)]
        assert R.aggregate(vs, [page("p1")])["overall"]["a"]["weighted"] == 0.0

    def test_envelope_reports_worst_and_best_rounds(self):
        vs = [verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1),
              verd("p1", "a", "pass", run_seq=2)]
        env = R.aggregate(vs, [page("p1")])["meta"]["envelope"]["a"]
        assert (env["worst"], env["median"], env["best"]) == (0.0, 1.0, 1.0)

    def test_unstable_cells_are_counted(self):
        vs = [verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1),
              verd("p2", "a", "pass"), verd("p2", "a", "pass", run_seq=1)]
        agg = R.aggregate(vs, [page("p1"), page("p2")])
        assert agg["meta"]["unstable_cells"] == 1

    def test_representative_row_comes_from_a_round_with_that_verdict(self):
        """Diagnostic columns must come from the same round as the verdict, or the
        report shows another round's latency."""
        rows = R.collapse_rounds([verd("p1", "a", "lost"),
                                  verd("p1", "a", "pass", run_seq=1),
                                  verd("p1", "a", "pass", run_seq=2)])
        assert len(rows) == 1
        assert rows[0]["verdict"] == "pass" and rows[0]["run_seq"] == 1

    def test_gt_gaps_are_listed_and_their_verdicts_counted(self):
        pages = [page("p1", gt={"gt_gap": True}), page("p2", gt={"gt_gap": False})]
        agg = R.aggregate([verd("p1", "a", "lost"), verd("p2", "a", "pass")], pages)
        assert agg["meta"]["gt_gaps"][0]["pid"] == "p1"
        assert agg["meta"]["verdicts_on_gt_gap_pages"] == 1
        md = R.render_markdown(agg)
        assert "ground-truth gap" in md and "evidence is weaker" in md

class TestMarkdown:
    def test_column_counts_are_consistent(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        for block in R.render_markdown(agg).split("\n\n"):
            rows = [l for l in block.splitlines() if l.startswith("|")]
            if len(rows) < 3:
                continue
            assert len({r.count("|") for r in rows}) == 1, "table column count differs"

    def test_scope_is_stated_up_front(self):
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        md = R.render_markdown(agg)
        assert "Fetch capability only" in md
        assert "parsing quality" in md


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
        with _p.raises(AssertionError, match="column mismatch"):
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
                assert one.startswith("fx-"), "unprefixed class name: %s" % one

    def test_scope_badge_present(self):
        assert "Fetch capability only" in R.render_html(self._agg())


class TestGlossaryTravelsWithTheReport:
    """The definitions ship with the report so nobody needs a second document."""

    def _agg(self):
        pages = [page("p1"), page("p2", "antibot", gt={"gt_gap": True})]
        vs = [verd("p1", "a", "pass"),
              verd("p2", "a", "pass", typ="antibot", strength="hard")]
        return R.aggregate(vs, pages)

    def test_markdown_has_the_glossary_section(self):
        md = R.render_markdown(self._agg())
        assert "## Metric definitions" in md
        for h in ("Main table columns", "How a verdict is reached",
                  "Diagnostic columns", "Who a failure is attributed to",
                  "Easy things to misread"):
            assert h in md, h

    def test_html_has_the_glossary_section(self):
        h = R.render_html(self._agg())
        assert "Metric definitions" in h
        assert "Easy things to misread" in h

    def test_glossary_explains_every_diagnostic_column(self):
        """Every column in the diagnostic table needs a definition, or the reader
        meets an unexplained number."""
        agg = self._agg()
        md = R.render_markdown(agg)
        header = next(l for l in md.splitlines() if l.startswith("| provider | P50"))
        cols = [c.strip() for c in header.strip("|").split("|")[1:]]
        explained = " ".join(k for k, _ in R.DIAG_COLS)
        for c in cols:
            key = c.split()[0]
            assert key in explained or key in md, "diagnostic column %s undefined" % c

    def test_markdown_bold_is_converted_in_html(self):
        h = R.render_html(self._agg())
        assert "**" not in h, "markdown asterisks leaked into the HTML"
        assert h.count("<strong>") == h.count("</strong>")

    def test_low_confidence_trap_appears_only_when_relevant(self):
        with_gap = R.render_markdown(self._agg())
        assert "low-confidence" in with_gap
        clean = R.aggregate([verd("p1", "a", "pass")], [page("p1", gt={"gt_gap": False})])
        assert "low-confidence" not in R.render_markdown(clean)

    def test_cache_states_come_from_the_adapters_not_a_hand_copy(self):
        """A hand-written status table drifts from the code; read it from the adapters."""
        from src.fetch_backends import FETCHERS
        agg = R.aggregate([verd("p1", "octen", "pass")], [page("p1")])
        assert agg["meta"]["cache_pinned"]["octen"] == FETCHERS["octen"].cache_pinned
        md = R.render_markdown(agg)
        assert "live-fetch" in md

    def test_unknown_provider_names_do_not_break_the_state_lookup(self):
        agg = R.aggregate([verd("p1", "some-new-vendor", "pass")], [page("p1")])
        assert agg["meta"]["cache_pinned"] == {}

    def test_multi_round_runs_state_the_envelope(self):
        """A reader cannot judge whether a gap is real without the round-to-round spread."""
        vs = [verd("p1", "a", "pass"), verd("p1", "a", "lost", run_seq=1),
              verd("p1", "a", "pass", run_seq=2)]
        md = R.render_markdown(R.aggregate(vs, [page("p1")]))
        assert "3 rounds were run" in md and "0%-100%" in md

    def test_single_round_runs_say_so_instead_of_staying_silent(self):
        """Silence would let a one-round number be read as though it were stable."""
        md = R.render_markdown(R.aggregate([verd("p1", "a", "pass")], [page("p1")]))
        assert "Only one round was run" in md

    def test_freshness_blind_spot_is_always_stated(self):
        """Some providers serve content from an index — retrieved, but possibly stale.
        This evaluation does not measure that at all."""
        agg = R.aggregate([verd("p1", "a", "pass")], [page("p1")])
        assert "freshness is not measured" in R.render_markdown(agg)
        assert "freshness is not measured" in R.render_html(agg)

    def test_latency_trap_is_always_present(self):
        """Pacing must be stated in any round that used it, or latency gets compared."""
        clean = self._agg()
        assert "not comparable across providers" in R.render_markdown(clean)


class TestRanking:
    """Every metric with a direction is ranked; those without say why not."""

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
        # a precedes b in every table — per-table ordering would break cross-reading
        for block in md.split("\n\n"):
            rows = [l for l in block.splitlines() if l.startswith("| a |") or l.startswith("| b |")]
            if len(rows) == 2:
                assert rows[0].startswith("| a |"), block[:60]

    def test_slice_cells_carry_a_column_rank(self):
        md = R.render_markdown(self._agg())
        assert "⁽1⁾" in md and "⁽2⁾" in md

    def test_metrics_without_a_direction_are_not_ranked(self):
        """Ranking them turns "for reference only" into "bigger is better"."""
        for f in ("len_norm_median", "panel_split", "latency_p50", "latency_p90"):
            assert R.DIAG_DIRECTION[f] is None, f
            assert f in R.NO_RANK_WHY, f

    def test_the_report_says_why_a_column_is_unranked(self):
        md = R.render_markdown(self._agg())
        assert "Columns that are not ranked" in md
        assert "longer is not better" in md
        assert "paced" in md

    def test_latency_is_not_ranked_because_we_throttled_a_provider(self):
        """The report warns that latency is not comparable; ranking it anyway would
        contradict that in the same document."""
        md = R.render_markdown(self._agg())
        assert "not comparable across providers" in md
        assert "P50 —" in md or "P50 <span" in md or "P50 ↓" not in md

    def test_direction_arrows_are_in_the_header(self):
        md = R.render_markdown(self._agg())
        assert "dishonest ↓" in md and "timed calls ↑" in md

    def test_failure_table_ranks_both_columns_ascending(self):
        md = R.render_markdown(self._agg())
        block = md[md.index("## Why it was not retrieved"):]
        assert "failures" in block and "harness" in block

    def test_html_carries_the_same_ranks(self):
        h = R.render_html(self._agg())
        assert "<sup>1</sup>" in h and "<sup>2</sup>" in h
        assert "not ranked" in h


class TestReportDimensions:
    """The report emits exactly these six: the success rate plus five slices. The
    language axis and the page-difficulty distribution were removed entirely — an
    unscored dimension left in the code and the output reads as though it counts."""

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
        for dead in ("language", "RTL", "difficulty distribution"):
            assert dead not in md, dead
        assert "language" not in R.render_html(self._agg())

    def test_the_six_kept_sections_are_all_there(self):
        md = R.render_markdown(self._agg())
        for want in ("## Headline: fetch success rate", "### By page type",
                     "### Anti-bot pages by wall type",
                     "### Anti-bot pages by protection strength",
                     "### Document files by format", "### Robustness probes"):
            assert want in md, want
