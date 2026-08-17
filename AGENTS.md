# xueqiu_monitor 仓：Agent 工作入口

> 本文件是 Codex 与 Claude Code 共用的项目指令正本。`CLAUDE.md` 只保留
> `@AGENTS.md`，不要在两处重复维护规则。

## 项目定位

Flask 本地监控看板，单一仓库、单进程应用，整合三类数据：

- 雪球作者帖子监控（Playwright 无头浏览器抓取）。
- 商品/市场行情早盘快照（yfinance）。
- A/H 股官方公告追踪（巨潮资讯、HKEXnews）。
- 公司 watchlist 每日量价信号扫描（Yahoo→东财→腾讯三级数据源）。
- 可选的 Webhook 推送（企业微信、飞书、钉钉）。

技术取向刻意保守：一个 Flask 进程 + SQLite 本地存储 + Jinja 渲染 HTML 外壳 +
浏览器端原生 JavaScript 渲染数据。**不引入前端框架和构建步骤。**

## 仓库边界与工作环境

- 项目根目录：`/home/cuijie/workspace/web_dev/xueqiu_monitor`
- Python 环境：conda 环境 `xueqiu_monitor`。不要假设或创建项目本地 `.venv`；
  缺 Playwright 浏览器二进制时在该 conda 环境内显式安装。
- 启动命令（owner 实际使用）：

```bash
cd /home/cuijie/workspace/web_dev/xueqiu_monitor
conda run -n xueqiu_monitor python app.py
```

- 验证命令：

```bash
conda run -n xueqiu_monitor python -m py_compile app.py database.py fetcher.py notifier.py price_fetcher.py scheduler.py announcements.py config.py
conda run -n xueqiu_monitor flask --app app routes
conda run -n xueqiu_monitor python -m unittest discover -v
```

- 需要临时 Web 服务做验证时，若线上 `WEB_PORT`（默认 5001）被占用，改用其他
  端口，验证完停掉临时进程：

```bash
conda run -n xueqiu_monitor flask --app app run --host 127.0.0.1 --port 5002
```

- 后台定时任务与首次抓取只在 `app.py` 的 `if __name__ == "__main__"` 且非
  Werkzeug reloader 子进程时启动；测试中 `import app` 或使用 `app.test_client()`
  不会启动后台任务。
- 开始工作前先检查工作区状态和相关源码，不覆盖、不回退用户已有改动。仓库可能
  已存在 `.codex`、`CODE_REVIEW.md` 等未跟踪文件，不要动。

## 代码地图

- `app.py`：Flask 应用、路由、手动刷新接口、`do_fetch()` / `do_fetch_prices()` /
  `do_fetch_announcements()` / `do_scan_signals()`、启动时的后台任务接线。
- `config.py`：`.env` 驱动的配置（python-dotenv）。
- `database.py`：SQLite 建表、轻量幂等迁移、全部持久化助手函数。
- `fetcher.py`：Playwright 雪球帖子抓取器（WAF 绕过、分页、部分失败语义）。
- `signals.py`：公司量价信号——日 K 线三级数据源（Yahoo→东财→腾讯）与
  形态检测纯函数（缩量下跌、横盘企稳、放量上涨）。
- `price_fetcher.py`：yfinance 行情抓取。
- `announcements.py`：巨潮/HKEXnews 公告抓取（org_id/stock_id 解析与回写）。
- `scheduler.py`：APScheduler 共享 `BackgroundScheduler` 的四个任务。
- `notifier.py`：Webhook 推送（分批、重试、业务错误码检查）。
- `templates/index.html`、`templates/announcements.html`、
  `templates/companies.html`、`static/style.css`：深色看板 UI，纯 Jinja + 原生 JS。
- `xueqiu_monitor.db`：本地 SQLite 数据库，git 忽略，**不得删除**。

## 功能语义（改动前必读）

### 雪球帖子抓取

- 作者存于 `authors` 表，通过首页弹窗管理；帖子存 `posts`，按帖子 `id` 去重。
- `do_fetch()` 使用共享的 `XueqiuFetcher` singleton，跑在专用单 worker executor
  线程（`_fetcher_executor`）上——Playwright 同步 API 有 greenlet 线程绑定。
  `_stop_fetcher()` 会换新 executor，避免残留 asyncio event loop 导致下次
  `sync_playwright().start()` 报 "using Playwright Sync API inside the asyncio loop"。
