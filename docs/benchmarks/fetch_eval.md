# Fetch / Extract Provider 抓取能力评测

**只评抓取能力，不评解析效果。核心指标 = 能否抓取成功。**

100 个网页 × 11 家 provider。主口径是单一的**抓取成功率**（成功 1.0 / 部分 0.5 / 失败 0），
所有页共用一个单位，跨类型跨厂商可比。

---

## 一、跑一轮

```bash
pip install -r requirements-fetch.txt && python -m playwright install chromium

# 1) 抓取（起跑前会自动比对 .env 与 shell 环境的凭据，不一致就硬失败）
python -m src.fetch_run \
    --pageset data/fetch_pageset_20260901.gt.jsonl \
    --providers octen exa tavily linkup parallel zyte you brightdata firecrawl \
                trafilatura readability \
    --out results/fetch_<date> --concurrency 6 --timeout 60 \
    --pace octen=2.5,firecrawl=6.5

# 2) 判定（机械层 + 三模型面板；人工金标优先级最高，自动读 data/fetch_gold_gap.jsonl）
python -m scripts.fetch_score_run \
    --extractions results/fetch_<date>/extractions.jsonl \
    --pageset data/fetch_pageset_20260901.gt.jsonl \
    --out results/fetch_<date>/verdicts.jsonl --concurrency 8

# 3) 报告
python -m scripts.fetch_report \
    --verdicts results/fetch_<date>/verdicts.jsonl \
    --pageset data/fetch_pageset_20260901.gt.jsonl \
    --out-md results/fetch_<date>/report.md \
    --out-html results/fetch_<date>/report.html \
    --out-json results/fetch_<date>/agg.json
```

**节流是必需的**，不是可选：octen 在更快的节奏下会拒绝请求；firecrawl 不加节流时
实测 100 页里有 20 格输在限速上（加 6.5s/请求后限速失败归零）。

---

## 二、报告出这六个维度

| 维度 | 内容 |
|---|---|
| **抓取成功率** | 主表，唯一进排行榜的分数 |
| 按页面类型 | 静态文档 8 · 渲染/SPA 18 · 文档文件 22 · 反爬 42 · 健壮性 10 |
| 反爬页按墙的类型 | WAF 26 · 登录墙 10 · 付费墙 6 |
| 反爬页按防护强度 | 软 26 · 中 3 · 硬 13（两条 GT 通道比对得出，不是主观定的） |
| 文档文件按格式 | pdf / docx / xlsx / pptx / csv / json / xml / rss / atom / txt / md |
| 健壮性探针 | oversize · url_quirk · raw_direct · redirect · empty_thin · encoding · plain_http |

外加三块支撑：**为什么没抓到**（9 类失败 × 责任方三分）、**诊断列**（不进分数）、
**口径速查 + 方法学声明**（给产品读的）。

每个有方向的指标都逐列排名，`↑`/`↓` 标方向；**没有方向的列显式标「不排名」并说明原因**
（长度中位、三方分歧、以及本轮因为有节流而不可横向比的延迟）。

---

## 三、判定怎么做出来的

**机械层**（纯函数，可复现、可核到证据）：
`coverage` 成功闸门 0.3 · `render_hit` SPA 闸门 0.4 · `identity_ok` 是不是这一页 ·
`encoding_ok` 乱码 · `wall_hit` 只作证据不参与判定。

**面板层**只处理非 clean-pass 的格：三 family 三模型盲判、多数决、三方分歧保留机械判定。
**机械层确定的结论不送复议**（抓错页 / 乱码 / 传输失败 —— 证据是硬的，且传输失败根本
没有文本可看）。

**没有参考答案的页整页一起判**：五家以上的返回并排给面板、厂商名隐掉标 A/B/C、
提问改成对抗式（"没有正面证据就判失败"）。既比单家裸判准，又把 N 次调用降成 1 次。

**优先级**：人工金标 > 机械层终局 > 面板 > 机械层档位。

---

## 四、GT 分层

| GT 档 | 页 | 做法 |
|---|--:|---|
| 强 | 48 | 文档文件走解析器（解析结果即 GT）；静态文档与 SPA 走 headless 渲染 |
| 弱 | 18 | 我们自己也抓不到的页 —— 从**中立 SERP** 取标题/摘要当身份锚点（13/18 拿到） |
| 无文本 GT | 3 | `expect != content` 的 404 / 503 / redirect |

