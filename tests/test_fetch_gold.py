"""人工金标回流：核对页导出 -> 金标文件 -> 判定优先采纳 -> ⚠ 消失。

这一条链断在任何一环，人工核那 15 分钟就白花了。
"""
import json
import subprocess
import sys
from pathlib import Path

from scripts import fetch_report as R
from src import fetch_score as S

REPO = Path(__file__).resolve().parents[1]


def _page(**kw):
    base = {"pid": "p001", "url": "https://e.com/a", "type": "antibot",
            "expect": "content", "probes": [], "doc_type": "html",
            "antibot_subclass": "waf", "lang": None,
            "gt": {"gt_gap": True, "vocab": [], "anchors": []}}
    return {**base, **kw}


def _resp(text="whatever", **kw):
    return {"provider": "octen", "status": "ok", "text": text, "latency_ms": 1.0, **kw}


class TestGoldStore:
    def test_gold_overrides_the_panel(self, tmp_path, monkeypatch):
        g = tmp_path / "gold.jsonl"
        g.write_text(json.dumps({"pid": "p001", "provider": "octen", "verdict": "lost"}),
                     encoding="utf-8")
        called = {"n": 0}
        monkeypatch.setattr(S, "panel_verdict",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        out = S.score_one(_page(), _resp(), panel=["m1"], gold=S.GoldStore(g))
        assert out["verdict"] == "lost" and out["reason"] == "human_gold"
        assert called["n"] == 0, "有金标就不该再烧面板的钱"

    def test_gold_expires_when_the_fetched_text_changes(self, tmp_path):
        import hashlib
        sha = hashlib.sha256(b"original").hexdigest()[:16]
        g = tmp_path / "gold.jsonl"
        g.write_text(json.dumps({"pid": "p001", "provider": "octen", "verdict": "pass",
                                 "text_sha": sha}), encoding="utf-8")
        store = S.GoldStore(g)
        assert store.lookup(_page(), _resp("original")) == "pass"
        assert store.lookup(_page(), _resp("something else")) is None, \
            "换了一轮抓取，人工当时看的内容已经不在了，金标不该继续生效"

    def test_unsure_never_becomes_gold(self, tmp_path):
        g = tmp_path / "gold.jsonl"
        g.write_text("\n".join(json.dumps(r) for r in (
            {"pid": "p001", "provider": "octen", "verdict": "unsure"},
            {"pid": "p002", "provider": "octen", "verdict": "pass"})), encoding="utf-8")
        assert len(S.GoldStore(g)) == 1

    def test_provider_comes_from_the_dict_key_not_the_payload(self, tmp_path):
        """交叉判按字典键分厂商；payload 里的字段万一对不上，会取到别人的金标。"""
        g = tmp_path / "gold.jsonl"
        g.write_text(json.dumps({"pid": "p001", "provider": "octen", "verdict": "pass"}),
                     encoding="utf-8")
        store = S.GoldStore(g)
        assert store.lookup(_page(), _resp(), provider="exa") is None
        assert store.lookup(_page(), _resp(provider="exa"), provider="octen") == "pass"

    def test_missing_gold_file_is_not_an_error(self, tmp_path):
        assert len(S.GoldStore(tmp_path / "nope.jsonl")) == 0
        assert len(S.GoldStore(None)) == 0

    def test_cross_mode_also_honours_gold(self, tmp_path, monkeypatch):
        g = tmp_path / "gold.jsonl"
        g.write_text(json.dumps({"pid": "p001", "provider": "octen", "verdict": "pass"}),
                     encoding="utf-8")
        seen = {}
        monkeypatch.setattr(S, "panel_cross",
                            lambda page, resps, panel, **k: seen.update(resps) or
                            {p: {"verdict": "lost", "panel_split": False,
                                 "dishonest": False, "votes": {}} for p in resps})
        out = S.score_page_cross(_page(),
                                 {"octen": _resp(), "exa": _resp(provider="exa")},
                                 panel=["m1"], gold=S.GoldStore(g))
        assert out["octen"]["verdict"] == "pass" and out["octen"]["reason"] == "human_gold"
        assert "octen" not in seen, "有金标的格不该送进交叉判"
        assert out["exa"]["verdict"] == "lost"


class TestIngestScript:
    def test_marks_become_gold_with_a_text_fingerprint(self, tmp_path):
        ext = tmp_path / "ext.jsonl"
        ext.write_text(json.dumps({"pid": "p001", "provider": "octen",
                                   "text": "hello", "run_seq": 0}), encoding="utf-8")
        marks = tmp_path / "marks.jsonl"
        marks.write_text("\n".join(json.dumps(r) for r in (
            {"pid": "p001", "provider": "octen", "human_verdict": "pass"},
            {"pid": "p001", "provider": "exa", "human_verdict": "unsure"})),
            encoding="utf-8")
        out = tmp_path / "gold.jsonl"
        subprocess.run([sys.executable, "-m", "scripts.fetch_gold_ingest",
                        "--marks", str(marks), "--out", str(out),
                        "--extractions", str(ext)], check=True, cwd=REPO)
        rows = [json.loads(l) for l in out.read_text(encoding="utf-8").split("\n") if l.strip()]
        assert len(rows) == 1
        assert rows[0]["verdict"] == "pass" and rows[0]["text_sha"]


class TestReportClearsTheWarning:
    def _rows(self, reason):
        pages = [{"pid": "p1", "url": "u", "host": "h", "type": "antibot",
                  "doc_type": "html", "probes": [], "lang": None,
                  "antibot_subclass": "waf", "gt": {"gt_gap": True}}]
        vs = [{"pid": "p1", "provider": "a", "type": "antibot", "verdict": "pass",
               "antibot_subclass": "waf", "lang": None, "strength": "hard",
               "latency_ms": 1.0, "len_norm": 10, "run_seq": 0, "dishonest": False,
               "suspicious_bypass": False, "panel_split": False, "reason": reason,
               "failure_reason": None, "fault": None, "checks": {}}]
        return R.aggregate(vs, pages)

    def test_panel_verdicts_on_gap_pages_stay_low_confidence(self):
        agg = self._rows("panel_cross")
        assert agg["slices"]["strength"]["hard"]["_gap_share"] == 1.0
        assert "低置信" in R.render_markdown(agg)

    def test_human_verified_cells_clear_the_warning(self):
        """不排掉的话，人工标了半天 ⚠ 也不会消失 —— 那这活就白干了。"""
        agg = self._rows("human_gold")
        assert agg["slices"]["strength"]["hard"]["_gap_share"] == 0.0
        assert agg["slices"]["strength"]["hard"]["_human_verified"] == 1
        assert agg["meta"]["human_verified"] == 1
        assert agg["meta"]["verdicts_on_gt_gap_pages"] == 0
        assert "低置信" not in R.render_markdown(agg)
