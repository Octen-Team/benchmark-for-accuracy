"""Add **weak anchors** to ground-truth gap pages: the title and snippet for that URL
from a neutral SERP.

On pages where our own browser is blocked, no reference vocabulary can be built, and
those cells fall back to the panel judging with no reference at all — which is
systematically lenient. Weak anchors cannot restore "how complete is the content", but
they are enough to arm the **identity** veto, and "returned the home page instead" is
exactly the failure an unreferenced panel most often waves through.

**Why a SERP is acceptable here**: a general search engine is a neutral third party in
this evaluation and is not among the systems under test. **Never** build a reference with
some provider's own unblocking product — that hands the exam to a contestant, and such a
provider may itself be on the roster.

Anchors take the **title** (almost certain to appear in the page, so the most reliable
signal) plus content words from the snippet, and record `anchor_source: "serp"` so the
report can state how many verdicts rested on weak anchors.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from src import fetch_gt as G
from src.serp import serp_fetch


def _norm(u: str) -> str:
    p = urlparse(u.lower())
    return (p.netloc + p.path).rstrip("/")


def serp_anchors(url: str, k: int = 8) -> dict:
    """Look up this URL's title and snippet on a SERP. A hit requires the **normalised
    URLs to be equal**, not one containing the other: containment would accept a
    different page on the same domain as this one."""
    target = _norm(url)
    parsed = urlparse(url)
    host = parsed.netloc
    # Turn the path into words. Feeding the raw path, with its slashes and hyphens,
    # straight in as a query finds almost nothing.
    slug = re.sub(r"[^a-z0-9]+", " ", parsed.path.lower()).strip()
    slug = " ".join(w for w in slug.split() if len(w) > 1)[:80]
    hits = []
    queries = ['"%s"' % url, url]
    if slug:
        queries.append("site:%s %s" % (host, slug))
    for query in queries:
        try:
            items = serp_fetch(query, engine="google", k=k)
        except Exception as e:                   # noqa: BLE001
            return {"anchor_source": "serp_failed", "error": str(e)[:150]}
        hits = [i for i in items if _norm(i.url) == target]
        if hits:
            break
        time.sleep(1)
    if not hits:
        return {"anchor_source": "serp_no_hit"}
    it = hits[0]
    title_terms = G._content_terms(it.title, set(), 6)
    snip_terms = [t for t in G._content_terms(it.snippet, set(title_terms), 6)]
    return {"anchor_source": "serp", "serp_title": it.title,
            "serp_snippet": it.snippet[:300],
            # Title words are the most reliable; the snippet contributes a couple more
            "anchors": (title_terms + snip_terms[:2])[:6]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True,
                    help="page set carrying ground truth; rewritten in place")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    path = Path(a.pageset)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
    gaps = [r for r in rows if (r.get("gt") or {}).get("gt_gap")]
    if a.limit:
        gaps = gaps[:a.limit]
    print("%d ground-truth gap pages; querying a neutral SERP for each" % len(gaps))

    got = 0
    for i, r in enumerate(gaps, 1):
        res = serp_anchors(r["url"])
        g = r["gt"]
        g["anchor_source"] = res["anchor_source"]
        for k in ("serp_title", "serp_snippet", "error"):
            if k in res:
                g[k] = res[k]
        if res.get("anchors"):
            g["anchors"] = res["anchors"]
            got += 1
        print("  %-6s %-26s %-14s %s" % (r["pid"], r["host"][:26], res["anchor_source"],
                                         res.get("anchors") or res.get("error", "")))
        time.sleep(1)

    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\nweak anchors obtained for %d / %d (the rest still judge without a reference)"
          % (got, len(gaps)))


if __name__ == "__main__":
    main()