- singleton 重启策略（重要，勿改坏）：
  - 仅当**所有作者**抓取失败（会话/WAF 级信号）或抓取层本身抛异常时重启；
    个别作者失败、DB/通知等**抓取层之外**的错误都不重启。
  - `fetcher.start()` 半途失败时 `_get_fetcher()` 先 `fetcher.stop()` 再上抛，
    防 Chromium 进程泄漏。
- 页内每次雪球 API 请求带 30 秒 `AbortSignal` 超时
  （`XueqiuFetcher._API_TIMEOUT_MS`），防止挂死请求永久持有抓取锁。
- `fetch_all_authors()` 返回 `(posts, errors)`：单个作者失败记入 errors 并继续，
  已抓到的帖子永远照常落库；全部失败时 `do_fetch()` 返回 `status=error` 且不推进
  `_last_fetch_time`，避免把旧数据呈现为刚检查过。
- `get_authors_summary()` 用 `LEFT JOIN posts`，零帖子的新作者也出现在侧边栏。
- 首页 API：`GET /api/posts`、`GET|POST /api/authors`、
  `PUT|DELETE /api/authors/<user_id>`、`PUT /api/authors/order`、`POST /api/refresh`。

### 行情

- `price_fetcher.py` 经 yfinance 抓取，存 `commodity_prices`；首页"早盘行情"展示
  各品种最新一条。API：`GET /api/prices`、`GET /api/prices/history`、
  `POST /api/prices/refresh`。

### 通知推送

- `notifier.py` 按渲染后 markdown 字节数分批（企业微信 3800、钉钉 18000、飞书
  20000），防止新增作者首抓时单条消息超限被平台拒收。
- `_post_with_retry` 除 HTTP 状态外还检查响应体 `errcode`/`code`——这三家平台
  业务失败返回 HTTP 200。
- 新帖子按已配置渠道入 `post_notification_outbox`；成功批次删除，失败批次保留
  attempts 与错误、留待下次雪球抓取重试；attempts 达到 `NOTIFICATION_MAX_ATTEMPTS`
  （默认 20）后移出队列并打日志，不得改回无限重试。

### 公告追踪

- watchlist 存 `announcement_watchlist`，公告存 `announcements`，按
  `(source, ann_id)` 去重。`source`：`hkex`（HKEXnews）、`cninfo`（巨潮）。
- 默认 watchlist 在 `init_db()` 时、仅当表为空插入：`02400.HK` 心动公司
  （hkex，stock_id `1000016859`）、`601919.SH` 中远海控（cninfo，org_id
  `9900003201`）。
- HKEX 的公开股票代码 ≠ 内部 `stockId`；巨潮 A 股查询需要代码 + `orgId`。
  两者为空时 `announcements.py` 自动解析并回写 `announcement_watchlist`，
  后续抓取跳过解析请求。
- `fetch_for_watchlist()` 返回 `(announcements, errors)`：单只股票失败不阻塞其他。
  巨潮按页码追到报告总数；HKEX 按官方 `hasNextRow`/`loadedRecord` 协议递增
  `rowRange`。全部股票失败时刷新返回 `status=error` 且不推进最近抓取时间。
- **公告结果当前故意不过滤**：UI 全量展示、仅标记关键词命中。owner 计划先积累
  一段时间再决定降噪规则——不要主动加过滤。
- **sentiment 利好/利空标签**：`announcements.classify_sentiment()` 按标题关键词
  分类（中英双语关键词表在 `POSITIVE_KEYWORDS`/`NEGATIVE_KEYWORDS`，冲突时
  **负面优先**），存 `announcements.sentiment` 列（幂等迁移），UI 展示徽标。
- **重点公司自动联动**：`do_fetch_announcements()` 开头调
  `_sync_focus_to_announcement_watchlist()`，把 `company_watchlist` 里
  `is_focus=1` 的公司自动加入公告 watchlist（**单向增加，不自动移除**；
  取消重点/删除公司后公告条目需手动删）。
- 页面与 API：`GET /announcements`、`GET /api/announcements`、
  `POST /api/announcements/refresh`、`GET|POST /api/announcement-stocks`、
  `DELETE /api/announcement-stocks/<code>`。

