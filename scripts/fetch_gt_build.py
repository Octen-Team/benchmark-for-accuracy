"""Build ground truth: turn the page set into a frozen file carrying `gt`.

**Built in layers** — strong ground truth is only built where the metric genuinely needs
a vocabulary:

  document files  parse channel. The parse result is the ground truth: the best quality
                  in the set, needing neither a browser nor a key.
  static docs     headless browser. Clean HTML, retrieved in moments.
  render / SPA    headless browser. Rendering is the capability under test.
  anti-bot        real Chrome. The metric is whether the wall was passed, so no text
                  ground truth is required.
  robustness      no text ground truth needed; expect and probes are enough.

Rows are appended as they are built and resume on pid, so being killed midway does not
lose the work already done.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from src import fetch_gt as G
from src.fetch_io import append, progress

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _load(path: Path) -> list[dict]:
    # **Split on "\n" only — never splitlines().** The latter also splits on U+2028,
    # U+2029 and U+0085, which occur legitimately in page text and which json.dumps does
    # not escape. One record gets torn in half and surfaces as "Unterminated string".
    return [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]


def _done_keys(path: Path) -> set[tuple[str, str]]:
    """Completed (pid, channel) keys. **The channel has to be part of the key**: the
    protection tier comes from comparing the two channels, and deduplicating on pid alone
    skips the second channel entirely, leaving the tier permanently uncomputable."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        try:
            r = json.loads(line)
            out.add((r["pid"], (r.get("gt") or {}).get("channel", "?")))
        except Exception:                        # noqa: BLE001  skip a corrupt line
            continue
    return out


def consolidate(path: Path) -> int:
    """Merge a pid's per-channel rows into one: content comes from **whichever channel
    succeeded** (real Chrome preferred), and both channels' success flags are kept so the
    protection tier can be derived."""
    rows = _load(path)
    merged: dict[str, dict] = {}
    for r in rows:
        pid = r["pid"]
        g = r.get("gt") or {}
        if pid not in merged:
            merged[pid] = r
            continue
        prev = merged[pid]
        pg = prev.get("gt") or {}
        flags = {k: v for k, v in list(pg.items()) + list(g.items())
                 if k in ("headless_ok", "chrome_ok")}
        # Prefer real Chrome when it retrieved content; otherwise keep the row that has it
        better = r if (g.get("channel") == "chrome_real" and g.get("vocab_n")) else (
            r if (g.get("vocab_n") and not pg.get("vocab_n")) else prev)
        better = json.loads(json.dumps(better))          # do not mutate the original
        better["gt"].update(flags)
        merged[pid] = better
    with path.open("w", encoding="utf-8") as f:
        for r in merged.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows) - len(merged)


def _channel_of(page: dict, browser_channel: str) -> str:
    """Which channel this page takes: anything non-HTML goes to the parse channel."""
    return "parse" if page["doc_type"] != "html" else browser_channel


def _raw_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))


