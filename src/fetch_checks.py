"""机械层：纯函数检查。无网络、无 key、无 LLM，所以全部可单测。

**本轮只评抓取能力，不评解析质量。** 正文纯度（noise_ratio）、结构保真
（structure_score）、截断完整度（tail_hit）三项已删除 —— 留着不评的指标躺在
代码和报告里，读的人会以为它们进了评价。

**分母为空一律 None，绝不 0.0** —— 空集合上的比值会伪装成"全部达标"或"全部不达标"，
design 那轮的 8eb095e 就是栽在这儿。调用方拿到 None 的含义是"这一项在这页上无定义"，
报告里写"未标注"而不是 0%。

`wall_hit` 是唯一一个**不参与判定**的检查：参考报告把 "cloudflare" 这个词当拦截证据，
于是 Cloudflare 官方文档被判成墙页。墙的判定归 GT 的 shape 标签（视觉判的）与面板。
"""
from __future__ import annotations

import re
import unicodedata

from .fetch_spec import TH

_WORD = re.compile("[0-9a-z\u00c0-\u024f]+")
_CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# 乱码特征：latin-1 误读 UTF-8 的残渣 —— 前导字节落在 C2-EF，续字节落在 C1 区
_MOJIBAKE = re.compile("[\u00c2-\u00ef][\u0080-\u00bf]")
_REPLACEMENT = "\ufffd"

_WALL_PATTERNS = {
    "challenge": r"checking your browser|just a moment|verify you are human"
                 r"|enable javascript and cookies",
    "captcha": r"\bcaptcha\b|recaptcha|hcaptcha",
    "login": r"log in to continue|sign in to continue|create an account to",
    "paywall": r"subscribe to (read|continue)|this article is for subscribers",
    "ratelimit": r"too many requests|rate limit exceeded",
}


def tokenize(text: str) -> list[str]:
    """小写、去标点。CJK 逐字成 token，其余按词切。"""
    if not text:
        return []
    t = unicodedata.normalize("NFKC", text).lower()
    out: list[str] = []
    for chunk in t.split():
        if _CJK.search(chunk):
            out.extend(_CJK.findall(chunk))
            out.extend(_WORD.findall(_CJK.sub(" ", chunk)))
        else:
            out.extend(_WORD.findall(chunk))
    return out


def len_norm(text: str) -> int:
    """CJK 按字符 + 其余按空白分词。

    `len(text.split())` 对日文是废的（整句算一个词），而集合里 aozora / rakuten / qiita
    三条是日文。跨家长度只作参考、不排名（playbook 9.6）。
    """
    if not text:
        return 0
    return len(_CJK.findall(text)) + len(_WORD.findall(_CJK.sub(" ", text.lower())))


def _ratio(text: str, terms: list[str] | None) -> float | None:
    if not terms:
        return None
    got = set(tokenize(text))
    return sum(1 for t in terms if t.lower() in got) / len(terms)


def coverage(text: str, vocab: list[str] | None) -> float | None:
    """召回：GT 内容词表被取到了多少。基线型的主指标之一。"""
    return _ratio(text, vocab)


def render_hit(text: str, anchors: list[str] | None) -> float | None:
    """「仅渲染后可见」锚点的命中率 —— SPA 页的成功闸门。

    这仍然是**抓取能力**而不是解析质量：拿到服务端骨架 HTML 的家 coverage 可能不低
    （nav 里就有商品名），但 JS 执行后才出现的价格 / 库存 / 列表项一个都没有 ——
    它没有把这一页抓下来，只抓到了一个壳。
    """
    return _ratio(text, anchors)


def identity_ok(text: str, anchors: list[str] | None) -> bool | None:
    """返回的是不是这条 URL 的内容。独有锚点命中过半即认为是。

    抓退回父页/索引页/搜索结果页的静默失败 —— 它比失败更坏，下游发现不了。
    """
    r = _ratio(text, anchors)
    return None if r is None else r >= 0.5


def encoding_ok(text: str) -> bool:
    """自足检查：只看文本自身，不需要 GT，所以弱 GT 的反爬页也能判（设计文档 1.3）。"""
    if not text:
        return True
    if text.count(_REPLACEMENT) >= 3:
        return False
    return not _MOJIBAKE.search(text)


def wall_hit(text: str) -> list[str]:
    """**只作证据字段记录，不参与判定。** 墙的判定归 GT 的 shape 标签与面板。"""
    low = (text or "").lower()[:20000]
    return sorted(n for n, pat in _WALL_PATTERNS.items() if re.search(pat, low))
