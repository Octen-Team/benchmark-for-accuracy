"""Collect the marks exported from the human review page into the gold file.

Gold has **the highest priority**: the judge adopts it directly, overriding both the
mechanical layer and the panel. These pages start as the weakest evidence in the set —
our own browser cannot fetch them, so no reference answer exists — and become the
strongest once a person has checked them. The verdicts are reused by every later round,
for as long as that cell's fetched content is unchanged.

`unsure` is never written to gold: an uncertain cell should keep going to the panel, and
a reviewer's "I cannot tell" must not harden into a conclusion.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID = {"pass", "partial", "lost"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--marks", help="JSONL exported by the review page; reads stdin if omitted")
    ap.add_argument("--out", default="data/fetch_gold_gap.jsonl")
    ap.add_argument("--extractions", required=True,
                    help="source of the text fingerprint; gold expires when the text changes")
    a = ap.parse_args()

    raw = (Path(a.marks).read_text(encoding="utf-8") if a.marks else sys.stdin.read())
    marks = [json.loads(l) for l in raw.split("\n") if l.strip()]

    import hashlib
    fp = {}
    for line in Path(a.extractions).read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("run_seq", 0) == 0:
            fp[(r["pid"], r["provider"])] = hashlib.sha256(
                (r.get("text") or "").encode("utf-8", "replace")).hexdigest()[:16]

    out, skipped = [], 0
    for m in marks:
        v = m.get("human_verdict")
        if v not in VALID:
            skipped += 1              # unsure or blank: keep sending it to the panel
            continue
        key = (m["pid"], m["provider"])
        out.append({"pid": m["pid"], "provider": m["provider"], "verdict": v,
                    "text_sha": fp.get(key, "")})

    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("wrote %d gold entries -> %s (skipped %d uncertain, which keep going to the panel)"
          % (len(out), path, skipped))
    missing = sum(1 for r in out if not r["text_sha"])
    if missing:
        print("!! %d marks have no matching fetched text; their gold will not apply" % missing)


if __name__ == "__main__":
    main()
