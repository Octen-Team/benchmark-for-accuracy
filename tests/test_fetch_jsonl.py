"""JSONL 读取的分行口径。

**按 "\\n" 切，不能用 splitlines()** —— 后者还会在 U+2028 / U+2029 / U+0085 上切，
而那些字符在网页正文里合法出现、`json.dumps` 也不转义。500 条抓取里实测就有一条，
表现是一个看不懂的 `Unterminated string`，而逐行迭代文件时又复现不出来。
"""
import json

import pytest

from scripts import fetch_report, fetch_score_run
from src import fetch_run

LOADERS = [fetch_score_run.load_jsonl, fetch_report.load_jsonl, fetch_run.load_jsonl]
SEPARATORS = ["\u2028", "\u2029", "\u0085", "\u000b", "\u000c", "\u001c"]


@pytest.mark.parametrize("sep", SEPARATORS)
@pytest.mark.parametrize("load", LOADERS)
def test_unicode_line_separators_inside_a_record_do_not_split_it(tmp_path, load, sep):
    p = tmp_path / "x.jsonl"
    rec = {"pid": "p001", "provider": "octen", "text": "before" + sep + "after"}
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = load(p)
    assert len(rows) == 1
    assert rows[0]["text"] == "before" + sep + "after"


@pytest.mark.parametrize("load", LOADERS)
def test_ordinary_records_still_load(tmp_path, load):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps({"pid": "p%03d" % i}) for i in range(3)) + "\n",
                 encoding="utf-8")
    assert len(load(p)) == 3


def test_splitlines_would_have_broken_it():
    """把被修掉的写法钉在这里，免得有人"顺手改回去"。"""
    line = json.dumps({"text": "a\u2028b"}, ensure_ascii=False)
    assert len(line.splitlines()) == 2, "splitlines 确实会劈开它"
    assert len(line.split("\n")) == 1


def test_resume_keys_survive_the_same_characters(tmp_path):
    """续跑的键也走同一条读取路径 —— 它坏了会把已完成的格当成没跑。"""
    p = tmp_path / "x.jsonl"
    rec = {"pid": "p001", "provider": "octen", "run_seq": 0, "text": "a\u2028b"}
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    assert fetch_run.done_keys(p) == {("p001", "octen", 0)}
    assert fetch_score_run.done_keys(p) == {("p001", "octen", 0)}