### 公司信号

- watchlist 存 `company_watchlist`（code 主键、`is_focus` 重点标记）；日 K 存
  `daily_klines`（`(code, date)` 主键，upsert）；信号存 `daily_signals`
  （`(code, date, signal_type)` 主键，天然去重）。
- `do_scan_signals()` 用 `_signal_scan_lock` 非阻塞锁（仿 `do_fetch_prices`）：
  K 线数 < `SIGNAL_MIN_HISTORY` 的公司预热近一年，其余只增量抓 12 自然日；
  全部公司抓取失败返回 `status=error` 且不推进 `_last_signal_scan_time`。
- 数据源三级降级：Yahoo `yf.download` 分批 25 只 → 逐只 `history()` → 东财
  `push2his`（前复权）→ 腾讯 `ifzq.gtimg`（北交所只有腾讯有）。Yahoo 会按 IP
  限流（429），**不要假设 Yahoo 永远可用**；批量下载异常只在确有公司缺失时
  才计入 errors。
- **成交量口径归一为"手"**：Yahoo A 股原始 volume 单位是股，东财/腾讯是手
  （1 手 = 100 股），跨源按 `(code, date)` 混存时口径不一致会让量比失真百倍。
  `_normalize_yahoo_volume` 在 Yahoo 数据入库前把 A 股 volume ÷100；港股三源
  均为股，保持不变。勿删此归一。
- 检测前提：历史不足或最新 K 线距今 >3 自然日（停牌/节假日）跳过，防止拿旧
  行情误报。
- 形态共四种：`high_vol_up` 放量上涨、`low_vol_bottom` 缩量下跌·底部
  （缩量阴跌 + 收盘价距 120 日低点 ≤10% + 距高点回撤 ≥20% + 单日跌幅 ≤5%，
  底部潜伏形态，detail 注明需人工核实基本面）、`consolidation` 横盘企稳、
  `low_vol_down` 缩量下跌。页面与推送均按此优先级排序（`get_signals` 的
  CASE 排序，勿改回按 code 排序）。
- 推送策略：**只推 `is_focus=1` 的公司**；同一公司同一形态
  `SIGNAL_REPEAT_SUPPRESS_DAYS` 天内不重复推（仍照常入库）。信号全部公司都
  在 `/companies` 页面可见。
- 页面与 API：`GET /companies`、`GET /api/companies`、`GET|POST
  /api/company-stocks`、`POST /api/company-stocks/import`（批量导入）、
  `DELETE /api/company-stocks/<code>`、`PUT /api/company-stocks/<code>/focus`、
  `GET /api/signals`、`POST /api/signals/refresh`。

### 后台任务

`scheduler.py` 共享一个 `BackgroundScheduler`（时区 Asia/Shanghai）：

- `start_scheduler(do_fetch, FETCH_INTERVAL_MINUTES)`：雪球帖子。
- `start_price_scheduler(do_fetch_prices, PRICE_REPORT_HOUR, PRICE_REPORT_MINUTE)`：
  工作日行情日报。
- `start_signal_scheduler(do_scan_signals, SIGNAL_REPORT_HOUR, SIGNAL_REPORT_MINUTE)`：
  工作日收盘后公司量价信号扫描（默认 16:40）。
- `start_announcement_scheduler(do_fetch_announcements, ANNOUNCEMENT_FETCH_INTERVAL_MINUTES)`：
  公告追踪。

`stop_scheduler()` 通过 `atexit` 注册，进程退出时优雅关闭。

## 配置

配置在 `.env` + `config.py`。关键变量：

- `XQ_A_TOKEN` / `XQ_R_TOKEN`：雪球 cookie。
- `POST_LOOKBACK_DAYS`（默认 7）、`POST_FETCH_PAGE_SIZE`（默认 20）、
  `FETCH_INTERVAL_MINUTES`（默认 60）。
