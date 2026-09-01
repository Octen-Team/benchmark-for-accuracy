"""凭据来源检查：`.env` 与 shell 环境不一致时必须喊出来。

`_load_dotenv` 用 `os.environ.setdefault`，**不覆盖已存在的变量** —— shell 里残留的
旧 key 会静默压过 .env 里的新 key，两边看起来都"设好了"。2026-09-01 实测踩到：
新的 firecrawl key 写进了 .env，跑出来一直是 402，因为 shell 里有个旧 key 一直生效，
新 key 一次都没被用过，还据此写了"该家欠费"的结论。
"""
import pytest

from src import fetch_backends as B
from src import fetch_run as R


@pytest.fixture
def envfile(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setattr(B, "__file__", str(tmp_path / "pkg" / "fetch_backends.py"))
    (tmp_path / "pkg").mkdir(exist_ok=True)
    return p


def test_divergence_is_detected(envfile, monkeypatch):
    envfile.write_text("OCTEN_API_KEY=octen-newkey-abcdefgh\n", encoding="utf-8")
    monkeypatch.setenv("OCTEN_API_KEY", "octen-oldkey-12345678")
    d = B.env_divergence(["octen"])
    assert "OCTEN_API_KEY" in d
    a, b = d["OCTEN_API_KEY"]
    assert "…" in a and "…" in b, "只报掩码，凭据不进日志"


def test_matching_values_are_not_flagged(envfile, monkeypatch):
    envfile.write_text("OCTEN_API_KEY=same-value-1234567\n", encoding="utf-8")
    monkeypatch.setenv("OCTEN_API_KEY", "same-value-1234567")
    assert B.env_divergence(["octen"]) == {}


def test_key_only_in_dotenv_is_not_flagged(envfile, monkeypatch):
    envfile.write_text("ZYTE_API_KEY=only-in-dotenv-123\n", encoding="utf-8")
    monkeypatch.delenv("ZYTE_API_KEY", raising=False)
    assert B.env_divergence(["zyte"]) == {}


def test_only_the_selected_providers_keys_are_checked(envfile, monkeypatch):
    envfile.write_text("OCTEN_API_KEY=a-value-abcdefgh\n"
                       "EXA_API_KEY=b-value-abcdefgh\n", encoding="utf-8")
    monkeypatch.setenv("OCTEN_API_KEY", "different-value-x")
    monkeypatch.setenv("EXA_API_KEY", "different-value-y")
    assert set(B.env_divergence(["octen"])) == {"OCTEN_API_KEY"}
    assert set(B.env_divergence(["octen", "exa"])) == {"OCTEN_API_KEY", "EXA_API_KEY"}


def test_the_runner_refuses_to_start_on_a_shadowed_key(tmp_path, monkeypatch):
    """静默用错 key 会让整轮结论作废 —— 起跑前就该拦住。"""
    monkeypatch.setattr(R, "env_divergence",
                        lambda provs: {"OCTEN_API_KEY": ("aaa…bbb", "ccc…ddd")})
    ps = tmp_path / "p.jsonl"
    ps.write_text('{"pid":"p001","url":"https://e.com/","type":"baseline"}\n',
                  encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        R.run(ps, ["octen"], tmp_path / "out")
    msg = str(e.value)
    assert "OCTEN_API_KEY" in msg
    assert "shell" in msg and "生效" in msg
    assert "--allow-env-override" in msg, "要告诉人怎么明确接受"


def test_the_override_flag_lets_it_through(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "env_divergence",
                        lambda provs: {"OCTEN_API_KEY": ("aaa…bbb", "ccc…ddd")})
    monkeypatch.setattr(R, "get_fetcher", lambda n: _Stub(n))
    ps = tmp_path / "p.jsonl"
    ps.write_text('{"pid":"p001","url":"https://e.com/","type":"baseline"}\n',
                  encoding="utf-8")
    out = R.run(ps, ["octen"], tmp_path / "out", allow_env_override=True)
    assert out.exists()


class _Stub:
    def __init__(self, name):
        self.name = name

    def fetch(self, url, timeout=60):
        from src.fetch_backends import FetchResponse
        return FetchResponse(url=url, provider=self.name, status="ok", text="body",
                             len_norm=1, latency_ms=1.0)
