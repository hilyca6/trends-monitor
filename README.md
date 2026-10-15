# TrendRadar

全球热词 / 新游戏 / 抢先建站选词雷达。每小时自动采集 **Google Trends**、**Bing SERP**、**谷歌自动补全**、**Steam/App Store**、**竞争站 Sitemap** 等免费公开数据，做去噪、判真、判空、分级，输出一个纯静态站点（`web/`）。

目标是回答四个问题：**这个词是不是真在涨？是不是新词？有没有人已经做了？现在该不该动手？**

---

## 快速开始

```powershell
# 1) 安装依赖
pip install -r requirements.txt

# 2) 复制并填写本地配置（代理、LLM key）
copy config.local.example.json config.local.json

# 3) 手动跑一次
python scripts\run_all.py

# 只看前端（离线 demo 数据）
python -m collector.collect --sample
python -m http.server 8000 --directory web   # 打开 http://localhost:8000
```

`config.local.json` 是唯一需要填的文件（**不要提交**，已在 `.gitignore`）。所有项都有默认值，缺省即可运行；最常改的是 `proxy` 和 `llm.api_key`。

---

## 目录结构

```
collector/           采集与分析核心（Python）
  config.py          静态配置：国家列表、分类映射、噪音规则、路径
  settings.py        读 config.local.json → 合并默认值 → 导出环境变量 / 代理池
  http.py            共享 Session（UA、Consent cookie、重试、代理）
  proxy.py           代理池：多出口轮换 + 被限流时切换
  quota.py           付费 API 配额守卫（模板，默认未接入）
  parser.py          解析 Google Trends "Trending Now" SSR payload
  providers/         数据源抽象：GoogleTrendsSSR（主）/ GoogleTrendsRSS（备）
  store.py           状态 + trends.json / history.json 落地
  enrich.py          规则去噪 + 可选 LLM（翻译 / 分类 / 噪音）
  collect.py         主编排：抓取 → enrich → state → 导出
  suggest.py         Google 自动补全长尾扩展
  discover.py        榜外新词（对种子词做 a-z 枚举；默认关闭，`is_due` 等工具仍复用）
  related.py         Google Trends related queries（rising 词 + 清洗；默认关闭）
  seocheck.py        Bing SERP：竞争度打分 + 市场空白判定 + 命名风险
  gtrends_api.py     Google Trends explore/widget 客户端（多关键词、限流轮换）
  signal.py          基准归一信号（GPTs 倍数 / 曲棍球杆 / 实体冲突 / score）
  fastheat.py        快速热度层（补全/SERP 基线快照 → 新增量）
  advice.py          ★推荐指数 + 推荐分级 + 内容机会 + LLM 建议文案
  sitemaps.py        竞争站 Sitemap 监控（域名收录先行指标）
  games/             新游戏雷达（Steam / AppStore / Trending + 兴趣曲线）
  gameradar/         游戏名雷达（itch/CrazyGames/Poki/Y8/GamePix/Lagged/Newgrounds）
  capcut.py          CapCut 最常用模板增量监控（video 热榜 → capcut.json）
  sample_data.py     离线 demo 数据
scripts/
  run_all.py         定时任务入口（串联所有 postpass）
  install_task.ps1   注册 Windows 计划任务
  uninstall_task.ps1
web/                 纯静态前端（index.html / app.js / style.css + data/*.json）
state/               运行状态与缓存（不进版本库）
logs/run.log         运行日志（滚动）
tests/               pytest（160+ 用例）
pyproject.toml       ruff + pytest 配置
```

---

## 数据流水线（`scripts/run_all.py`）

每次运行按顺序执行；每个阶段都有 `--no-xxx` 关闭开关，且异常隔离（一个阶段失败不影响其他阶段）。也可用 `--group {hourly,games,sitemap,all}` 只跑一组（用于错峰定时任务，见「定时任务」）。

