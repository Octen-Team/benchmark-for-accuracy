"""Distribution and threshold assertions for fetch_spec.

Each assertion here exists to catch one specific way the config can drift; a test that
never fails when the thing it guards breaks is worse than no test.
"""
import pytest

from src import fetch_spec as S


def _valid_rows():
    rows = []
    for t, n in S.TYPE_COUNTS.items():
        for i in range(n):
            rows.append({"pid": f"{t}{i}", "type": t, "probes": [],
                         "expect": "content", "defended": t == "antibot"})
    return rows


def test_five_types_sum_to_100():
    assert set(S.TYPE_COUNTS) == {"baseline", "render", "docfmt", "antibot", "reliability"}
    assert S.TYPE_COUNTS == {"baseline": 8, "render": 18, "docfmt": 22,
                             "antibot": 42, "reliability": 10}
    assert sum(S.TYPE_COUNTS.values()) == 100


def test_every_csv_category_maps_to_a_type():
    assert set(S.CATEGORY_TO_TYPE) == {
        "Static Docs", "E-commerce/SPA", "Documents",
        "Social/Login", "Defended (WAF/Reviews/Paywall)", "Robustness",
    }
    assert set(S.CATEGORY_TO_TYPE.values()) <= set(S.TYPE_COUNTS)


def test_antibot_subclasses_are_the_three_declared():
    assert set(S.ANTIBOT_SUBCLASS.values()) == {"waf", "login_wall", "paywall"}
    assert S.ANTIBOT_SUBCLASS["www.wsj.com"] == "paywall"
    assert S.ANTIBOT_SUBCLASS["x.com"] == "login_wall"
    assert S.ANTIBOT_SUBCLASS["stackoverflow.com"] == "waf"



def test_thresholds_are_the_fetch_capability_numbers():
    """Fetch capability only: the thresholds answer whether this is the substantive
    content of this page, nothing about how cleanly it was parsed."""
    assert S.TH["fetch_ok"] == 0.3
    assert S.TH["fetch_lost"] == 0.05
    assert S.TH["render_ok"] == 0.4
    assert S.TH["vocab_min"] == 12
    assert S.TH["slow_loss_ms"] == 10_000


def test_parsing_quality_thresholds_are_gone():
    """Text purity / structural fidelity / truncation completeness were removed from
    the scope, so their thresholds must not linger."""
    for dead in ("noise_pass", "structure_pass", "tail_pass", "coverage_pass"):
        assert dead not in S.TH, "%s survives; a reader would assume it is scored" % dead


def test_removed_enums_stay_removed():
    """Screenshot shape labelling was never wired up (gt.shape was always empty) and
    was removed with the rest of the narrowing. Leaving an enum nothing produces makes
    a reader believe the judge consults it."""
    assert not hasattr(S, "SHAPES")
    # The language axis is not one of the reported dimensions; removed entirely.
    assert not hasattr(S, "LANG_PAGES")
    assert not hasattr(S, "RTL_LANGS")


def test_enums_are_the_declared_sets():
    assert S.EXPECT == frozenset({"content", "error", "redirect_final"})
    assert len(S.FAILURE_REASONS) == 9
    assert S.FAULTS == ("harness", "provider", "page")
    assert "our_size_cap" in S.FAILURE_REASONS


def test_assert_pageset_accepts_a_valid_set():
    S.assert_pageset(_valid_rows())


def test_assert_pageset_rejects_wrong_type_counts():
    with pytest.raises(AssertionError, match="type counts do not match"):
        S.assert_pageset([{"pid": "x", "type": "baseline", "probes": [],
                           "expect": "content", "defended": False}] * 100)


def test_assert_pageset_rejects_unknown_probe():
    rows = _valid_rows()
    rows[0]["probes"] = ["no_such_probe"]
    with pytest.raises(AssertionError, match="unknown probe"):
        S.assert_pageset(rows)


def test_assert_pageset_rejects_unknown_expect():
    rows = _valid_rows()
    rows[0]["expect"] = "maybe"
    with pytest.raises(AssertionError, match="unknown expect"):
        S.assert_pageset(rows)


def test_assert_pageset_rejects_wrong_defended_count():
    rows = _valid_rows()
    rows[0]["defended"] = not rows[0]["defended"]
    with pytest.raises(AssertionError, match="defended subset"):
        S.assert_pageset(rows)
