"""离线重分类：分类规则改了之后，就地更新已有结果，不用重抓。"""
import json
import subprocess
import sys
from pathlib import Path

from scripts.fetch_reclassify import reclassify

REPO = Path(__file__).resolve().parents[1]


def test_a_site_ban_in_the_body_beats_the_status_code():
    """Zyte 用 HTTP 520 + `"title":"Website Ban"` 表达"被站点封了"。
    落进 other 会让「被拦」看起来像「不明错误」，归因表上含义完全不同。"""
    rec = {"status": "error", "http_status": 520, "failure_reason": "other",
           "error": '{"type":"/download/website-ban","title":"Website Ban","status":520}'}
    assert reclassify(rec) is True
    assert rec["failure_reason"] == "anti_bot_blocked"


def test_a_plain_5xx_without_an_explanation_is_left_alone():
    rec = {"status": "error", "http_status": 500, "failure_reason": "timeout_upstream",
           "error": "internal server error"}
    assert reclassify(rec) is False


def test_policy_403_is_relabelled():
    rec = {"status": "error", "http_status": 403, "failure_reason": "anti_bot_blocked",
           "fault": "provider", "error": "we do not support this site"}
    assert reclassify(rec) is True
    assert rec["failure_reason"] == "blocklisted_domain"


def test_a_real_403_is_left_alone():
    rec = {"status": "error", "http_status": 403, "failure_reason": "anti_bot_blocked",
           "fault": "provider", "error": "Cloudflare challenge"}
    assert reclassify(rec) is False
    assert rec["failure_reason"] == "anti_bot_blocked"


def test_rows_without_an_explicit_reason_are_untouched():
    """只在响应体**明说了原因**时才改；没说的一律保留按状态码的判断。"""
    rec = {"status": "error", "http_status": 429, "failure_reason": "rate_limited",
           "error": "Too many requests, slow down"}
    assert reclassify(rec) is False
    assert rec["failure_reason"] == "rate_limited"


def test_an_explicit_reason_wins_over_the_status_code():
    """429 但响应体说的是政策拒绝 —— 以响应体为准，状态码只是兜底。"""
    rec = {"status": "error", "http_status": 429, "failure_reason": "rate_limited",
           "error": "we do not support this site"}
    assert reclassify(rec) is True
    assert rec["failure_reason"] == "blocklisted_domain"


def test_the_verdict_reason_string_is_updated_too():
    """判定记录里 reason 嵌着 failure_reason —— 不同步改的话两个字段会对不上。"""
    rec = {"status": "error", "http_status": 403, "failure_reason": "anti_bot_blocked",
           "error": "we do not support this site",
           "reason": "fetch_failed:anti_bot_blocked"}
    assert reclassify(rec) is True
    assert rec["reason"] == "fetch_failed:blocklisted_domain"


def test_script_patches_a_file_in_place(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (
        {"status": "error", "http_status": 403, "failure_reason": "anti_bot_blocked",
         "error": "we do not support this site"},
        {"status": "ok", "http_status": 200, "failure_reason": None, "error": None},
    )), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "scripts.fetch_reclassify", str(p)],
                   check=True, cwd=REPO)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]
    assert rows[0]["failure_reason"] == "blocklisted_domain"
    assert rows[1]["status"] == "ok"


def test_verdicts_sync_attribution_from_extractions(tmp_path):
    """判定记录里没有 http_status / error，自己重分类分不出来 —— 只能从抓取结果搬。
    不同步的话报告读的还是旧归因，重分类等于白做。"""
    ext = tmp_path / "e.jsonl"
    ext.write_text(json.dumps({"pid": "p1", "provider": "firecrawl", "run_seq": 0,
                               "failure_reason": "blocklisted_domain",
                               "fault": "provider"}), encoding="utf-8")
    ver = tmp_path / "v.jsonl"
    ver.write_text(json.dumps({"pid": "p1", "provider": "firecrawl", "run_seq": 0,
                               "failure_reason": "anti_bot_blocked", "fault": "provider",
                               "reason": "fetch_failed:anti_bot_blocked"}), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "scripts.fetch_reclassify", str(ver),
                    "--sync-from", str(ext)], check=True, cwd=REPO)
    r = json.loads(ver.read_text(encoding="utf-8").strip())
    assert r["failure_reason"] == "blocklisted_domain"
    assert r["reason"] == "fetch_failed:blocklisted_domain"


def test_sync_does_not_touch_rows_missing_from_extractions(tmp_path):
    ext = tmp_path / "e.jsonl"
    ext.write_text("", encoding="utf-8")
    ver = tmp_path / "v.jsonl"
    orig = {"pid": "p9", "provider": "x", "run_seq": 0, "failure_reason": "other"}
    ver.write_text(json.dumps(orig), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "scripts.fetch_reclassify", str(ver),
                    "--sync-from", str(ext)], check=True, cwd=REPO)
    assert json.loads(ver.read_text(encoding="utf-8").strip())["failure_reason"] == "other"
