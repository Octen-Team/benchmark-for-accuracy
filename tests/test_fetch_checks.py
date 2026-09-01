"""Mechanical pure functions. Fetch capability only — text purity, structural fidelity
and truncation completeness are no longer scored.

Two conventions run through this file:
  An empty denominator returns None.  A ratio over an empty set disguises itself as
                                      "everything passed".
  Mojibake literals are written escaped.  Real C1 control bytes are invisible in an
                                      editor and in a diff, so a reviewer cannot see
                                      what the test is actually asserting.
"""
from src import fetch_checks as C

# The classic residue of UTF-8 misread as latin-1, written escaped
MOJIBAKE = "\u00e3\u0081\u0082 \u00e2\u0080\u009c"
REPLACEMENT = "\ufffd"


class TestTokenize:
    def test_cjk_counted_per_character_not_per_sentence(self):
        """len(text.split()) is useless for Japanese: a whole sentence is one word."""
        ja = "\u543e\u8f29\u306f\u732b\u3067\u3042\u308b"
        assert C.len_norm(ja) == 7
        assert len(ja.split()) == 1          # counter-example: naive splitting finds 1

    def test_latin_split_on_whitespace(self):
        assert C.len_norm("the quick brown fox") == 4

    def test_mixed_text_adds_both(self):
        assert C.len_norm("hello \u732b\u72ac") == 1 + 2

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
        """A fallback to the parent or index page can slip past a coverage gate;
        the identity check exists specifically to catch it."""
        assert C.identity_ok("Attention Is All You Need transformer",
                             ["attention", "transformer"]) is True
        assert C.identity_ok("arXiv listing cs.CL recent submissions",
                             ["attention", "transformer"]) is False

    def test_identity_undefined_is_none(self):
        assert C.identity_ok("x", []) is None


class TestEncoding:
    def test_replacement_chars_fail(self):
        assert C.encoding_ok("\u6b63\u5e38" + REPLACEMENT * 3) is False

    def test_mojibake_signature_fails(self):
        assert C.encoding_ok(MOJIBAKE) is False

    def test_clean_cjk_and_latin_pass(self):
        assert C.encoding_ok("\u543e\u8f29\u306f\u732b\u3067\u3042\u308b") is True
        assert C.encoding_ok("perfectly ordinary english") is True

    def test_empty_text_is_not_an_encoding_failure(self):
        assert C.encoding_ok("") is True


class TestWallHit:
    def test_returns_evidence_not_a_verdict(self):
        """Treating the bare word "cloudflare" as evidence of blocking judges that
        vendor's own documentation as a wall."""
        assert "challenge" in C.wall_hit("Checking your browser before accessing")
        assert C.wall_hit("Cloudflare Workers documentation: routes and bindings") == []

    def test_multiple_signals_all_reported(self):
        hits = C.wall_hit("Please solve the CAPTCHA. Too many requests.")
        assert set(hits) == {"captcha", "ratelimit"}
