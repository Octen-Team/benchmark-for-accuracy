"""Mechanical transcription from CSV to page set. Two places are easy to get wrong:
   expect    the correct behaviour for 404/503 is a clean error, not content;
   doc_type  anything the URL cannot settle must stay unknown — guessing defeats
             the sniffing test entirely.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CSV = """#,category,url
1,Static Docs,https://en.wikipedia.org/wiki/Large_language_model
2,Robustness,https://httpbingo.org/status/404
3,Robustness,https://httpbingo.org/redirect/3
4,Documents,https://arxiv.org/pdf/2005.14165.pdf
5,Robustness,https://arxiv.org/pdf/1706.03762
6,Documents,https://raw.githubusercontent.com/datasets/gdp/master/data/gdp.csv
7,Defended (WAF/Reviews/Paywall),https://www.wsj.com/
8,Social/Login,https://x.com/elonmusk
9,Robustness,http://www.textfiles.com/computers/
10,E-commerce/SPA,https://www.rakuten.co.jp/
"""


def _build(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(CSV, encoding="utf-8")
    out = tmp_path / "pageset.jsonl"
    subprocess.run([sys.executable, "-m", "scripts.fetch_pageset_build",
                    "--csv", str(src), "--out", str(out), "--no-assert"],
                   check=True, cwd=REPO)
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]


def test_category_maps_to_type_and_subclass(tmp_path):
    rows = {r["url"]: r for r in _build(tmp_path)}
    wiki = rows["https://en.wikipedia.org/wiki/Large_language_model"]
    assert wiki["type"] == "baseline" and wiki["defended"] is False
    wsj = rows["https://www.wsj.com/"]
    assert wsj["type"] == "antibot" and wsj["defended"] is True
    assert wsj["antibot_subclass"] == "paywall"
    assert rows["https://x.com/elonmusk"]["antibot_subclass"] == "login_wall"


def test_expect_is_error_for_status_pages(tmp_path):
    rows = {r["url"]: r for r in _build(tmp_path)}
    assert rows["https://httpbingo.org/status/404"]["expect"] == "error"
    assert rows["https://httpbingo.org/redirect/3"]["expect"] == "redirect_final"
    assert rows["https://en.wikipedia.org/wiki/Large_language_model"]["expect"] == "content"


def test_probes_attach_by_url_substring(tmp_path):
    rows = {r["url"]: r for r in _build(tmp_path)}
    assert "oversize" in rows["https://arxiv.org/pdf/2005.14165.pdf"]["probes"]
    assert "url_quirk" in rows["https://arxiv.org/pdf/1706.03762"]["probes"]
    assert "raw_direct" in rows[
        "https://raw.githubusercontent.com/datasets/gdp/master/data/gdp.csv"]["probes"]
    tf = rows["http://www.textfiles.com/computers/"]["probes"]
    assert "empty_thin" in tf and "plain_http" in tf


def test_doc_type_never_guesses_from_a_missing_suffix(tmp_path):
    rows = {r["url"]: r for r in _build(tmp_path)}
    assert rows["https://arxiv.org/pdf/2005.14165.pdf"]["doc_type"] == "pdf"
    quirk = rows["https://arxiv.org/pdf/1706.03762"]
    assert quirk["doc_type"] == "unknown"
    assert quirk["doc_type_rule"] == "no_suffix_defer"
    assert rows["https://raw.githubusercontent.com/datasets/gdp/master/data/gdp.csv"
                ]["doc_type"] == "csv"


def test_host_dup_marks_repeated_domains(tmp_path):
    rows = _build(tmp_path)
    arxiv = [r for r in rows if r["host"] == "arxiv.org"]
    assert len(arxiv) == 2 and all(r["host_dup"] for r in arxiv)
    assert not [r for r in rows if r["host"] == "en.wikipedia.org"][0]["host_dup"]


def test_the_language_axis_is_gone(tmp_path):
    """The language axis is not a reported dimension and was removed; the page set
    must not carry the field either."""
    rows = _build(tmp_path)
    assert all("lang" not in r for r in rows)

def test_probes_union_when_two_labels_match(tmp_path):
    """One page carries both oversize and raw_direct — the second must not overwrite
    the first."""
    from src.fetch_spec import PAGE_LABELS  # noqa: F401  ensure the label table loaded
    from scripts.fetch_pageset_build import label_for
    lab = label_for("https://www.gutenberg.org/files/1342/1342-0.txt")
    assert set(lab["probes"]) == {"oversize", "raw_direct"}
