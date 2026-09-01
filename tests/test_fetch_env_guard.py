"""Credential-source check: a mismatch between `.env` and the shell must be reported.

`_load_dotenv` uses `os.environ.setdefault` and therefore **does not override variables
that already exist**. A stale key left in the shell silently wins over the new one in
.env, while both look correctly configured. The failure mode is expensive: an entire
round runs against the wrong credential, every cell fails on authorisation, and the
results invite a conclusion about the provider that is actually about our own shell.
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
    assert "…" in a and "…" in b, "only masked values are reported; secrets never logged"


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
    """Silently using the wrong key invalidates the whole round; stop before starting."""
    monkeypatch.setattr(R, "env_divergence",
                        lambda provs: {"OCTEN_API_KEY": ("aaa…bbb", "ccc…ddd")})
    ps = tmp_path / "p.jsonl"
    ps.write_text('{"pid":"p001","url":"https://e.com/","type":"baseline"}\n',
                  encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        R.run(ps, ["octen"], tmp_path / "out")
    msg = str(e.value)
    assert "OCTEN_API_KEY" in msg
    assert "shell" in msg and "in effect" in msg
    assert "--allow-env-override" in msg, "must say how to accept the value explicitly"


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
