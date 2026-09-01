"""CSV -> page-set JSONL. **The input is copied into data/ and versioned** first; the
original file is left untouched.

This does mechanical transcription and label attachment only. Ground truth is written
back separately, into a `.gt.jsonl` sidecar.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from src import fetch_spec as S


def label_for(url: str) -> dict:
    """Attach per-page labels by URL substring. **When several rules match, probes are
    unioned** — one page can be both oversize and a raw direct link, and letting the
    later rule overwrite the earlier one silently drops a probe."""
    expect, probes = "content", []
    for frag, lab in S.PAGE_LABELS.items():
        if frag in url:
            expect = lab.get("expect", expect)
            probes.extend(lab.get("probes", []))
    return {"expect": expect, "probes": sorted(set(probes))}


def build(csv_path: Path, out_path: Path, *, run_assert: bool = True) -> list[dict]:
    with csv_path.open(encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    hosts = Counter(urlparse(r["url"]).netloc for r in raw)
    rows = []
    for i, r in enumerate(raw, 1):
        url, cat = r["url"].strip(), r["category"].strip()
        host = urlparse(url).netloc
        typ = S.CATEGORY_TO_TYPE[cat]      # an unknown category should raise, not default
        dt, rule = S.doc_type_from_url(url)
        rows.append({
            "pid": "p%03d" % i,
            "url": url,
            "host": host,
            "host_dup": hosts[host] > 1,
            "category": cat,
            "type": typ,
            "defended": typ == "antibot",
            "antibot_subclass": S.ANTIBOT_SUBCLASS.get(host) if typ == "antibot" else None,
            "doc_type": dt,
            "doc_type_rule": rule,
            **label_for(url),
        })
    if run_assert:
        S.assert_pageset(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--archive", help="copy the input CSV here to version it")
    ap.add_argument("--no-assert", action="store_true",
                    help="skip the page-count assertions (for small test samples only)")
    a = ap.parse_args()
    if a.archive:
        shutil.copy2(a.csv, a.archive)
    rows = build(Path(a.csv), Path(a.out), run_assert=not a.no_assert)
    print("wrote %d rows -> %s" % (len(rows), a.out))
    for t, n in sorted(Counter(r["type"] for r in rows).items()):
        print("  %-12s %d" % (t, n))
    sub = Counter(r["antibot_subclass"] for r in rows if r["antibot_subclass"])
    print("  anti-bot sub-classes %s" % dict(sub))
    missing = sorted({r["host"] for r in rows
                      if r["type"] == "antibot" and not r["antibot_subclass"]})
    if missing:
        # Letting None through silently leaves those pages unsliceable in the report
        print("  !! hosts with no anti-bot sub-class (add them to fetch_spec): %s" % missing)
    print("  %d pages carry a probe" % sum(1 for r in rows if r["probes"]))


if __name__ == "__main__":
    main()
