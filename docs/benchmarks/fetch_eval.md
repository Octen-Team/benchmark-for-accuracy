# Fetch / extract provider evaluation

**Fetch capability only; parsing quality is not scored. The metric is whether the page
was retrieved.**

One headline number: a weighted **fetch success rate** (pass 1.0 / partial 0.5 / lost 0)
on a single unit shared by every page, so it is comparable across page types and across
providers.

---

## 1. Running a round

The benchmark ships built. `data/datasets/fetch/pageset.gt.jsonl` holds the 100 pages
together with the ground truth every verdict is measured against, so there is nothing to
construct — three commands produce a report. No browser is needed on this path.

```bash
pip install -r requirements-fetch.txt      # optional deps; the core stays requests-only
export RUN=results/fetch_$(date +%Y%m%d)
export SET=data/datasets/fetch/pageset.gt.jsonl

# 1) Fetch. Providers you have no key for are reported as unavailable, never scored zero,
#    so start with whichever subset you have. Before starting, the runner compares the
#    credentials in .env with those in the shell and refuses to run if they disagree.
python -m src.fetch_run --pageset $SET \
    --providers octen exa tavily firecrawl trafilatura readability \
    --out $RUN --concurrency 6 --timeout 60 \
    --pace octen=2.5,firecrawl=6.5

# 2) Judge: the mechanical layer first, then a three-model panel for what it cannot
#    settle. Each provider is judged on its own return; nothing is compared side by side.
#    Needs an LLM key. `--no-panel` runs the mechanical layer alone, which spends nothing
#    and is a good way to check the wiring first.
python -m scripts.fetch_score_run --pageset $SET \
    --extractions $RUN/extractions.jsonl \
    --out $RUN/verdicts.jsonl --concurrency 8

# 3) Report: Markdown, JSON, and a standalone HTML page.
python -m scripts.fetch_report --pageset $SET \
    --verdicts $RUN/verdicts.jsonl \
    --out-md $RUN/report.md --out-html $RUN/report.html --out-json $RUN/agg.json
```

Every step appends as it goes and resumes where it stopped, so an interrupted run picks
up rather than starting over.

**Pacing is required, not optional.** Several providers reject requests above a certain
cadence, and an unpaced round measures their rate limits rather than their fetch
capability. The pacing actually used is recorded in `run_meta.json` and stated in the
report, so the latency caveat is always true for the round it describes.

**One round is what the report is built from.** On defended pages a provider's result
can vary between calls, so a single round carries that variance: two providers a few
points apart are not separated by it, and the report says so rather than letting the
ordering speak for itself. `--repeat N` runs each cell N times and scores the per-cell
median instead, for the rare case where a close ordering has to be settled.

**Ground truth ages.** It was captured on a particular day; pages get redesigned, articles
get taken down, and a defence that was soft can harden. A page whose reference no longer
matches shows up as a provider-wide failure on that one page rather than a difference
between providers, which is the signal to rebuild.

### Building your own page set

Only needed to evaluate a different set of URLs. This is the path that needs a browser.

```bash
python -m playwright install chromium

# A CSV with a `#,category,url` header; the six categories map onto the five page types.
python -m scripts.fetch_pageset_build --csv your_urls.csv --out data/my_pageset.jsonl

# Documents go through the parsers, everything else through a headless browser.
# Roughly 10 minutes for 100 pages.
python -m scripts.fetch_gt_build --pageset data/my_pageset.jsonl \
    --out data/my_pageset.gt.jsonl --channel playwright_headless --concurrency 4

