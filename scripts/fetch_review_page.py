"""GT 缺口页的人工核对页。

这些页我们自己的浏览器也抓不到，判定完全依赖面板 —— 是全场证据最弱的一批。
页数很少（本轮 18 页），人工扫一遍就能把它们从"最不可信"变成"金标"，
而且金标可以被后续每一轮复用。

页面把每一页的五家返回**并排**列出来，附上我们能拿到的唯一参考（中立搜索引擎的
标题与摘要），以及当前判定。人眼要做的只有一件事：这一段是不是这一页的内容。
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

/* 标注 */
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

    H = ["<title>缺口页人工核对</title>", "<style>%s</style>" % CSS,
         '<div class="rv-wrap">',
         '<div><h1 class="rv-h1">这 %d 页，机器判不准</h1>'
         '<p class="rv-lede">我们自己的浏览器也抓不到它们，所以没有参考答案，'
         '判定完全依赖模型面板 —— 全场证据最弱的一批。每页把五家的返回并排列出，'
         '附上唯一能拿到的参考（中立搜索引擎为这条 URL 收录的标题与摘要）。'
         '人眼只需回答一件事：<strong>这一段是不是这一页的内容。</strong></p></div>']

    for r in gaps:
        g = r.get("gt") or {}
        tags = [t for t in (r.get("antibot_subclass"),
                            {"hard": "硬档", "medium": "中档", "soft": "软档"}.get(g.get("strength")),
                            "验证页" if g.get("gt_wall_hit") else None,
                            "搜索引擎也没收录" if g.get("anchor_source") != "serp" else None) if t]
        H.append('<div class="rv-page">')
        H.append('<div class="rv-head"><span class="rv-pid">%s</span>'
                 '<span class="rv-url">%s</span>%s</div>'
                 % (r["pid"], html.escape(r["url"]),
                    "".join('<span class="rv-tag">%s</span>' % html.escape(t) for t in tags)))
        if g.get("serp_title"):
            H.append('<div class="rv-ref"><b>搜索引擎收录的标题</b>：%s<br><b>摘要</b>：%s</div>'
                     % (html.escape(g["serp_title"]),
                        html.escape(g.get("serp_snippet") or "（无）")))
        else:
            H.append('<div class="rv-ref rv-none">这条 URL 搜索引擎也没有收录 —— '
                     '完全没有参考，只能靠五家横向对比。</div>')
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
                    ) if text else '<span class="rv-none">（该服务报错：%s）</span>' % html.escape(
                        e.get("failure_reason") or "error")
            key = "%s|%s" % (r["pid"], prov)
            marks = "".join(
                '<button type="button" data-k="%s" data-v="%s" aria-pressed="false">%s</button>'
                % (key, code, label)
                for code, label in (("pass", "对"), ("partial", "部分"),
                                    ("lost", "错"), ("unsure", "拿不准")))
            H.append('<div class="rv-row" data-row="%s"><div class="rv-prov">%s</div>'
                     '<div><span class="rv-v %s">%s</span>'
                     '<p class="rv-why">%s</p>'
                     '<div class="rv-mark">%s</div></div>'
                     '<pre class="rv-text">%s</pre></div>'
                     % (key, prov, _v_class(verdict), verdict or "判不了",
                        html.escape((why or "")[:90]), marks, body))
        accept = json.dumps({("%s|%s" % (r["pid"], p)): (ver.get((r["pid"], p), {}) or {}).get("verdict")
                             for p in provs if (r["pid"], p) in ext}, ensure_ascii=False)
        H.append('<button type="button" class="rv-accept" data-accept=\'%s\'>'
                 '这一页当前判定都对 —— 一键采纳</button>' % html.escape(accept, quote=False))
        H.append("</div></div>")

    total_cells = sum(1 for r in gaps for p in provs if (r["pid"], p) in ext)
    H.append('<div class="rv-bar">'
             '<span class="rv-prog"><span id="rv-n">0</span> / %d 已标</span>'
             '<button type="button" id="rv-export">导出结果</button>'
             '<button type="button" class="rv-ghost" id="rv-copy">复制</button>'
             '<button type="button" class="rv-ghost" id="rv-clear">清空</button>'
             '<span class="rv-meta">标注存在本地浏览器里，刷新不丢。</span>'
             '<textarea class="rv-out" id="rv-out" readonly '
             'aria-label="导出的标注结果"></textarea></div>' % total_cells)
    H.append('<p class="rv-meta">共 %d 页 × %d 家 = %d 格。导出后存成 '
             '<code>data/fetch_gold_gap.jsonl</code>，再跑一次判定即可 —— '
             '人工结论优先级最高，会覆盖面板，并且以后每一轮都复用。</p>'
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
      // 再点一次取消，方便改主意
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
      // 两步确认，不用 confirm() —— artifact 的沙箱 iframe 可能拦掉对话框，
      // 被拦时它返回 undefined，「取消」的分支会让清空永远失效。
      var btn = ev.target;
      if (btn.dataset.armed !== '1') {
        if (!Object.keys(store).length) { return; }
        btn.dataset.armed = '1';
        btn.textContent = '再点一次确认清空';
        setTimeout(function () {
          btn.dataset.armed = ''; btn.textContent = '清空';
        }, 4000);
        return;
      }
      btn.dataset.armed = ''; btn.textContent = '清空';
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
    print("人工核对页 -> %s（%.2f MB）" % (a.out, len(html_text.encode()) / 1e6))


if __name__ == "__main__":
    main()