| 顺序 | 阶段 | 频率 | 作用 |
|---|---|---|---|
| 1 | `collect` | 每小时 | 抓各国 Trends 热榜 → 规则/LLM 去噪翻译 |
| 2 | `seo_postpass` | 每小时 | 对热词（new 或近 `refresh_hours` 内出现）跑 Bing 竞争度 + 补全长尾 |
| 3 | `games_task` | 每 3h | 新游戏雷达（Steam/AppStore/Trending + 兴趣曲线） |
| 4 | `gameradar_task` | 每 3h | 游戏名雷达（itch/CrazyGames/Poki/Y8/GamePix/Lagged/Newgrounds → `gameradar.json`） |
| 5 | `sitemap_task` | 每天 | 抓竞争站 Sitemap，统计词被多少独立域名收录 |
| 6 | `capcut_task` | 每 4h | CapCut "最常用模板"榜增量监控（video；精确使用量/排名/新进榜 → `capcut.json`） |
| 7 | `discover_task` | 默认关闭 | 种子词自动补全枚举，挖榜外新词（效果不佳，默认关闭；开启见 `discover` 配置） |
| 8 | `related_postpass` | 默认关闭 | 从榜外词挖 Trends rising 相关词（依赖 `discover`，故默认关闭） |
| 9 | `signal_postpass` | 每小时 | 基准归一：GPTs 倍数 / 曲棍球杆 / 实体冲突 / score（含游戏雷达 online 频道） |
| 10 | `advice` | 每小时 | ★星级 + 推荐分级 + 内容机会（含游戏雷达：Bing竞争度/市场/命名风险 + LLM 文案） |
| 11 | `fast_postpass` | 每小时 | 对比补全/SERP 基线快照，算"新增量"（含游戏雷达） |
| 12 | `social_postpass` | 每小时 | 游戏雷达社媒外溢（YouTube/Reddit/X/TikTok；多平台多独立创作者） |
| 13 | `opportunity_postpass` | 每小时 | 游戏雷达：机会分 + 硬门槛（`test-now` / `independent`） |
| 14 | `push` | 按 `daily_at_hour` | 汇总摘要推送到企业微信/钉钉/Telegram |

输出统一写进 `web/data/*.json`，前端直接读取。

---

## 输出数据与字段参考

### `web/data/trends.json` —— 各国热榜（主）
```
{ updated, geos[], cats{id:name}, items{ geo: [ {…词条…} ] } }
```

### 词条公共字段

| 字段 | 说明 |
|---|---|
| `q` | 关键词 |
| `zh` | 中文翻译/注释（LLM） |
| `vol` / `growth` | 近似搜索量桶 / 涨幅%（Google Trends） |
| `cats` | Google 分类 id 列表 |
| `noise` | 噪音标签（比赛/天气/人物/品牌/彩票/事故/导航/大词），空=干净 |
| `new` | 本轮首次出现 |

### 各分析字段（可能缺省，取决于是否跑到）

**`seo`**（Bing SERP）
| 字段 | 说明 |
|---|---|
| `score` / `label` | 竞争度 0–100（高=好做）/ 高·中·低 |
| `ugc` / `strong` / `n` | 是否含 UGC、强站数、结果数 |
| `market` | `{status, confidence, reason, dedicatedDomains[], contentDomains[], checked}`，`status ∈ greenfield(空白)/contested(早期竞争)/occupied(已被占位)/unknown` |
| `nameRisk` | 命名风险 0–30（通用词/高危词/单词） |
| `gameIntent` | `{game, news, nonGame, other, total, gameRatio, newsRatio, nonGameRatio}`：结果按游戏/新闻/歌曲影视等实体分类 |
| `entityConflict` | 同名非线性实体占比过高（`nonGameRatio≥0.4`），做站需谨慎 |
| `serpUrls` | 前 5 条结果 URL（去 query） |
| `checked` | 检查时间 |

**`longtails`**：Google 自动补全长尾列表。

**`related`**：`{rising:[...], top:[...]}`（该词的相关上升/热门查询）。

**`signal`**（Google Trends 基准归一，同一请求内比较）
| 字段 | 说明 |
|---|---|
| `ratio` | 主词热度 ÷ 基准词（近期均值）＝"GPTs 倍数" |
| `qualifiedRatio` | 限定词（`{q} guide/wiki/online`）热度 ÷ 基准 |
| `classification` | `breakout/rising/strong/moderate/weak/none`（爆发/明显上涨/需求较强/需求中等/需求较弱/无需求） |
| `freshness` | `new/existing/unknown`（90 天前段是否接近无量） |
| `entityConflict` | 老词但限定词热度远低于主词 → 主词热度可能来自其他实体 |
| `score` | 0–100 需求分 |
| `channel` / `anchor` | 频道（generic/wiki/online）/ 基准词（gpts/steam/crazy games） |
| `windows` | 7d/30d/90d 各自的 `{ratio,qualifiedRatio,classification,spike,coverage,momentum,slope,...}` |