- `WECHAT_WEBHOOK_URL` / `FEISHU_WEBHOOK_URL` / `DINGTALK_WEBHOOK_URL`：留空即禁用。
- `NOTIFICATION_MAX_ATTEMPTS`：单条通知失败重试上限，默认 20。
- `PRICE_REPORT_HOUR` / `PRICE_REPORT_MINUTE`：默认 8:30。
- `ANNOUNCEMENT_LOOKBACK_DAYS`（默认 30）、`ANNOUNCEMENT_FETCH_PAGE_SIZE`
  （默认 50）、`ANNOUNCEMENT_FETCH_INTERVAL_MINUTES`（默认 60）。
- `SIGNAL_REPORT_HOUR` / `SIGNAL_REPORT_MINUTE`：信号扫描时刻，默认 16:40。
- `SIGNAL_*` 阈值：`SIGNAL_MIN_HISTORY`（25）、`SIGNAL_LOWVOL_RATIO`（0.6）、
  `SIGNAL_CONSOLIDATION_DAYS`（10）、`SIGNAL_CONSOLIDATION_MAX_RANGE`（0.03）、
  `SIGNAL_CONSOLIDATION_MAX_DRIFT`（0.05）、`SIGNAL_CONSOLIDATION_VOL_RATIO`
  （0.8）、`SIGNAL_UP_MIN_PCT`（0.03）、`SIGNAL_UP_VOL_RATIO`（2.0）、
  `SIGNAL_REPEAT_SUPPRESS_DAYS`（3）。
- `WEB_HOST` / `WEB_PORT`：默认 `127.0.0.1:5001`。
- `FLASK_DEBUG`：设为 `1`/`true`/`yes`/`on` 启用调试模式。

## 数据库约定

`database.init_db()` 启动时建表并做轻量迁移。表：

- `posts`（主键 `id`）、`authors`（主键 `user_id`，含 `sort_order`）、
  `commodity_prices`、`announcement_watchlist`（主键 `code`）、
  `announcements`（主键 `(source, ann_id)`）、
  `post_notification_outbox`（主键 `(post_id, channel)`）、
  `company_watchlist`（主键 `code`）、`daily_klines`（主键 `(code, date)`）、
  `daily_signals`（主键 `(code, date, signal_type)`）。

约定：

- 新增 schema 变更一律走 `init_db()` 或其助手函数里的**幂等迁移**逻辑。
- `save_announcements()` 按 `(source, ann_id)` upsert；`first_seen_at` 只在首次
  插入写入，后续更新不得覆盖。
- `save_posts()` 单事务内逐条插入：主键冲突静默跳过，其他单条错误记录后跳过，
  保证整批照常提交。
- **保留 `xueqiu_monitor.db` 中的全部既有用户数据。**

## UI 约定

- 遵循 `static/style.css` 的现有深色看板风格，卡片保持紧凑（信息密度高）。
- 页面为 Jinja HTML + 原生 JavaScript，不引入框架或构建步骤。
- 渲染外部数据一律走 `escapeHtml` / `textContent` / DOM API；外链 URL 使用协议
  白名单校验（参照 `announcements.html` 的 `safeExternalUrl`），外链加
  `rel="noopener noreferrer"`。

## 通用工作流程

1. 先确认用户目标、当前分支和工作区状态。
2. 先读实际代码和测试，再决定实现；不要只依赖本文件猜测现状。
3. 修改保持范围最小，保护用户已有改动，不使用破坏性 Git 命令。
4. 完成后运行与风险匹配的验证：`py_compile` + `flask --app app routes` +
   `python -m unittest discover -v` 是基线；涉及 UI 时起临时服务实机确认。
5. review 整体 diff，如实报告已验证、未验证、风险和需要用户决定的事项。

## Git 与交付

- 提交前基线门禁：`py_compile` 与全部单元测试必须通过；任何一项失败不得提交，
  不得通过跳过、删除或弱化测试制造通过。
- commit、push 的时机由用户或宿主工作流决定；本文件不额外授权推送等外部写操作。

## 绝对安全底线

- `.env`（含雪球 token、Webhook URL）与数据库文件不得提交、不得覆盖或删除。
- 未经用户明确授权，不执行删除数据、覆盖改动、推送等难以恢复的操作。
- 雪球、巨潮、HKEXnews 均为非官方/易变接口：保持错误在 API 响应与日志中可见，
  不要静默吞掉。
- 任务触及数据库既有数据、迁移逻辑、抓取 singleton 生命周期或通知重试语义时，
  先对照上文专项规则核对，拿不准就先向用户确认。
