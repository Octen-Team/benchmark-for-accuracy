"""runner 的续跑、抖动键、故障记录。全部打桩，不打真网络。"""
import json
from pathlib import Path

from src import fetch_run as R
from src.fetch_backends import FetchResponse


def _pageset(tmp_path, n=3):
    p = tmp_path / "pages.jsonl"
    p.write_text("\n".join(
        json.dumps({"pid": "p%03d" % i, "url": "https://e%d.com/" % i, "type": "baseline"})
        for i in range(1, n + 1)), encoding="utf-8")
    return p


class _Stub:
    def __init__(self, name):
        self.name = name

    def fetch(self, url, timeout=60):
        return FetchResponse(url=url, provider=self.name, status="ok",
                             text="body of " + url, len_norm=3, latency_ms=12.0)


def test_writes_one_row_per_page_and_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    out = R.run(_pageset(tmp_path), ["a", "b"], tmp_path / "run", concurrency=2)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6
    assert {r["provider"] for r in rows} == {"a", "b"}
    assert all(r["run_seq"] == 0 for r in rows)


def test_resumes_and_skips_done_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    ps = _pageset(tmp_path)
    R.run(ps, ["a"], tmp_path / "run", concurrency=2)
    out = R.run(ps, ["a"], tmp_path / "run", concurrency=2)     # 第二次应当全部跳过
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3, "续跑重复写了行"


def test_bad_line_does_not_abort_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "extractions.jsonl").write_text('{"pid": "p001", "prov', encoding="utf-8")
    out = R.run(_pageset(tmp_path), ["a"], run_dir, concurrency=1)
    rows = [l for l in out.read_text(encoding="utf-8").splitlines() if l.startswith("{\"pid")]
    assert len(rows) >= 3


def test_repeat_writes_distinct_run_seq(tmp_path, monkeypatch):
    """抖动模式的键必须含 run_seq，否则续跑把第二轮当成已完成跳掉。"""
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    out = R.run(_pageset(tmp_path), ["a"], tmp_path / "run", repeat=3, concurrency=2)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 9
    assert sorted(r["run_seq"] for r in rows if r["pid"] == "p001") == [0, 1, 2]


def test_adapter_exception_is_recorded_as_harness_fault(tmp_path, monkeypatch):
    class Boom:
        def fetch(self, url, timeout=60):
            raise RuntimeError("zyte: 缺少环境变量 ZYTE_API_KEY")
    monkeypatch.setattr(R, "get_fetcher", lambda n: Boom())
    out = R.run(_pageset(tmp_path, n=1), ["zyte"], tmp_path / "run")
    r = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert r["status"] == "error" and r["fault"] == "harness"
    assert "ZYTE_API_KEY" in r["error"]


def test_empty_content_is_not_retried(tmp_path, monkeypatch):
    """HTTP 200 但内容为空是被测方的真实行为，重试等于把"取不到"洗成"取到了"。"""
    calls = {"n": 0}

    class Empty:
        def fetch(self, url, timeout=60):
            calls["n"] += 1
            return FetchResponse(url=url, provider="x", status="error", text="",
                                 failure_reason="nothing_extractable", fault="page")
    monkeypatch.setattr(R, "get_fetcher", lambda n: Empty())
    R.run(_pageset(tmp_path, n=1), ["x"], tmp_path / "run")
    assert calls["n"] == 1


def test_rate_limited_is_retried(tmp_path, monkeypatch):
    calls = {"n": 0}

    class Limited:
        def fetch(self, url, timeout=60):
            calls["n"] += 1
            return FetchResponse(url=url, provider="x", status="error",
                                 failure_reason="rate_limited", fault="provider")
    monkeypatch.setattr(R, "get_fetcher", lambda n: Limited())
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    R.run(_pageset(tmp_path, n=1), ["x"], tmp_path / "run")
    assert calls["n"] == 3


def test_limit_is_deterministic_by_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    out = R.run(_pageset(tmp_path, n=5), ["a"], tmp_path / "run", limit=2)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["pid"] for r in rows) == ["p001", "p002"]


def test_pace_spec_parses():
    assert R._parse_pace("octen=2.5,firecrawl=6.5") == {"octen": 2.5, "firecrawl": 6.5}
    assert R._parse_pace(None) == {}
