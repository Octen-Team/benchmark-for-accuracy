"""GT 建库脚本的多通道续跑与合并。防护强度档依赖两条通道各跑一遍。"""
import json

from scripts import fetch_gt_build as B


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")


def test_resume_key_includes_the_channel(tmp_path):
    """只按 pid 去重的话第二条通道整轮被跳过，强度档永远算不出来。"""
    p = tmp_path / "gt.jsonl"
    _write(p, [{"pid": "p1", "gt": {"channel": "playwright_headless"}}])
    keys = B._done_keys(p)
    assert ("p1", "playwright_headless") in keys
    assert ("p1", "chrome_real") not in keys


def test_channel_of_routes_non_html_to_parse(self=None):
    assert B._channel_of({"doc_type": "pdf"}, "chrome_real") == "parse"
    assert B._channel_of({"doc_type": "html"}, "chrome_real") == "chrome_real"


def test_consolidate_keeps_both_channel_flags(tmp_path):
    p = tmp_path / "gt.jsonl"
    _write(p, [
        {"pid": "p1", "gt": {"channel": "playwright_headless", "headless_ok": False,
                             "vocab_n": 0, "vocab": []}},
        {"pid": "p1", "gt": {"channel": "chrome_real", "chrome_ok": True,
                             "vocab_n": 30, "vocab": ["a"]}},
    ])
    B.consolidate(p)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    g = rows[0]["gt"]
    assert g["headless_ok"] is False and g["chrome_ok"] is True
    assert g["vocab_n"] == 30, "内容应取真 Chrome 那条"


def test_strength_needs_both_channels(tmp_path):
    p = tmp_path / "gt.jsonl"
    _write(p, [
        {"pid": "soft", "gt": {"channel": "playwright_headless", "headless_ok": True}},
        {"pid": "med", "gt": {"channel": "chrome_real", "headless_ok": False,
                              "chrome_ok": True}},
        {"pid": "hard", "gt": {"channel": "chrome_real", "headless_ok": False,
                               "chrome_ok": False}},
        {"pid": "unk", "gt": {"channel": "playwright_headless", "headless_ok": False}},
    ])
    counts = B.backfill_strength(p)
    got = {r["pid"]: r["gt"]["strength"]
           for r in (json.loads(l) for l in p.read_text(encoding="utf-8").splitlines())}
    assert got == {"soft": "soft", "med": "medium", "hard": "hard", "unk": "unknown"}
    assert counts["unknown"] == 1


def test_unknown_is_not_silently_hard(tmp_path):
    """真 Chrome 没跑过时默认 hard 会把"没测"伪装成"测出来最难"。"""
    p = tmp_path / "gt.jsonl"
    _write(p, [{"pid": "x", "gt": {"channel": "playwright_headless", "headless_ok": False}}])
    B.backfill_strength(p)
    r = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert r["gt"]["strength"] == "unknown"
