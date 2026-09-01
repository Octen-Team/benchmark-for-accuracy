"""A human review page for ground-truth gap pages.

Our own browser cannot fetch these either, so their verdicts rest entirely on the
panel — the weakest evidence in the set. There are few of them, and one pass by a
person turns them from the least trustworthy cells into gold, which every later
round then reuses.

The page lists every provider's return for a page **side by side**, together with
the only reference available (a neutral search engine's title and snippet) and the
current verdict. The reviewer answers one question: is this the content of this
page.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

PREVIEW = 420

CSS = """
:root{--rv-bg:#faf9f7;--rv-surface:#fff;--rv-ink:#1c1b19;--rv-mut:#726d66;
  --rv-line:#e4e1dc;--rv-accent:#7a5c2e;--rv-ok:#2f7d52;--rv-mid:#a8761c;--rv-bad:#a63d2f;
  --rv-code:#f0ede8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --rv-bg:#171614;--rv-surface:#1f1e1b;--rv-ink:#eae7e2;--rv-mut:#9e9891;
  --rv-line:#302e2a;--rv-accent:#c9a468;--rv-ok:#5cb98a;--rv-mid:#d3a54c;--rv-bad:#d9705e;
  --rv-code:#26241f}}
:root[data-theme="dark"]{--rv-bg:#171614;--rv-surface:#1f1e1b;--rv-ink:#eae7e2;
  --rv-mut:#9e9891;--rv-line:#302e2a;--rv-accent:#c9a468;--rv-ok:#5cb98a;
  --rv-mid:#d3a54c;--rv-bad:#d9705e;--rv-code:#26241f}
*{box-sizing:border-box}
body{background:var(--rv-bg);color:var(--rv-ink);margin:0;padding:2.5rem 1.25rem 5rem;
  font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
.rv-wrap{max-width:960px;margin:0 auto;display:flex;flex-direction:column;gap:2rem}
.rv-h1{font:600 1.9rem/1.2 ui-serif,Georgia,serif;margin:0 0 .4rem;letter-spacing:-.01em}
.rv-lede{color:var(--rv-mut);margin:0;max-width:64ch}
.rv-page{background:var(--rv-surface);border:1px solid var(--rv-line);border-radius:10px;
  padding:1.1rem 1.25rem;display:flex;flex-direction:column;gap:.75rem}
.rv-head{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline}
.rv-pid{font:600 .8rem/1 ui-monospace,Menlo,monospace;color:var(--rv-mut)}
.rv-url{font-weight:600;word-break:break-all}
.rv-tag{font-size:.74rem;padding:.12rem .45rem;border-radius:4px;border:1px solid var(--rv-line);
  color:var(--rv-mut);white-space:nowrap}
.rv-ref{background:var(--rv-code);border-radius:7px;padding:.6rem .8rem;font-size:.86rem}
.rv-ref b{color:var(--rv-accent)}
.rv-none{color:var(--rv-mut);font-style:italic}
.rv-rows{display:flex;flex-direction:column;gap:.5rem}
.rv-row{display:grid;grid-template-columns:6.5rem 5.2rem 1fr;gap:.7rem;align-items:start;
  padding-top:.5rem;border-top:1px solid var(--rv-line)}
.rv-prov{font-weight:560}
.rv-v{font-size:.78rem;font-weight:640;letter-spacing:.02em}
.rv-pass{color:var(--rv-ok)}.rv-partial{color:var(--rv-mid)}.rv-lost{color:var(--rv-bad)}
.rv-null{color:var(--rv-mut)}
.rv-why{color:var(--rv-mut);font-size:.78rem;margin:.15rem 0 0}
.rv-text{font:.83rem/1.5 ui-monospace,Menlo,monospace;background:var(--rv-code);
  border-radius:6px;padding:.5rem .65rem;margin:0;white-space:pre-wrap;word-break:break-word;
  max-height:8.5rem;overflow:auto}
.rv-meta{color:var(--rv-mut);font-size:.85rem}

/* Marking */
.rv-mark{display:flex;gap:.25rem;flex-wrap:wrap;margin:.3rem 0 0}
.rv-mark button{font:500 .74rem/1 ui-sans-serif,sans-serif;padding:.3rem .5rem;
  border:1px solid var(--rv-line);border-radius:5px;background:transparent;
  color:var(--rv-mut);cursor:pointer}
.rv-mark button:hover{border-color:var(--rv-accent);color:var(--rv-ink)}
.rv-mark button:focus-visible{outline:2px solid var(--rv-accent);outline-offset:1px}
.rv-mark button[aria-pressed="true"]{background:var(--rv-accent);border-color:var(--rv-accent);
  color:var(--rv-bg);font-weight:640}
.rv-row.rv-done{background:color-mix(in srgb,var(--rv-accent) 7%,transparent);
  border-radius:6px}
.rv-accept{font:500 .76rem/1 ui-sans-serif,sans-serif;padding:.32rem .6rem;
  border:1px solid var(--rv-line);border-radius:5px;background:transparent;
  color:var(--rv-mut);cursor:pointer;align-self:flex-start}
.rv-accept:hover{border-color:var(--rv-accent);color:var(--rv-ink)}
.rv-bar{position:sticky;bottom:0;background:var(--rv-surface);
  border:1px solid var(--rv-line);border-radius:10px;padding:.8rem 1rem;
  display:flex;flex-wrap:wrap;gap:.8rem;align-items:center;
  box-shadow:0 -2px 12px rgba(0,0,0,.06)}
.rv-prog{font-variant-numeric:tabular-nums;font-weight:600}
.rv-bar button{font:600 .82rem/1 ui-sans-serif,sans-serif;padding:.45rem .8rem;
  border:1px solid var(--rv-accent);border-radius:6px;background:var(--rv-accent);
  color:var(--rv-bg);cursor:pointer}
.rv-bar button.rv-ghost{background:transparent;color:var(--rv-mut);
  border-color:var(--rv-line)}
.rv-out{width:100%;min-height:9rem;font:.78rem/1.5 ui-monospace,Menlo,monospace;
  background:var(--rv-code);color:var(--rv-ink);border:1px solid var(--rv-line);
  border-radius:7px;padding:.6rem;display:none}
.rv-out.rv-show{display:block}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _v_class(v):
    return {"pass": "rv-pass", "partial": "rv-partial", "lost": "rv-lost"}.get(v, "rv-null")


def build(pageset: Path, extractions: Path, verdicts: Path) -> str:
    rows = [json.loads(l) for l in pageset.read_text(encoding="utf-8").split("\n") if l.strip()]
    pages = {r["pid"]: r for r in rows}
    ext, ver = {}, {}
    for p, store in ((extractions, ext), (verdicts, ver)):
        for line in p.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("run_seq", 0) == 0:
                store[(r["pid"], r["provider"])] = r
    gaps = [r for r in rows if (r.get("gt") or {}).get("gt_gap")]
    gaps.sort(key=lambda r: r["pid"])
    provs = sorted({k[1] for k in ext})

    H = ["<title>Gap page review</title>", "<style>%s</style>" % CSS,
         '<div class="rv-wrap">',
         '<div><h1 class="rv-h1">%d pages the automated judge cannot settle</h1>'
         '<p class="rv-lede">Our own browser cannot fetch these either, so '
         'there is no reference answer and the verdicts rest entirely on the '
         'model panel &mdash; the weakest evidence in the set. Each page lists '
         'every provider&rsquo;s return side by side, with the only reference '
         'available: the title and snippet a neutral search engine holds for '
         'this URL. You only need to answer one question: <strong>is this the '
         'content of this page?</strong></p></div>']

    for r in gaps:
        g = r.get("gt") or {}
        tags = [t for t in (r.get("antibot_subclass"),
                            {"hard": "hard", "medium": "medium",
                             "soft": "soft"}.get(g.get("strength")),
                            "challenge screen" if g.get("gt_wall_hit") else None,
                            "not indexed anywhere"
                            if g.get("anchor_source") != "serp" else None) if t]
        H.append('<div class="rv-page">')
        H.append('<div class="rv-head"><span class="rv-pid">%s</span>'
                 '<span class="rv-url">%s</span>%s</div>'
                 % (r["pid"], html.escape(r["url"]),
                    "".join('<span class="rv-tag">%s</span>' % html.escape(t) for t in tags)))
        if g.get("serp_title"):
            H.append('<div class="rv-ref"><b>Indexed title</b>: %s<br>'
                     '<b>Snippet</b>: %s</div>'
                     % (html.escape(g["serp_title"]),
                        html.escape(g.get("serp_snippet") or "(none)")))
        else:
            H.append('<div class="rv-ref rv-none">No search engine has '
                     'indexed this URL either, so there is no reference at '
                     'all &mdash; only the side-by-side comparison between '
                     'providers.</div>')
        H.append('<div class="rv-rows">')
        for prov in provs:
            e, v = ext.get((r["pid"], prov)), ver.get((r["pid"], prov))
            if not e:
                continue
            verdict = (v or {}).get("verdict")
            why = ""
            pan = (v or {}).get("panel") or {}
            votes = pan.get("votes") or {}
            if votes:
                first = next(iter(votes.values()))
                why = first.get("why") or ""
            text = (e.get("text") or "").strip()
            body = (html.escape(text[:PREVIEW]) + ("…" if len(text) > PREVIEW else "")
                    ) if text else '<span class="rv-none">(this provider errored: %s)</span>' % html.escape(
                        e.get("failure_reason") or "error")
            key = "%s|%s" % (r["pid"], prov)
            marks = "".join(
                '<button type="button" data-k="%s" data-v="%s" aria-pressed="false">%s</button>'
                % (key, code, label)
                for code, label in (("pass", "correct"), ("partial", "partial"),
                                    ("lost", "wrong"), ("unsure", "not sure")))
            H.append('<div class="rv-row" data-row="%s"><div class="rv-prov">%s</div>'
                     '<div><span class="rv-v %s">%s</span>'
                     '<p class="rv-why">%s</p>'
                     '<div class="rv-mark">%s</div></div>'
                     '<pre class="rv-text">%s</pre></div>'
                     % (key, prov, _v_class(verdict), verdict or "unjudged",
                        html.escape((why or "")[:90]), marks, body))
        accept = json.dumps({("%s|%s" % (r["pid"], p)): (ver.get((r["pid"], p), {}) or {}).get("verdict")
                             for p in provs if (r["pid"], p) in ext}, ensure_ascii=False)
        H.append('<button type="button" class="rv-accept" data-accept=\'%s\'>'
                 'Every current verdict on this page is correct &mdash; accept '
                 'all</button>' % html.escape(accept, quote=False))
        H.append("</div></div>")

    total_cells = sum(1 for r in gaps for p in provs if (r["pid"], p) in ext)
    H.append('<div class="rv-bar">'
             '<span class="rv-prog"><span id="rv-n">0</span> / %d marked</span>'
             '<button type="button" id="rv-export">Export</button>'
             '<button type="button" class="rv-ghost" id="rv-copy">Copy</button>'
             '<button type="button" class="rv-ghost" id="rv-clear">Clear</button>'
             '<span class="rv-meta">Marks are stored in this browser and '
             'survive a refresh.</span>'
             '<textarea class="rv-out" id="rv-out" readonly '
             'aria-label="exported marks"></textarea></div>' % total_cells)
    H.append('<p class="rv-meta">%d pages &times; %d providers = %d cells. '
             'Save the export as <code>data/fetch_gold_gap.jsonl</code> and '
             're-run judging: a human verdict has the highest priority, '
             'overrides the panel, and is reused by every later round.</p>'
             % (len(gaps), len(provs), total_cells))
    H.append(_SCRIPT % total_cells)
    H.append("</div>")
    return "\n".join(H)


_SCRIPT = """<script>
(function () {
  var KEY = 'fetchGapReview.v1';
  var total = %d;
  var store = {};
  try { store = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { store = {}; }

  function paint() {
    document.querySelectorAll('.rv-mark button').forEach(function (b) {
      var on = store[b.dataset.k] === b.dataset.v;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    document.querySelectorAll('[data-row]').forEach(function (row) {
      row.classList.toggle('rv-done', !!store[row.dataset.row]);
    });
    document.getElementById('rv-n').textContent = Object.keys(store).length;
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(store)); } catch (e) {}
    paint();
  }

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('.rv-mark button');
    if (b) {
      // Clicking the same choice again clears it, so a reviewer can change their mind
      if (store[b.dataset.k] === b.dataset.v) { delete store[b.dataset.k]; }
      else { store[b.dataset.k] = b.dataset.v; }
      save();
      return;
    }
    var acc = ev.target.closest('[data-accept]');
    if (acc) {
      var m = JSON.parse(acc.dataset.accept);
      Object.keys(m).forEach(function (k) { if (m[k]) { store[k] = m[k]; } });
      save();
      return;
    }
    if (ev.target.id === 'rv-export' || ev.target.id === 'rv-copy') {
      var out = document.getElementById('rv-out');
      var lines = Object.keys(store).sort().map(function (k) {
        var p = k.split('|');
        return JSON.stringify({pid: p[0], provider: p[1], human_verdict: store[k]});
      });
      out.value = lines.join('\\n');
      out.classList.add('rv-show');
      if (ev.target.id === 'rv-copy') {
        out.select();
        try { navigator.clipboard.writeText(out.value); } catch (e) {
          try { document.execCommand('copy'); } catch (e2) {}
        }
      }
      return;
    }
    if (ev.target.id === 'rv-clear') {
      // Two-step confirmation rather than confirm(): a sandboxed iframe can block
      // dialogs, and a blocked confirm() returns undefined, which would make the
      // cancel branch swallow every clear attempt.
      var btn = ev.target;
      if (btn.dataset.armed !== '1') {
        if (!Object.keys(store).length) { return; }
        btn.dataset.armed = '1';
        btn.textContent = 'Click again to confirm';
        setTimeout(function () {
          btn.dataset.armed = ''; btn.textContent = 'Clear';
        }, 4000);
        return;
      }
      btn.dataset.armed = ''; btn.textContent = 'Clear';
      store = {}; save();
      var o = document.getElementById('rv-out');
      o.value = ''; o.classList.remove('rv-show');
    }
  });

  paint();
})();
</script>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pageset", required=True)
    ap.add_argument("--extractions", required=True)
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    html_text = build(Path(a.pageset), Path(a.extractions), Path(a.verdicts))
    Path(a.out).write_text(html_text, encoding="utf-8")
    print("review page -> %s (%.2f MB)" % (a.out, len(html_text.encode()) / 1e6))


if __name__ == "__main__":
    main()
