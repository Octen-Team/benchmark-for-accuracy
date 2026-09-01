"""Reclassify failure reasons offline. The original error text is stored, so nothing
needs re-fetching.

Use this after a rule change — for example splitting 403 into "refused by policy" and
"genuinely blocked" — to update existing fetch and verdict files in place. **Verdict
bands are unaffected** (a transport failure is lost either way); only the attribution
column changes.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from src.fetch_backends import classify_body


def reclassify(rec: dict) -> bool:
    if rec.get("status") != "error":
        return False
    told = classify_body(rec.get("http_status") or 0, rec.get("error") or "")
    if not told:
        return False
    reason, fault = told
    if rec.get("failure_reason") == reason:
        return False
    rec["failure_reason"], rec["fault"] = reason, fault
    if isinstance(rec.get("reason"), str) and rec["reason"].startswith("fetch_failed:"):
        rec["reason"] = "fetch_failed:%s" % reason
    return True


def sync_from(extractions: Path, rows: list[dict]) -> int:
    """Copy the attribution from the fetch rows across to the verdict rows.

    **Verdict rows carry no `http_status` or `error`**, so they cannot be reclassified on
    their own; the values have to be carried over by (pid, provider). Without this the
    two files disagree, and since the report reads the verdict file, the
    reclassification would have no effect.
    """
    src = {}
    for line in extractions.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        r = json.loads(line)
        src[(r["pid"], r["provider"], r.get("run_seq", 0))] = (
            r.get("failure_reason"), r.get("fault"))
    n = 0
    for rec in rows:
        key = (rec.get("pid"), rec.get("provider"), rec.get("run_seq", 0))
        if key not in src:
            continue
        reason, fault = src[key]
        if reason and rec.get("failure_reason") != reason:
            rec["failure_reason"], rec["fault"] = reason, fault
            if isinstance(rec.get("reason"), str) and rec["reason"].startswith("fetch_failed:"):
                rec["reason"] = "fetch_failed:%s" % reason
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="extractions.jsonl / verdicts.jsonl")
    ap.add_argument("--sync-from",
                    help="fetch results to copy attributions from into the verdict file")
    a = ap.parse_args()
    for f in a.files:
        p = Path(f)
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").split("\n") if l.strip()]
        n = sum(1 for r in rows if reclassify(r))
        if a.sync_from:
            n += sync_from(Path(a.sync_from), rows)
        with p.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        after = Counter(r.get("failure_reason") for r in rows if r.get("failure_reason"))
        print("%s: updated %d rows" % (p, n))
        for k, v in after.most_common(5):
            print("    %-22s %d" % (k, v))


if __name__ == "__main__":
    main()
