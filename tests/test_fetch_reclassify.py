"""Offline reclassification: update existing results in place after a rule change,
without re-fetching."""
import json
import subprocess
import sys
from pathlib import Path

from scripts.fetch_reclassify import reclassify

REPO = Path(__file__).resolve().parents[1]


def test_a_site_ban_in_the_body_beats_the_status_code():
    """Some providers express "the site banned us" as HTTP 520 with a ban title in the
    body. Left in `other`, a genuine block reads as an unexplained error, and those
    mean different things in the attribution table."""
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
    """Only rewrite when the body **states** a reason; otherwise keep the status-code call."""
    rec = {"status": "error", "http_status": 429, "failure_reason": "rate_limited",
           "error": "Too many requests, slow down"}
    assert reclassify(rec) is False
    assert rec["failure_reason"] == "rate_limited"


def test_an_explicit_reason_wins_over_the_status_code():
    """A 429 whose body describes a policy refusal: the body wins, the status code is
    only a fallback."""
    rec = {"status": "error", "http_status": 429, "failure_reason": "rate_limited",
           "error": "we do not support this site"}
    assert reclassify(rec) is True
    assert rec["failure_reason"] == "blocklisted_domain"


def test_the_verdict_reason_string_is_updated_too():
    """Verdict rows embed failure_reason inside `reason`; without updating both, the two
    fields disagree."""
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
    """Verdict rows carry no http_status or error, so they cannot be reclassified on
    their own — the values have to come from the extraction rows. Out of sync, the
    report still shows the old attribution and the reclassification did nothing."""
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
