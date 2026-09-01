"""Fetch 实跑 runner。逐条落盘、可恢复、按家节流。

**抓取不套用检索那套重试**：检索的重试条件里有「结果不满 k 要重试」，
是给 `search(query, k)` 的；fetch 没有 k 的概念。这里只在传输层异常 / 429 / 5xx 上重试，
**HTTP 200 但内容为空不重试** —— 那是被测方的真实行为，重试等于把「取不到」洗成「取到了」。

抖动模式（`--repeat`）的键是 `(pid, provider, run_seq)`，不是 `(pid, provider)`，
否则续跑会把第二次重复跑当成「已完成」跳掉。
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from .fetch_io import append, progress
from .fetch_backends import env_divergence, get_fetcher

RETRY_REASONS = {"rate_limited", "timeout_upstream"}
_PACE_LOCKS: dict[str, Lock] = {}
_LAST_CALL: dict[str, float] = {}


def load_jsonl(path: Path) -> list[dict]:
    # **按 "\n" 切，不能用 splitlines()**：后者还会在 U+2028 / U+2029 / U+0085 上切，
    # 而那些字符在网页正文里合法出现、json.dumps 也不转义 —— 一条完整记录会被从
    # 中间劈开，报出一个看不懂的 "Unterminated string"。实测 500 条抓取里就有。
    return [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]


def done_keys(path: Path) -> set[tuple[str, str, int]]:
    """已完成的 (pid, provider, run_seq)。坏行跳过而不是让整轮起不来。"""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        try:
            r = json.loads(line)
            out.add((r["pid"], r["provider"], int(r.get("run_seq", 0))))
        except Exception:                        # noqa: BLE001
            continue
    return out


def _pace(provider: str, seconds: float) -> None:
    """按家节流。octen 在 2.5s/请求下仍会拒 —— 参考报告实测过。"""
    if seconds <= 0:
        return
    lock = _PACE_LOCKS.setdefault(provider, Lock())
    with lock:
        wait = seconds - (time.time() - _LAST_CALL.get(provider, 0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[provider] = time.time()


def fetch_once(page: dict, provider: str, *, timeout: int, pace: float,
               attempts: int = 3) -> dict:
    """一格。重试只针对**传输层与限速**，内容为空不重试。"""
    last = None
    for i in range(attempts):
        _pace(provider, pace)
        try:
            resp = get_fetcher(provider).fetch(page["url"], timeout=timeout)
        except Exception as e:                   # noqa: BLE001
            # adapter 自己抛（缺 key / 缺配置）——记一行 harness 故障，整轮继续
            return {"pid": page["pid"], "url": page["url"], "provider": provider,
                    "status": "error", "text": "", "len_norm": 0, "latency_ms": 0.0,
                    "http_status": None, "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                    "failure_reason": "normalizer_crashed", "fault": "harness",
                    "cache_pinned": False, "raw_meta": {}}
        last = resp
        if resp.status == "ok" or resp.failure_reason not in RETRY_REASONS:
            break
        if i < attempts - 1:
            time.sleep(3 * (i + 1))
    return {"pid": page["pid"], "url": page["url"], **asdict(last)}


def run(pageset_path: str | Path, providers: list[str], out_dir: str | Path, *,
        limit: int | None = None, repeat: int = 1, concurrency: int = 4,
        timeout: int = 60, pace: dict[str, float] | None = None,
        allow_env_override: bool = False) -> Path:
    pages = load_jsonl(Path(pageset_path))
    pages.sort(key=lambda p: p["pid"])
    if limit:
        pages = pages[:limit]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "extractions.jsonl"
    done = done_keys(out)
    pace = pace or {}

    # **起跑前验一次凭据来源**（playbook §5.6：隐式开关会静默失效）。
    # `_load_dotenv` 不覆盖已存在的环境变量，shell 里残留的旧 key 会压过 .env 里的新 key，
    # 而两边看起来都"设好了"—— 实测因此白跑过一整轮 firecrawl。
    div = env_divergence(providers)
    if div and not allow_env_override:
        raise RuntimeError(
            "以下凭据 .env 与 shell 环境不一致，**实际生效的是 shell 那个**：\n"
            + "\n".join("  %s: .env=%s  shell=%s（生效）" % (k, a, b)
                         for k, (a, b) in sorted(div.items()))
            + "\n先决定用哪一把（`unset` 掉 shell 里的，或改 .env），"
              "或加 --allow-env-override 明确接受 shell 的值。")

    jobs = [(p, prov, seq) for seq in range(repeat) for p in pages for prov in providers
            if (p["pid"], prov, seq) not in done]
    total = len(jobs)
    print("目标 %d 格（%d 页 x %d 家 x %d 轮），已完成 %d，待跑 %d"
          % (len(pages) * len(providers) * repeat, len(pages), len(providers), repeat,
             len(pages) * len(providers) * repeat - total, total))

    t0, n = time.time(), 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(fetch_once, p, prov, timeout=timeout,
                          pace=pace.get(prov, 0.0)): (p, prov, seq)
                for p, prov, seq in jobs}
        for fut in as_completed(futs):
            p, prov, seq = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:               # noqa: BLE001
                rec = {"pid": p["pid"], "url": p["url"], "provider": prov,
                       "status": "error", "text": "", "len_norm": 0, "latency_ms": 0.0,
                       "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                       "failure_reason": "normalizer_crashed", "fault": "harness"}
            rec["run_seq"] = seq
            append(out, rec)
            n += 1
            if (s := progress(n, total, t0)):
                print(s)
    return out


def _parse_pace(spec: str | None) -> dict[str, float]:
    if not spec:
        return {}
    out = {}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--providers", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repeat", type=int, default=1, help="抖动模式：同一格跑几次")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--allow-env-override", action="store_true",
                    help="明确接受 shell 环境里的凭据压过 .env")
    ap.add_argument("--pace", help='按家节流，如 "octen=2.5,firecrawl=6.5"（秒/请求）')
    a = ap.parse_args()
    out = run(a.pageset, a.providers, a.out, limit=a.limit, repeat=a.repeat,
              concurrency=a.concurrency, timeout=a.timeout, pace=_parse_pace(a.pace),
              allow_env_override=a.allow_env_override)
    print("写出 -> %s" % out)


if __name__ == "__main__":
    main()