# Recommended. For pages the browser could not fetch either, take the title and snippet
# from a neutral search engine as identity anchors. Without this those cells are judged
# with no reference at all, which is systematically lenient. Needs a SERP key.
python -m scripts.fetch_weak_anchors --pageset data/my_pageset.gt.jsonl
```

---

## 2. What the report contains

| Dimension | Content |
|---|---|
| **Fetch success rate** | the headline table, and the only score that ranks |
| By page type | static docs · render/SPA · document files · anti-bot · robustness |
| Anti-bot pages by wall type | WAF · login wall · paywall |
| Anti-bot pages by protection strength | soft · medium · hard, derived by comparing the two ground-truth channels rather than assigned by hand |
| Document files by format | pdf / docx / xlsx / pptx / csv / json / xml / rss / atom / txt / md |
| Robustness probes | oversize · url_quirk · raw_direct · redirect · empty_thin · encoding · plain_http |

Three supporting sections accompany them: **why it was not retrieved** (the failure
taxonomy crossed with three fault owners), **diagnostic columns** that never enter the
score, and **metric definitions plus methodology notes** written for a non-technical
reader.

Every metric with a direction is ranked within its column, marked `↑` or `↓`. **Columns
with no direction are explicitly marked as not ranked, with the reason** — median length,
panel splits, and latency whenever any provider was paced.

---

## 3. How a verdict is reached

**The mechanical layer** (pure functions, reproducible, traceable to evidence):
`coverage` as the success gate · `render_hit` as the SPA gate · `identity_ok` for whether
this is the page · `encoding_ok` for mojibake · `wall_hit` recorded as evidence only,
never feeding the verdict.

**The panel** handles only the cells that are not a clean pass: three models from three
different families judge blind, the majority wins, and a three-way split keeps the
mechanical verdict. **Conclusions the mechanical layer is certain of are never sent for
review** — wrong page, mojibake, transport failure. The evidence there is hard, and a
transport failure has no text for a panel to look at anyway.

**Each provider is judged on its own return, and only its own.** The panel never sees
what another provider produced for the same page. Showing them side by side would let the
best return set the bar for the rest, which measures relative completeness rather than
whether each one got the page — a different question, and not the one the metric asks.

**Some providers classify the page they fetched**, and the honesty flag consumes that.
A label such as "No Main Content" or "Index Page" is the provider's own statement that
what came back is not the article at this URL; returning body text anyway presents a
non-page as the page. Two rules keep it fair: it is **evidence, never a score** — it can
only add the honesty flag, never set a verdict — and it only ever counts *against* the
provider, so shipping the field can never improve a number. A long body overrides a
"No Main Content" label, which is a measured false negative on substantial articles.

**Priority**: a terminal mechanical verdict > the panel > a mechanical band.

---

## 4. Ground truth is layered

| Tier | Method |
|---|---|
| Strong | document files go through the parsers, where the parse result *is* the ground truth; static docs and SPAs go through a headless render |
| Weak | pages we cannot fetch either — a **neutral SERP** supplies the title and snippet as identity anchors |
| No text ground truth | pages where `expect != content`: 404, 503, redirect chains |

Anti-bot pages run through **both browser channels** (headless and the locally installed
real Chrome); the protection tier is the comparison of the two. The real-Chrome channel
uses a clean profile and is never signed in — ground truth built from a signed-in session
would contain content no provider can reach, and every provider would fail against an
answer none of them can obtain.

**A gap page's vocabulary never feeds a verdict.** When our own browser is blocked, what
it captured is the challenge screen, and using that as the reference marks providers that
genuinely retrieved the content as lost.

---

## 5. Credentials

Read from environment variables only; a missing key fails hard. **The runner refuses to
start when `.env` and the shell environment disagree.** `_load_dotenv` uses `setdefault`
and does not override existing variables, so a stale key left in the shell silently wins
over the new one in `.env` while both look correctly configured — the symptom is a whole
round failing on authorisation against a key that was never used. Pass
`--allow-env-override` to accept the shell value deliberately.

**Every provider's live-fetch (cache-bypass) switch must be set explicitly.** Unset, the
column measures index coverage rather than fetch capability: a provider whose live-crawl
parameter defaults to consulting its own index first returns cached content on a hit.

`cache_pinned` has four states, not two:

| State | Meaning |
|---|---|
| `pinned` | the knob exists and is set to fetch live |
| `no_knob` | verified: the documented API has no such parameter |
| `unknown` | could not determine — say so rather than pretend |
| `unpinned` | the knob exists and is not set (should not occur once wired) |

`unknown` matters because some APIs **do not validate unknown parameters**: a guessed name
is silently ignored while we believe the cache was disabled. **When it cannot be
determined, report that it cannot be determined.**

**When wiring up a new provider, find and set its live-fetch knob at the same time.**

---

## 6. Provider wiring

| Provider | Endpoint | Notes |
|---|---|---|
| octen | `POST api.octen.ai/extract` | |
| firecrawl | `POST api.firecrawl.dev/v1/scrape` | maintains a domain blocklist (social platforms and similar); needs the heaviest pacing |
| exa | `POST api.exa.ai/contents` | `livecrawl=always` is required |
| tavily | `POST api.tavily.com/extract` | |
| parallel | `POST api.parallel.ai/v1beta/extract` | `full_content=True`; **not** the v1 objective mode, which is query-driven |
| zyte | `POST api.zyte.com/v1/extract` | HTTP Basic with the key as username; `browserHtml`, returns HTML |
| you | `POST api.you.com/v1/contents` | the field is `urls` (plural); returns HTML |
| linkup | `POST api.linkup.so/v1/fetch` | returns markdown |
| brightdata | `POST api.brightdata.com/datasets/v3/scrape` | needs `BRIGHTDATA_SCRAPE_DATASET`; returns **JSONL**. This is Datasets v3, not the Web Unlocker `/request` endpoint |
| trafilatura / readability | local libraries | control group |
| context / cloudflare / apify | — | **not wired: no key** |

Providers that return HTML are **normalised to text on our side**. Without that they would
all be judged as having returned a raw payload, which measures output format rather than
fetch capability. The step is declared in the report through `output_form`.

---

## 7. Code map

```
src/fetch_spec.py       page types, anti-bot sub-classes, probes, thresholds;
                        assertions run at import
