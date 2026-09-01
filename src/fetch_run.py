"""The fetch round: one row per cell, resumable, with per-provider pacing.

**Search-style retry rules do not apply here.** A search runner retries when fewer than
k results come back; fetch has no k. This retries only on transport errors, 429 and 5xx.
**An HTTP 200 with an empty body is never retried** — that is the real behaviour of the
system under test, and retrying it would launder "could not retrieve" into "retrieved".

In repeat mode (`--repeat`) the resume key is `(pid, provider, run_seq)`, not
`(pid, provider)`; otherwise a resumed run treats the second round as already done and
skips it.
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
    # **Split on "\n" only — never splitlines().** The latter also splits on U+2028,
    # U+2029 and U+0085, which occur legitimately in page text and which json.dumps does
    # not escape. One complete record then gets torn in half and surfaces as a baffling
    # "Unterminated string". Real page text does contain these.
    return [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]


def done_keys(path: Path) -> set[tuple[str, str, int]]:
    """Completed (pid, provider, run_seq) keys. A corrupt line is skipped rather than
    preventing the whole round from starting."""
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
    """Per-provider pacing. Several providers reject requests above a certain rate; an
    unpaced round measures their rate limits instead of their fetch capability."""
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
    """One cell. Retries cover transport and rate limiting only, never an empty body."""
    last = None
    for i in range(attempts):
        _pace(provider, pace)
        try:
            resp = get_fetcher(provider).fetch(page["url"], timeout=timeout)
        except Exception as e:                   # noqa: BLE001
            # The adapter itself raised (missing key or config): record a harness fault
            # and keep the round going.
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

    # **Verify where the credentials come from before starting.** `_load_dotenv` does
    # not override variables that already exist, so a stale key left in the shell
    # silently wins over the new one in .env while both look correctly configured. That
    # failure mode costs a whole round and is invisible from the results.
    div = env_divergence(providers)
    if div and not allow_env_override:
        raise RuntimeError(
            "These credentials differ between .env and the shell environment. "
            "**The shell value is the one in effect:**\n"
            + "\n".join("  %s: .env=%s  shell=%s (in effect)" % (k, a, b)
                         for k, (a, b) in sorted(div.items()))
            + "\nDecide which one you mean (unset the shell variable, or update .env), "
              "or pass --allow-env-override to accept the shell value explicitly.")

    jobs = [(p, prov, seq) for seq in range(repeat) for p in pages for prov in providers
            if (p["pid"], prov, seq) not in done]
    total = len(jobs)
    print("target %d cells (%d pages x %d providers x %d rounds), %d done, %d to run"
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

    # Persist the run parameters. The report layer needs them to state **which providers
    # were paced** — a paced provider's latency includes deliberate waiting and is not
    # comparable across providers. Hard-coding that sentence into the report template
    # makes it lie for any other set of providers.
    (out_dir / "run_meta.json").write_text(
        json.dumps({"providers": sorted(providers), "pace": pace, "repeat": repeat,
                    "concurrency": concurrency, "timeout": timeout},
                   ensure_ascii=False, indent=1), encoding="utf-8")
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
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each cell this many times, to measure round-to-round variance")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--allow-env-override", action="store_true",
                    help="explicitly accept shell credentials overriding .env")
    ap.add_argument("--pace",
                    help='per-provider pacing, e.g. "a=2.5,b=6.5" (seconds per request)')
    a = ap.parse_args()
    out = run(a.pageset, a.providers, a.out, limit=a.limit, repeat=a.repeat,
              concurrency=a.concurrency, timeout=a.timeout, pace=_parse_pace(a.pace),
              allow_env_override=a.allow_env_override)
    print("wrote -> %s" % out)


if __name__ == "__main__":
    main()
