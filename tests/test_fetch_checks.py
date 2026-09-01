"""机械层纯函数。**本轮只评抓取能力**，
正文纯度 / 结构保真 / 截断完整度三项已删除。

两条贯穿全文件的约定：
  分母为空返回 None    空集合上的比值伪装成"全部达标"，design 那轮的 8eb095e 栽在这儿。
  乱码字面量用转义写   真实的 C1 控制字节在编辑器和 diff 里不可见，review 读不出问题。
"""
from src import fetch_checks as C

# latin-1 误读 UTF-8 的典型残渣，用转义写
MOJIBAKE = "\u00e3\u0081\u0082 \u00e2\u0080\u009c"
REPLACEMENT = "\ufffd"


class TestTokenize:
    def test_cjk_counted_per_character_not_per_sentence(self):
        """len(text.split()) 对日文是废的 —— 整句算一个词（设计文档 1.3）。"""
        ja = "吾輩は猫である"
        assert C.len_norm(ja) == 7
        assert len(ja.split()) == 1          # 反例：朴素分词只数出 1

    def test_latin_split_on_whitespace(self):
        assert C.len_norm("the quick brown fox") == 4

    def test_mixed_text_adds_both(self):
        assert C.len_norm("hello 世界") == 1 + 2

    def test_tokenize_lowercases_and_drops_punctuation(self):
        assert C.tokenize("Hello, World!") == ["hello", "world"]

    def test_empty_is_zero_not_error(self):
        assert C.len_norm("") == 0
        assert C.tokenize("") == []


class TestRatios:
    def test_coverage_full_and_partial(self):
        assert C.coverage("alpha beta gamma", ["alpha", "beta"]) == 1.0
        assert C.coverage("alpha", ["alpha", "beta"]) == 0.5

    def test_coverage_empty_vocab_is_none_not_zero(self):
        assert C.coverage("anything", []) is None

    def test_render_hit_is_a_ratio(self):
        assert C.render_hit("price 19.99 in stock", ["19", "stock"]) == 1.0


class TestIdentity:
    def test_identity_detects_a_parent_page(self):
        """退回父页/索引页在覆盖率口径下能蒙过去，同一性是专门抓它的（设计文档 5.1）。"""
        assert C.identity_ok("Attention Is All You Need transformer",
                             ["attention", "transformer"]) is True
        assert C.identity_ok("arXiv listing cs.CL recent submissions",
                             ["attention", "transformer"]) is False

    def test_identity_undefined_is_none(self):
        assert C.identity_ok("x", []) is None


class TestEncoding:
    def test_replacement_chars_fail(self):
        assert C.encoding_ok("正常" + REPLACEMENT * 3) is False

    def test_mojibake_signature_fails(self):
        assert C.encoding_ok(MOJIBAKE) is False

    def test_clean_cjk_and_latin_pass(self):
        assert C.encoding_ok("吾輩は猫である") is True
        assert C.encoding_ok("perfectly ordinary english") is True

    def test_empty_text_is_not_an_encoding_failure(self):
        assert C.encoding_ok("") is True


class TestWallHit:
    def test_returns_evidence_not_a_verdict(self):
        """参考报告把 cloudflare 这个词当拦截证据，于是官方文档被判成墙页（设计文档 4）。"""
        assert "challenge" in C.wall_hit("Checking your browser before accessing")
        assert C.wall_hit("Cloudflare Workers documentation: routes and bindings") == []

    def test_multiple_signals_all_reported(self):
        hits = C.wall_hit("Please solve the CAPTCHA. Too many requests.")
        assert set(hits) == {"captcha", "ratelimit"}