**`fast`**（快速热度层，比 Trends 更早）
| 字段 | 说明 |
|---|---|
| `newSuggestionCount` / `newSerpPageCount` | 相比基线新增的补全长尾 / SERP 页面 |
| `classification` | `pass/watch/weak` |
| `score` | 0–100 |

**`cat` / `subcat` / `entity` / `trigger`**（LLM 分类）
- `cat`：领域（gaming/ai/tools/finance/health/…），`subcat`：子类，`entity`：实体类型，`trigger`：上涨成因（新发布/病毒传播/季节/…）。

**`advice`**（推荐）
| 字段 | 说明 |
|---|---|
| `stars` | ★1–5（需求×竞争×窗口，代码计算） |
| `level` | `值得做 / 测试 / 观察 / 淘汰` |
| `opportunities` | 可做内页 `[{query,intent,priority}]` |
| `market` / `nameRisk` | 同 `seo`（复制过来便于前端展示） |
| `audience/directions/main_keyword/monetize/risk/evidence/window` | LLM 建议文案（无 key 时为空） |

### 其它输出
- `history.json`：近 30 天留档（含峰值/首末次/新字段）。
- `games.json`：`items[{name, series[], chart_at, score, rise, growth, rank, size, rankChange, breakout, web_game, sources, source, url, zh, new, group, heat, history[], growth7d, advice?}]`。`group` = `trending`（有搜索热度：Google 热榜/网页游戏）或 `listed`（仅 Steam/App Store 榜单，`rank`/`size` 为榜内名次/榜单深度）；`history` 为近 `games.history_days`（默认 7 天）的 `[[iso, heat], ...]` 自建采样点，`heat` 0–100（已采曲线用 `score`，否则用榜单位置），前端分两段展示并画自建走势线。
- `gameradar.json`：`items[{name, q(=name), first, last, sightings, sources[], url, web_game, new, seo?, advice?, fast?}]`。来自 itch/CrazyGames/Poki/Y8/GamePix/Lagged/Newgrounds 的网页新游戏名；竞争度/市场/命名风险与 ★ 由 advice 层补齐。
- `sitemap.json`：`items[{q, domains[], domain_count, domain_count_7d/15d/30d, first, baseline, new, zh, noise, cat?, advice?, signal?}]`。`first` 为 `null` / `baseline=true` 表示该词在监控开始前就已被收录（无真实首次时间），前端显示"基线"。
- `capcut.json`：`items[{id, title, desc, url, type, uses, usesDelta, deltaPerHour, growth, usesGain3d, growth3d, likes, likesDelta, comments, rank, rankDelta, first, baseline, new, level, history}]`。CapCut "最常用模板"榜增量（默认 video）：`uses` 为精确使用量（取自页面内嵌 `__MODERN_ROUTER_DATA__`），`rank` 按使用量降序，`desc` 为模板描述/hashtag；`growth3d` 为窗口内(≤3天)增值率，`history` 为 `[[iso, uses], ...]` 采样点供前端画走势线。

---

## 算法速览

- **signal 分类**：主词÷基准 ≥ `breakout_ratio`(5) 且动量≥1 → `breakout`；≥ `rising_ratio`(1) 且动量≥1.15 → `rising`；≥0.5 → `strong`；≥0.2 → `moderate`；否则 `weak`；单日孤立尖峰降级 `weak`。
- **freshness**：90 天前段接近无量、近期才有量 → `new`；前段就有量 → `existing`。
- **entityConflict**：`existing` 且 `qualifiedRatio < max(0.01, ratio×0.25)`。
- **market**：SERP 出现同名专站/百科路径 → `occupied`；≥2 个攻略/Wiki 站 → `occupied`；恰好 1 个 → `contested`；无专站无内容竞争站 → `greenfield`（平台/聚合站被忽略）。
- **fast**：新增补全 ×8 + 新增 SERP 页面 ×4，≥40 `pass`、≥12 `watch`，否则 `weak`；有基线后 0 新增不计。
- **level**：★≥4 且市场非 `occupied` → 值得做；★≥3 或（爆发/上涨且新词）→ 测试；★2 → 观察；否则淘汰。