def build_parse(page: dict, timeout: int, sidecar: Path | None = None) -> dict:
    """Parse channel: download raw bytes, sniff the real type, parse accordingly."""
    try:
        r = requests.get(page["url"], timeout=timeout, headers={"User-Agent": UA})
    except Exception as e:                       # noqa: BLE001
        return {"channel": "parse", "rule": "fetch_failed",
                "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
    dt, rule = G.sniff_doc_type(r.content, r.headers.get("content-type", ""),
                                page.get("doc_type", "unknown"))
    parsed = G.parse_document(r.content, dt, page["url"])
    _save_raw(sidecar, page["pid"], parsed["text"], "")
    v = G.derive_vocab(parsed["text"], "")       # documents have no chrome to exclude
    return {"channel": "parse", "http_status": r.status_code,
            "doc_type_observed": dt, "doc_type_rule": rule,
            "struct": parsed["struct"], "rule": parsed["rule"],
            "title": page["url"].rsplit("/", 1)[-1],
            "text_len": len(parsed["text"]), **v}


def _save_raw(sidecar: Path | None, pid: str, main: str, boiler: str) -> None:
    """Keep a copy of the raw body and chrome text. **Changing how anything is derived
    would otherwise mean re-running the whole browser pass**, which is expensive; with
    the sidecar, `--rederive` recomputes offline."""
    if not sidecar:
        return
    append(sidecar, {"pid": pid, "main_text": main, "boiler_text": boiler})


def build_browser(page: dict, timeout: int, shots: str | None,
                  sidecar: Path | None = None,
                  channel_name: str = "playwright_headless") -> dict:
    """Browser channel: after rendering, take both the body and the chrome, and derive
    the anchors that are only visible once rendered."""
    got = G.render_page(page["url"], channel=channel_name, timeout=timeout,
                        screenshot_dir=shots, pid=page["pid"])
    if got.get("rule") != "rendered":
        flag = "chrome_ok" if channel_name == "chrome_real" else "headless_ok"
        return {"channel": channel_name, flag: False,
                "rule": got.get("rule", "render_failed"),
                "error": got.get("error"), "http_status": got.get("http_status")}
    _save_raw(sidecar, page["pid"], got["main_text"], got["boiler_text"])
    v = G.derive_vocab(got["main_text"], got["boiler_text"])
    walled = G.gt_is_walled(got["main_text"], got.get("title", ""))
    render_anchors: list[str] = []
    if page["type"] == "render":
        # For render pages the metric needs words that only appear after JavaScript:
        # the rendered result minus the raw HTTP response body
        try:
            raw = requests.get(page["url"], timeout=timeout,
                               headers={"User-Agent": UA}).text
            before = set(G.tokenize(_raw_text(raw)))
            render_anchors = [t for t in v["vocab"] if t not in before][:12]
        except Exception:                        # noqa: BLE001
            render_anchors = []
    ok = bool(v.get("vocab_n")) and not walled
    flag = "chrome_ok" if channel_name == "chrome_real" else "headless_ok"
    return {"channel": channel_name, flag: ok,
            # A wall renders "successfully" and produces a vocabulary; vocab_n alone
            # cannot catch that, so it has to be flagged explicitly
            "rule": "rendered_wall" if walled else "rendered",
            "gt_wall_hit": walled,
            "http_status": got.get("http_status"), "title": got.get("title", ""),
            "struct": got.get("struct", {}), "screenshot_path": got.get("screenshot_path"),
            "render_anchors": render_anchors,
            "text_len": len(got["main_text"]), **v}


def backfill_gaps(path: Path) -> list[dict]:
    """Flag ground-truth gaps. **Both kinds have to be flagged:**

      could not build   a page where `expect == content` yet no vocabulary exists
      built a wall      the render "succeeded" and produced a vocabulary, but what came
                        back was a challenge screen. vocab_n alone cannot catch this, and
                        unflagged it means every provider is compared against a challenge
                        page.

    A gap page keeps its vocabulary in the file for inspection, but the judge skips it.
    """
    rows = _load(path)
    gaps = []
    for r in rows:
        g = r["gt"] or {}
        walled = bool(g.get("gt_wall_hit"))
        empty = r.get("expect") == "content" and not g.get("vocab_n")
        g["gt_gap"] = walled or empty
        if g["gt_gap"]:
            gaps.append({"pid": r["pid"], "host": r.get("host"),
                         "why": "walled" if walled else "no_vocab",
                         "rule": g.get("rule")})
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return gaps


def backfill_strength(path: Path) -> dict:
    """Protection strength is the comparison of the two ground-truth channels.

    **When real Chrome never ran the answer is unknown, never hard.** Defaulting to hard
    disguises "not measured" as "measured, hardest" and invents a tier that was never
    established.
    """
    rows = _load(path)
    counts: Counter = Counter()
    for r in rows:
        g = r["gt"] or {}
        # Rows built by an older version carry no explicit flags; infer them once from
        # the channel and the result (success = has a vocabulary and is not a wall)
        if "headless_ok" not in g and g.get("channel") == "playwright_headless":
            g["headless_ok"] = bool(g.get("vocab_n")) and not g.get("gt_wall_hit")
        if "chrome_ok" not in g and g.get("channel") == "chrome_real":
            g["chrome_ok"] = bool(g.get("vocab_n")) and not g.get("gt_wall_hit")
        g["strength"] = G.derive_strength(
            g.get("headless_ok") if "headless_ok" in g else None,
            g.get("chrome_ok") if "chrome_ok" in g else None)
        counts[g["strength"]] += 1
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return dict(counts)


def backfill_anchors(path: Path) -> int:
    """**A second pass**: collect every page's vocabulary to compute document frequency,
    then choose each page's distinctive anchors.

    It has to be a second pass — "distinctive" is relative to the whole set, and while
    building the first page nothing is known about the others. Skipping it fails
    silently: `gt.anchors` is missing, `identity_ok` always returns None, and the
    wrong-page veto **looks implemented but never fires**. That veto catches the worst
    class of silent failure — falling back to a parent, index or search page — which
    nothing downstream can detect.
    """
    rows = _load(path)
    n = len(rows)
    df: Counter = Counter()
    for r in rows:
        g = r["gt"] or {}
        # **Title words count towards document frequency too.** The parse channel uses
        # the filename as the title, so format words appear only there; counting the
        # vocabulary alone gives them a frequency of zero and they slip into the anchors.
        terms = set(g.get("vocab") or []) | set(G.tokenize(g.get("title") or ""))
        df.update(terms)
    filled = 0
    for r in rows:
        g = r["gt"] or {}
        anchors = G.derive_anchors(g.get("title") or "",
                                   g.get("vocab_head") or g.get("vocab") or [],
                                   df, n, url=r.get("url", ""))
        g["anchors"] = anchors
        g["anchors_df_pages"] = n          # anchors are relative to these n pages
        if anchors:
            filled += 1
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return filled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--types", nargs="+", default=["docfmt", "baseline", "render"])
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=6, help="parse channel only")
    ap.add_argument("--shots", default=None, help="screenshot directory (browser channel)")
    ap.add_argument("--channel", default="playwright_headless",
                    choices=["playwright_headless", "chrome_real"],
                    help="browser channel; chrome_real launches the locally installed "
                         "Chrome (headed, clean profile, never signed in)")
    ap.add_argument("--sidecar", help="keep raw body text so --rederive can recompute offline")
    ap.add_argument("--anchors-only", action="store_true",
                    help="run only the anchor backfill (second pass); fetch nothing")
    a = ap.parse_args()

    sidecar = Path(a.sidecar) if a.sidecar else None
    if a.anchors_only:
        gg = backfill_gaps(Path(a.out))
        print("%d ground-truth gaps: %s" % (len(gg), [(g["pid"], g["why"]) for g in gg]))
        print("anchor backfill: %d pages have distinctive anchors" % backfill_anchors(Path(a.out)))
        print("protection tiers: %s" % backfill_strength(Path(a.out)))
        return
    pages = [p for p in _load(Path(a.pageset)) if p["type"] in set(a.types)]
    pages.sort(key=lambda p: p["pid"])
    if a.limit:
        pages = pages[:a.limit]
    out = Path(a.out)
    done = _done_keys(out)
    todo = [p for p in pages if (p["pid"], _channel_of(p, a.channel)) not in done]
    print("target %d pages, %d already built, %d to build"
          % (len(pages), len(pages) - len(todo), len(todo)))

    # **Route by doc_type, not by page type.** A suffix-less PDF may sit in the
    # robustness type, but sending it to a browser only yields the PDF viewer shell.
    # Unknown also takes the parse channel, where sniffing settles it.
    parse_jobs = [p for p in todo if p["doc_type"] != "html"]
    browser_jobs = [p for p in todo if p["doc_type"] == "html"]
    t0, n = time.time(), 0
    total = len(todo)

    # Parse channel: plain HTTP, safe to run concurrently
    if parse_jobs:
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = {ex.submit(build_parse, p, a.timeout, sidecar): p for p in parse_jobs}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    gt = fut.result()
                except Exception as e:           # noqa: BLE001
                    gt = {"channel": "parse", "rule": "builder_crashed",
                          "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
                append(out, {**p, "gt": gt})
                n += 1
                if (s := progress(n, total, t0)):
                    print(s)

    # Browser channel: the synchronous API runs sequentially, one driver at a time
    for p in browser_jobs:
        try:
            gt = build_browser(p, a.timeout, a.shots, sidecar, a.channel)
        except Exception as e:                   # noqa: BLE001
            gt = {"channel": "playwright_headless", "rule": "builder_crashed",
                  "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
        append(out, {**p, "gt": gt})
        n += 1
        if (s := progress(n, total, t0)):
            print(s)

    dropped = consolidate(out)
    if dropped:
        print("merged multi-channel rows: %d duplicates folded in" % dropped)
    walled = [r["pid"] for r in _load(out) if r["gt"].get("gt_wall_hit")]
    if walled:
        print("!! ground truth itself was blocked on %d pages (flagged as gaps; "
              "otherwise every provider is compared against a challenge screen): %s"
              % (len(walled), walled))
    gaps = backfill_gaps(out)
    if gaps:
        print("%d ground-truth gaps (the judge skips their vocabulary): %s"
              % (len(gaps), [(g["pid"], g["why"]) for g in gaps]))
    filled = backfill_anchors(out)
    print("anchor backfill: %d pages have distinctive anchors" % filled)
    print("protection tiers: %s (unknown wherever real Chrome never ran; never hard by default)"
          % backfill_strength(out))
    rows = _load(out)
    ok = [r for r in rows if r["gt"].get("vocab_n")]
    degen = [r["pid"] for r in rows if r["gt"].get("degenerate")]
    failed = [(r["pid"], r["gt"].get("rule")) for r in rows if not r["gt"].get("vocab_n")]
    print("\nwrote %d rows; %d carry a vocabulary" % (len(rows), len(ok)))
    print("degenerate pages (vocab_n below the minimum; judged by panel, not by "
          "threshold): %d %s" % (len(degen), degen))
    if failed:
        # Say so out loud; an empty gt left in place reads downstream as "this page has
        # no content"
        print("!! no vocabulary could be built for %d pages: %s" % (len(failed), failed))


if __name__ == "__main__":
    main()