src/fetch_checks.py     mechanical pure functions (an empty denominator returns None,
                        never 0.0)
src/fetch_backends.py   FetchResponse, the provider adapters, credential-shadowing guard
src/fetch_gt.py         ground truth: parse channel, browser channel, vocabulary and
                        anchor derivation, protection tiers
src/fetch_run.py        the fetch round (row-by-row persistence, resumable, per-provider
                        pacing)
src/fetch_io.py         durable append and progress reporting
src/fetch_score.py      the two hard vetoes, the success gate, the blind panel, the
                        provider self-report check
scripts/fetch_pageset_build.py   CSV -> page set
scripts/fetch_gt_build.py        build ground truth
                                 (--channel playwright_headless | chrome_real)
scripts/fetch_weak_anchors.py    identity anchors for gap pages, from a neutral SERP
scripts/fetch_score_run.py       judging driver
scripts/fetch_report.py          the report (Markdown, JSON and a standalone HTML page)
```

**Always split JSONL on `"\n"`, never with `splitlines()`** — the latter also splits on
U+2028, U+2029 and U+0085, which occur legitimately in page text and which `json.dumps`
does not escape. A single record then gets torn in half and surfaces as a baffling
`Unterminated string`.

---

## 8. Load-bearing rules (read before changing anything)

These are not style preferences. Each one exists because its absence produced a wrong
number.

**Passing means something different for each kind of wall.** WAF = the body was
retrieved; login wall = the pre-wall content was retrieved **and honestly identified as a
wall**; paywall = the free portion was retrieved. **Content from behind the wall earns
nothing** and is flagged `suspicious_bypass` — expecting it would reward circumvention,
which is neither a legitimate measure of fetch capability nor something a buyer should see
scored as a strength. Averaging the three sub-classes together is meaningless.

**Never build ground truth with a product that is under test.** The reference comes either
from our own browser or from a neutral search engine. Using one provider's unblocking
product to build the reference hands the exam to a contestant — and such a provider may
itself be on the roster.

**The per-page `expect` label decides what counts as correct.** For a page that returns
404 or 503, **the correct behaviour is a clean error**, and returning the error page's body
as content is wrong. Without this field those pages would be scored as failures for every
provider, penalising everyone equally and polluting the slice numbers.

**A fault is not a zero.** A failed fetch records its `failure_reason` and `fault`
honestly, and a cell that cannot be judged is left genuinely blank — in neither the
numerator nor the denominator. Converting "we could not judge this" into "the provider
failed" charges the limits of the judging system to the provider.

**Retry the delivery, never the answer.** Our own connection to a provider dropping is a
delivery problem, and it is retried. A provider replying "I tried and could not reach the
target" is its result, and it is not: measured across rounds that answer is stable, so
retrying buys three calls and the same outcome while laundering "could not retrieve" into
"retrieved". The same rule already governs an HTTP 200 with an empty body. That is why
`timeout_upstream` and `target_unreachable` are separate reasons rather than one.

**`harness` must stay a separate fault.** An exhausted account, our own size cap, our own
parser crashing — these are ours. Folded into a provider's failure count, someone else
carries our mistakes.

**A reason stated in the response body outranks the status code.** "I will not fetch this
domain for you" (`blocklisted_domain`) and "the target site blocked me"
(`anti_bot_blocked`) can both arrive as a 403, but one is a policy choice and the other a
capability gap, and they mean opposite things to a buyer. Some providers even wrap "the
site banned us" inside an unrelated status code.

**Three rules for the HTML report**, each of which has failed silently before:
embed data as a **JavaScript literal**, never as `<script type="application/json">`, which
does not survive publishing; prefix every class name with `fx-` so nothing collides with a
host stylesheet, and restate cell attributes explicitly on every `th`/`td`; and print
missing buckets as **unlabelled** rather than letting an empty set masquerade as full
compliance, because a ratio over an empty set displays as "everything passed".

**A metric with no direction is not ranked.** Median length (providers strip boilerplate
differently, so longer is not better), panel splits (a measure of how hard the cells were,
not of provider quality), and latency whenever pacing was applied. Ranking them turns "for
reference only" into "bigger is better".

---

## 9. The page set

Two files ship under `data/datasets/fetch/`:

| File | What it is |
|---|---|
| `pageset.gt.jsonl` | the benchmark: 100 pages with their ground truth. This is what you run against. |
| `eval_urls.csv` | the URL list it was built from, kept so the build is reproducible and so a different set can be built the same way. |

The 100 pages span 8 static docs, 18 render/SPA,
22 document files, 42 anti-bot and 10
robustness probes. The CSV's six categories map onto those five types; the mapping is
transcribed once, in `src/fetch_spec.py`, and asserted at import.

100 pages across **87 hosts** — several hosts contribute more than one page —
so the report also prints a **de-duplicated-by-domain** figure that averages within a
domain first. Without it, being good at one site would count twice.

**20 pages carry no usable reference.** Our own browser is blocked on them too, so
their vocabulary is withheld from judging and the panel rules on the fetched content
alone. They are listed in every report, and verdicts resting on them are counted
separately, because that is the weakest evidence the benchmark produces.

Each page also carries `expect` (`content` / `error` / `redirect_final`) and `probes`
(seven robustness probes), attached by URL substring in the spec's `PAGE_LABELS` and
likewise transcribed once.

`doc_type` **must take the observed content-type into account** and cannot rely on the URL
suffix alone. A URL such as `arxiv.org/pdf/1706.03762` has no suffix, and guessing "pdf"
from the path defeats the page whose whole purpose is to test sniffing.