反爬 42 页跑**两条浏览器通道**（headless + 本机真实 Chrome），防护强度档就是两者的比对结果。
`chrome_real` 走 Playwright 驱动本机 Chrome，干净 profile、不登录。

**GT 缺口页的词表不参与判定** —— 被拦时抓到的是验证页文案，拿它当参考会把真的抓到了
内容的家判成 lost（w3.org 实测）。

---

## 五、人工核对闭环

18 页无参考的格是全场证据最弱的一批。核对页把各家返回并排列出、可逐格打标：

```bash
python -m scripts.fetch_review_page --pageset ... --extractions ... --verdicts ... --out review.html
# 页面上标完点「导出」，存成 marks.jsonl，然后：
python -m scripts.fetch_gold_ingest --marks marks.jsonl --extractions ... --out data/fetch_gold_gap.jsonl
```

金标带**内容指纹守卫**：换一轮实跑、那家返回的东西变了，这条金标自动失效。
`拿不准` 不写进金标。核过的格不再计入「低置信」占比 —— 否则标了 ⚠ 也不会消失。

实测：面板对人工的准确率 **90%**（81/90），错误不对称 —— 比人工松 7 格、严 2 格。

---

## 六、凭据

只从环境变量读，缺失即硬失败。**`.env` 与 shell 环境不一致时 runner 会拒绝起跑** ——
`_load_dotenv` 用 `setdefault` 不覆盖已有变量，shell 里残留的旧 key 会静默压过 `.env` 里的新
key，实测因此白跑过一整轮并写出过错误结论。要接受 shell 的值得显式加 `--allow-env-override`。

各家的**「实时抓、不走缓存」开关必须显式设上** —— 不设的话那一列量的是索引覆盖率而不是
抓取能力（exa 的 `livecrawl` 不传时默认 `fallback` 先查自家索引，强制实时后它掉了 8 个百分点）。

| 家 | 开关 | 状态 |
|---|---|---|
| octen | `max_age_seconds=0` | pinned |
| exa | `livecrawl="always"` | pinned |
| firecrawl | `maxAge=0` | pinned |
| trafilatura / readability | HTTP `Cache-Control: no-cache` | pinned |
| tavily / zyte | 官方 API 无此参数 | no_knob |
| you / linkup / parallel / brightdata | 查不到 | **unknown** |

`unknown` 是第四态，不是 `unpinned`：linkup 和 tavily 一样**不校验未知参数**，猜一个名字会被
静默忽略而我们以为钉住了。**查不到就标查不到。**

**接新家时顺手把它的实时抓开关一并查清设上。**

---

## 七、13 家的接线状态

| 家 | endpoint | 备注 |
|---|---|---|
| octen | `POST api.octen.ai/extract` | |
| firecrawl | `POST api.firecrawl.dev/v1/scrape` | 有域名黑名单（社交平台等）；需 6.5s 节流 |
| exa | `POST api.exa.ai/contents` | 必须 `livecrawl=always` |
| tavily | `POST api.tavily.com/extract` | |
| parallel | `POST api.parallel.ai/v1beta/extract` | `full_content=True`；**不用 v1 的 objective 模式**（那是查询驱动） |
| zyte | `POST api.zyte.com/v1/extract` | HTTP Basic（key 作用户名）；`browserHtml`，返回 HTML |
| you | `POST api.you.com/v1/contents` | 字段是 `urls`（复数）；返回 HTML |
| linkup | `POST api.linkup.so/v1/fetch` | 返回 markdown |
| brightdata | `POST api.brightdata.com/datasets/v3/scrape` | 需 `BRIGHTDATA_SCRAPE_DATASET`；返回 **JSONL**。不是 Web Unlocker 的 `/request` |
| trafilatura / readability | 本地库 | 对照组 |
| **context / cloudflare / apify** | — | **无 key，未接线** |

返回 HTML 的家（zyte / you）**在我们这边归一化成文本** —— 不做的话它们会被判成"返回了原始
载荷"全判 lost，那量到的是输出格式不是抓取能力。报告里以 `output_form` 声明。

---

## 八、代码