---

## 配置参考（`config.local.json`，缺省走 DEFAULTS）

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy` | `""` | 代理，**字符串或数组**（数组=多出口，被限流时自动轮换） |
| `proxy_switch_url` | `""` | 可选：切换上游节点的 GET 地址（如 Clash 控制 API） |
| `proxy_rotate_on_block` | `true` | 被 Google 限流时是否自动轮换 |
| `geos` | `"default"` | `default`(10 国) / `all`(38 国) / 逗号列表 `US,GB,JP` |
| `retention_days` | `90` | 各缓存/留档保留天数 |
| `llm.enabled` / `api_key` / `base_url` / `model` | 智谱 glm-4-flash | 不填 key 则跳过所有 LLM 步骤 |
| `seo.enabled/serp_budget/suggest_budget/serp_throttle/geo` | 见文件 | Bing + 补全预算 |
| `seo.refresh_hours` | `24` | 热词滚动重采窗口（0=只查新词） |
| `seo.cache_days` | `7` | SERP 缓存 TTL（过期重查，让 fast 的新增 SERP 可变化） |
| `fast.enabled` | `true` | 快速热度层开关 |
| `related.enabled/budget/min_stars/window/throttle` | `enabled=false` | rising 相关词；依赖 `discover`，默认关闭 |
| `signal.enabled/baseline/anchors/windows/budget/gameradar_budget/throttle/breakout_ratio/rising_ratio/recent_hours` | | 基准归一信号；`recent_hours`>0 时把近 N 小时出现的词也纳入；`gameradar_budget` 限制单轮游戏名数量 |
| `sitemap.enabled/interval_hours/watch_urls/throttle/budget/max_sitemaps/min_words/max_llm` | | 竞争站 Sitemap；`watch_urls` 填你要监控的站点图 |
| `advice.enabled/budget/seo_budget/ttl_days/batch/workers/weights` | | 建议与星级 |
| `discover.enabled/interval_hours/throttle/budget/max_llm/seeds` | `enabled=false` | 榜外新词枚举；默认关闭，开启需填游戏/目标向 `seeds` |
| `games.enabled/sources/interval_hours/max_chart/max_refresh/refresh_hours/throttle/history_days` | | 新游戏雷达；`history_days` 为自建走势保留天数（默认 7） |
| `gameradar.enabled/sources/interval_hours/throttle` | | 游戏名雷达；`sources` 见下 |
| `capcut.enabled/interval_hours/template_types/max_pages/throttle` | 见文件 | CapCut 最常用模板增量监控；`template_types` 默认 `["video"]`（图片无使用量），独立于 Google/Bing 预算 |
| `youtube.enabled/api_key/budget/throttle/max_age_hours` | `api_key=""` | 游戏雷达的 YouTube 热度；填 key 才生效 |
| `opportunity.enabled` | `true` | 游戏雷达机会分 + 硬门槛开关 |
| `social.enabled/budget/max_age_hours/min_platforms/throttle/timeout_ms/reddit_*/x_bearer_token/tiktok_research_token` | 见文件 | 游戏雷达社媒外溢；`min_platforms` 默认 2，未填凭证的 Provider 自动跳过 |
| `push.enabled/daily_at_hour/quiet_hours/min_stars/top_gameradar/top_sitemap/urgent.*/wecom_webhook/dingtalk_webhook/dingtalk_secret/telegram_bot_token/telegram_chat_id` | 默认关 | 推送渠道；内容见下 |

“预算”都是**每轮最多消耗多少次该数据源请求**（防限流/控量），不是钱；只有 LLM 是付费（可关）。

---

## 游戏雷达（网页游戏平台）

`gameradar_task` 从浏览器游戏平台抓取**新游戏名**（抢先建站候选），写入 `web/data/gameradar.json`，再复用既有验证层：

- 竞争度 / 市场空白 / 命名风险：由 `advice` 层调用 `seocheck`（Bing SERP）补齐
- 游戏意图 / 同名实体：`seocheck` 把结果分为 游戏 / 新闻 / 非游戏（歌曲·影视等）/ 其他，算 `gameIntent` 占比并标记 `entityConflict`
- 在线平台扩散 / 快速热度：`gameradar/signals.py` 统计跨平台数、24h 新增平台、出现轮次，输出 `fast`（多个 feed 归并为一个平台）
- Newgrounds 动画过滤：Newgrounds 的 RSS 游戏/动画混排且无类型字段，按标题关键词（animation/cartoon/meme/`Ep N` 等，见 `sources.is_probably_movie`）剔除动画条目
- YouTube 视频侧热度（可选）：`gameradar/youtube.py` 用 YouTube Data API v3 查 `{游戏名} gameplay` 近 7 天的视频数/频道数/播放量，并入 `fast` 与 ★（未配置 key 时自动跳过）
- ★ 推荐指数 / 推荐分级 / 内容机会：由 `advice` 层计算，页面点 ★ 可看建议
- Google Trends 需求信号：由 `signal_postpass` 用 **online 频道**（基准词 `crazy games`，辅助词 `{q} online` / `play {q}`）验证；**游戏名优先**挑选，单轮最多 `signal.gameradar_budget`（默认 8）个，避免抢占其它列表的 Trends 预算
- SERP 新增量：由 `fast_postpass` 复用既有 SERP 快照对比
- 机会分 + 硬门槛：`gameradar/opportunity.py`（移植自 game-name-radar 的 `opportunity-finalizer`）输出可解释分值 `opportunity`：
  - 权重：社媒 30% · 增长速度 25% · 搜索形成 15% · 内容可扩展 15% · SERP空缺 10% · 名称安全 5%
  - **测试局 `test-now`**：增长/搜索/内容/竞争/名称达到门槛即可（**不依赖社媒**）
  - **正式独立站 `independent`**：还需 `externalSpillover`（社媒）等全部硬门槛 + 总分≥70
  - 决策显示为徽章：`机会 测试局 63` / `机会 观察 31` / `机会 淘汰 10`
- 社媒外溢：`gameradar/social.py`（移植自 `social-buzz`）检查 YouTube/Reddit/X/TikTok 的**跨平台、多独立创作者**传播；≥2 平台 + ≥4 独立创作者 + 24h 仍有新增才算 `externalSpillover`（单平台只算 `singlePlatformMomentum`）。**YouTube 复用游戏雷达已抓的数据**，不重复消耗配额。

`gameradar.sources` 可选（逗号分隔）：

```
itch               itch.io 最新网页游戏
itch-popular       itch.io New & Popular
itch-new-feed      itch.io New Feed
itch-featured-feed itch.io Featured Feed
crazygames         CrazyGames 新游戏
poki               Poki 新游戏
y8                 Y8 新游戏
gamepix            GamePix 新游戏
lagged             Lagged 新游戏
newgrounds         Newgrounds Daily Top Five
```

单独跑一次：`python -m collector.gameradar.radar --sources itch,crazygames,newgrounds`

### YouTube 热度（可选）

在 `config.local.json` 填 `youtube.api_key`（或设环境变量 `YOUTUBE_API_KEY`）后，游戏雷达每轮会挑最多 `youtube.budget` 个新/热门游戏名查 YouTube：

```json
"youtube": { "enabled": true, "api_key": "AIza...", "budget": 3, "throttle": 1.0, "max_age_hours": 72 }
```

获取 key：Google Cloud Console → 新建项目 → 「API 和服务 → 库」启用 **YouTube Data API v3** → 「凭据 → 创建 API 密钥」。免费额度 10,000 单位/天（一次 search 约 100 单位）。留空则整步跳过，不影响其他功能。

### 社媒外溢（可选）

`social_postpass` 检查「跨平台、多独立创作者」的真实传播。四个 Provider 全部可选，**没填凭证就跳过**：

```json
"social": {
  "enabled": true, "budget": 5, "max_age_hours": 6, "min_platforms": 2,
  "reddit_client_id": "", "reddit_client_secret": "", "reddit_user_agent": "trendradar/1.0",
  "x_bearer_token": "", "tiktok_research_token": ""
}
```

- **YouTube**：复用 `youtube.api_key`，不额外消耗配额
- **Reddit**：Reddit app（script 类型）的 client id/secret + 自定义 `user_agent`
- **X**：API v2 的 `Bearer Token`
- **TikTok**：Research API token

> 要放开 `independent`，默认需要**至少两个平台**有凭证（≥2 平台 + ≥4 独立创作者 + 24h 有新增）。只配 YouTube 时，可把 `social.min_platforms` 设为 **1**，让「单个平台的强信号」也能解锁 `independent`——代价是误报变多。改回 2 即恢复严格模式。

---

## 定时任务（错峰多任务）

为降低 Google Trends 的集中请求（`collect` / `games` / `signal` 共用同一出口 IP），把阶段**按频率拆成三个错峰任务**，各在不同分钟触发：

| 任务 | `--group` | 频率 | 默认分钟 | 内容 |
|---|---|---|---|---|
| `TrendRadar-Hourly` | `hourly` | 每 60 min | `:00` | collect / seo / signal / advice / fast / social / opportunity / persist / push |
| `TrendRadar-Games` | `games` | 每 180 min | `:20` | games + gameradar |
| `TrendRadar-Sitemap` | `sitemap` | 每 1440 min | `:40` | sitemap |

```powershell
# 注册（可自定义错峰分钟）
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -HourlyMinute 15 -GamesMinute 0 -SitemapMinute 40
Start-ScheduledTask -TaskName TrendRadar-Hourly        # 立即跑一次
Get-ScheduledTask -TaskName TrendRadar-* | Get-ScheduledTaskInfo
powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1   # 卸载全部
```

手动跑（默认 `--group all`，一次性跑所有阶段，和以前一致）：
```powershell
python scripts\run_all.py
python scripts\run_all.py --group games          # 只跑游戏雷达
python scripts\run_all.py --group hourly --no-push
```

日志：`logs\run.log`。

### GitHub Actions（可选）

`.github/workflows/collect.yml` 用 GHA 定时采集（三条 cron 对应 `hourly`/`games`/`sitemap` 组，UTC）：

- **Secrets**：加一个 `CONFIG_LOCAL_JSON`，内容就是你的 `config.local.json`（至少填 `proxy`，否则 GHA 的数据中心出口会被 Google Trends 限流；建议再填 `llm.api_key`）。
- **状态回写**：跑完自动 `git commit state web/data` 回仓库（runner 是临时的，不回写就没有增量）。
- **部署**：可选 job 把 `web/` 发到 GitHub Pages（Settings → Pages → Source 选 “GitHub Actions”；不用就删掉 `deploy` job）。
- 公开仓 GHA 分钟免费；`state/` 已在 `.gitignore`，流程里用 `git add -f` 强制加入。
- 注意 GHA 的 `TZ` 是 UTC，`push.daily_at_hour` / `quiet_hours` 按 UTC 理解。

---

## 测试与代码风格

```powershell
python -m pytest -q          # 全部测试
python -m ruff check collector scripts tests
node --check web/app.js      # 前端语法
```

---

## 限流与代理轮换

Google Trends 无官方 API，靠抓取，**按出口 IP 限流**（返回 429 或 `/sorry/` 拦截页）。

- 触发限流时：`gtrends_api` 会尝试**切换代理并重试**（最多 `TRENDS_MAX_ROTATIONS`，默认 3）；轮换用尽则抛 `BlockedError`，对应阶段本轮跳过（非故障）。
- 启用轮换（任选其一）：
  - 多代理：`"proxy": ["http://127.0.0.1:7897", "http://127.0.0.1:7898"]`
  - 切换端点：`"proxy_switch_url": "http://127.0.0.1:9090/proxies/节点名"`（发 GET；如需鉴权/PUT 改 `collector/proxy.py:_hit_switch`）
- 单代理时行为与以前一致，不会乱试。
- 限流当日会推送一次告警（含本次轮换次数）。

> 付费 API（SerpAPI/Apify/Brave 等）未接入；若将来接入，用 `collector/quota.py` 包一层即可，避免超支。

---

## 前端

`web/index.html` + `app.js`（无框架，纯读 `data/*.json`）。四个 Tab：

- 🌍 全球热点（按搜索量/★/信号排序；可按国家、分类、量级、涨幅筛选；"🔥 只看爆发/新词"开关）
- 🚀 站点信号（Sitemap 收录先行指标）
- 🎮 新游戏（4 个子 Tab：热度/榜单 × 原始曲线/自建7天；自建7天按近 1 天增值率排序）
- 🎯 游戏雷达（网页游戏平台新游戏名；竞争度/市场/命名风险/★，点 ★ 看建议）
- ✂️ CapCut（CapCut 最常用视频模板榜；精确使用量/排名变化/新进榜）

词条徽章：`🚀爆发 / 📈上涨 / 🆕新词 / ⚠️实体冲突 / ×N基准 / 🔥搜索新增N`。点 ★ 打开建议弹窗（含市场、命名风险、可做内页）。

---

## 推送内容

开启 `push.enabled` 后，到点（`daily_at_hour`）把摘要推到企业微信/钉钉/Telegram；已推过的不重复（`state/pushed.json`）。摘要标题为 **📊 每日摘要**，即时提醒标题为 **🚨【紧急】**，便于区分。摘要分五段：

| 段 | 来源 | 条件 |
|---|---|---|
| 🌍 新热词 | `trends.json` | `new` 且非噪音，且 **★≥`min_stars` 或 signal 为 breakout/rising** |
| 🎮 新游戏 | `games.json` | 已采曲线（`chart_at`） |
| 🎯 游戏雷达·可行动 | `gameradar.json` | `opportunity.decision ∈ {test-now, independent}` |
| 🔥 社媒外溢传播 | `gameradar.json` | `social.hasExternalSpillover` |
| 🚀 站点信号升温 | `sitemap.json` | `domain_count_7d ≥ 1`（别人开始抢） |

另有 **限流告警**（Google 被限流时，每天最多一次）。数量用 `top_trends/top_games/top_gameradar/top_sitemap`（或按 slot 的固定值）控制。

### ⚡ 即时提醒（urgent）

除定时摘要外，`push.urgent` 会在**每次运行**检查“最强、最该马上行动”的信号，命中就**立即推**（不等 `daily_at_hour`）：

| 指标 | 触发门槛（默认） |
|---|---|
| 🏆 游戏雷达决策 | `decision == independent`，或 `test-now` 且 `score ≥ gameradar_test_score(70)` |
| 🔥 社媒爆发 | `social.classification == viral` |
| 🎮 新游戏突涨 | `breakout`（涨幅≥500% 或 环比≥1.0） |
| 🚀 站点信号突增 | `domain_count_7d ≥ sitemap_7d(3)` |
| 🌍 强爆发新词 | `signal == breakout` 且 `new` 且 `★ ≥ trend_breakout_stars(4)` |

- **去重**：与定时摘要共用 `state/pushed.json`，推过不再推
- **半夜也推**：`urgent.ignore_quiet: true`（你已开启）；设 `false` 则半夜静默
- **失败重试**：发送失败自动重试 3 次（退避 2s/4s），并识别钉钉 `errcode`、Telegram `ok=false` 这类“HTTP 200 但实际失败”的情况；全部失败则不写去重标记，下一轮再试
- **标注**：即时消息标题带 **🚨【紧急】**，与普通摘要区分
- **时效**：每次 run 检查（hourly 组每小时一次），所以“立刻”≈最多延后到下一次 run

---

## 常见注意点

- **首次运行**：`signal/sitemap/fast` 首次为 **baseline**（不产出"新增"，属正常）；`fast` 的新增量需**至少跑两轮**。
- **LLM 可选**：不配 key 时翻译/分类/建议文案为空，但星级、分级、内容机会、市场判定等**纯代码部分照常**。
- **代理**：Trends/Suggest 走代理；Bing 直连（部分代理会阻断 Bing）。代理挂了相关项会失败但不影响其余。
- **SERP 竞争度兜底**：竞争度取值链为 **Bing → Yahoo Search → 竞品 Sitemap 收录数 → 默认 50**。Bing 解析不到结果时（如数据中心 IP 被拦）自动回退 Yahoo（Bing 系、直连、免 key）；两者都失败时用 sitemap 的 `domain_count` 估一个 0–100 的易做度（被越多域名收录越低），最后才回退默认 50。保证被限流时 ★ 不至于全变默认值。
- **噪音**：命中的词会被打 `noise` 标签，前端可一键隐藏，推送也默认过滤。