```
src/fetch_spec.py       5 type / 反爬三小类 / probes / 阈值，import 期断言
src/fetch_checks.py     机械层纯函数（分母为空返回 None，绝不 0.0）
src/fetch_backends.py   FetchResponse + 13 家 adapter + 凭据遮蔽检查
src/fetch_gt.py         GT：解析通道 + 浏览器通道 + 词表/锚点导出 + 强度档
src/fetch_run.py        实跑 runner（逐条落盘、可恢复、按家节流）
src/fetch_score.py      两条硬否决 + 成功闸门 + 跨家交叉判 + 金标
scripts/fetch_pageset_build.py   CSV -> 页面集
scripts/fetch_gt_build.py        GT 建库（--channel playwright_headless | chrome_real）
scripts/fetch_weak_anchors.py    缺口页从中立 SERP 补身份锚点
scripts/fetch_score_run.py       判定驱动
scripts/fetch_reclassify.py      失败归因离线重分类（错误原文都存着，不用重抓）
scripts/fetch_gold_ingest.py     人工标注 -> 金标
scripts/fetch_review_page.py     人工核对页
scripts/fetch_report.py          报告（Markdown + artifact 页）
```

**读 JSONL 一律按 `"\n"` 切，不能用 `splitlines()`** —— 后者还会在 U+2028 / U+2029 / U+0085
上切，而那些字符在网页正文里合法出现、`json.dumps` 也不转义。500 条抓取里实测就有一条。

---

## 九、几条承重的规矩（改动前先读）

这些不是风格偏好，每一条背后都有踩过的坑。

**反爬页的「过」按墙的类型定义，三小类各不相同。**
WAF = 拿到正文；登录墙 = 拿到墙前可见内容**且诚实标明这是墙**；付费墙 = 拿到免费可见部分。
**拿到墙后内容不加分**，另标 `suspicious_bypass` —— 期待墙后内容等于奖励绕墙，而那不是抓取
能力的正当度量，也是采购时要单独评估的合规问题。三小类混在一起平均没有意义。

**绝不用被测厂商的产品来建 GT。** 参考答案要么来自我们自己的浏览器，要么来自中立搜索引擎
（google/bing 不在被测名单上）。拿某家的 unlocker 去给全场建参考，等于让参赛选手出考题 ——
而 brightdata 本身就是候选厂商之一。

**逐页的 `expect` 标签决定什么才算对。** `httpbingo.org/status/404` 和 `/status/503` 这两条的
**正确行为是干净报错**，把错误页的页面体当正文返回是错的。没有这个字段，这两条会对所有家
判成失败，等于给全场同等扣分还污染切片数字。

**故障不是 0 分。** 抓取失败如实记 `failure_reason` + `fault`，判不了的如实留空 ——
既不进分子也不进分母。把「我们判不了」折算成「厂商失败」，是把判定系统的局限记到厂商头上。

**`fault` 三分里 `harness` 必须单列。** 账户欠费、我们的长度上限、我们的解析器崩了 ——
这些是我们自己的锅，混进厂商的失败数里会让别人替我们背。

**响应体里明说的原因优先于状态码。** 「我不给你抓这个域」（`blocklisted_domain`）和
「目标站把我拦了」（`anti_bot_blocked`）都可能是 403，但一个是政策选择、一个是能力差距，
采购时含义完全不同。Zyte 甚至把「被站点封了」包在 HTTP 520 里。

**报告页三条硬纪律**（都栽过）：
数据作 **JS 字面量**嵌入，不用 `<script type="application/json">`（发布成 artifact 后不保留）；
类名一律 `fx-` 前缀，不撞共享样式，且基础样式在 `th,td` 上设的属性要逐个显式重申；
**缺档写「未标注」，空集合不伪装成达标** —— 空集合上的比值会显示成「全部达标」。

**没有方向的指标不排名。** 长度中位（各家正文剥离口径不同，长不等于好）、三方分歧
（量的是格子有多难判，不是厂商好坏）、以及本轮因为有节流而不可横向比的延迟 ——
硬给它们排名会把「仅供参考」读成「越大越好」。

---

## 十、页面集

100 条 URL 来自 `data/eval_urls_20260901.csv`（输入的版本化拷贝），CSV 自带的 6 个类别
映射到 5 个 type，映射表只在 `src/fetch_spec.py` 转写一次并在 import 期断言。

**87 个 host / 100 页** —— 13 行同域，报告出一个「按域去重」的副口径（同域先取域内均值），
避免「某个域处理得好」被计两次。

逐页还带 `expect`（`content` / `error` / `redirect_final`）与 `probes`（7 种健壮性探针），
都在 spec 的 `PAGE_LABELS` 里按 URL 子串挂载，只转写一次。

`doc_type` **必须由实测 content-type 参与判定**，不能只看 URL 后缀 ——
`arxiv.org/pdf/1706.03762` 没有后缀，猜成 pdf 就把「考嗅探」那道题做废了。
