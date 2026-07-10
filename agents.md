# xueqiu_monitor Agent Guide

This project is a Flask-based local monitoring dashboard for investment-related information. It currently combines:

- Xueqiu author post monitoring.
- Commodity/market early-session price snapshots.
- A/H share announcement tracking.
- Optional push notifications through webhook integrations.

Use this file as the operational context for Codex or other coding agents working in this repository.

## Runtime

- Project root: `/home/cuijie/workspace/web_dev/xueqiu_monitor`
- Python environment: conda environment `xueqiu_monitor`
- Start command used by the owner:

```bash
cd /home/cuijie/workspace/web_dev/xueqiu_monitor
conda run -n xueqiu_monitor python app.py
```

Do not assume a project-local `.venv` is the canonical runtime. The project uses the conda environment above.

For validation:

```bash
conda run -n xueqiu_monitor python -m py_compile app.py database.py fetcher.py notifier.py price_fetcher.py scheduler.py announcements.py config.py
conda run -n xueqiu_monitor flask --app app routes
conda run -n xueqiu_monitor python -m unittest discover -v
```

If a temporary web server is needed for validation, use a port other than the live `WEB_PORT` when it is already occupied, and stop the temporary process afterward.

## Architecture

- `app.py`: Flask application, HTTP routes, manual refresh endpoints, and startup wiring for background jobs.
- `config.py`: Environment-driven configuration loaded from `.env` through `python-dotenv`.
- `database.py`: SQLite schema creation, migrations-on-init, and persistence helpers.
- `fetcher.py`: Playwright-based Xueqiu author post fetcher.
- `price_fetcher.py`: Commodity/market price fetcher using `yfinance`.
- `announcements.py`: Official disclosure-source announcement fetcher for HKEXnews and CNINFO.
- `scheduler.py`: APScheduler setup for background jobs.
- `notifier.py`: Webhook push integrations for Xueqiu posts and prices.
- `templates/index.html`: Main dashboard for Xueqiu posts and early-session price bar.
- `templates/announcements.html`: Announcement tracking page and watchlist management UI.
- `static/style.css`: Shared dark-theme styles for the dashboard and announcement page.
- `xueqiu_monitor.db`: Local SQLite database, ignored by git.

The app is intentionally simple: it uses one Flask process, SQLite for local state, server-rendered HTML shells, and browser-side JavaScript for API-driven rendering.

## Main Features

### Xueqiu Author Monitoring

- Authors are stored in the `authors` table and managed from the homepage modal.
- `do_fetch()` in `app.py` uses a shared `XueqiuFetcher` singleton to fetch recent posts for tracked authors.
- Posts are stored in `posts`, deduplicated by post `id`.
- The singleton runs on a dedicated executor thread (`_fetcher_executor`, single-worker `ThreadPoolExecutor`) because Playwright's sync API requires greenlet thread affinity. On `_stop_fetcher()`, the executor is replaced with a fresh one to avoid a residual asyncio event loop that would block `sync_playwright().start()` on the same thread.
- `fetch_all_authors()` returns `(posts, errors)`. A failure for one author is recorded in `errors` and the remaining authors are still fetched; already-fetched posts are always saved. `do_fetch()` restarts the singleton only when every author failed (session/WAF-level signal), and includes an `errors` list in the API response on partial failure.
- When every configured author fails, `do_fetch()` returns `status=error` and does not advance `_last_fetch_time`, so stale data is not presented as freshly checked.
- If `fetcher.start()` fails partway, `_get_fetcher()` calls `fetcher.stop()` before re-raising to avoid leaking Chromium processes.
- `get_authors_summary()` returns every tracked author via `LEFT JOIN posts`, so authors with zero posts still appear in the sidebar.
- Homepage APIs:
  - `GET /api/posts`
  - `GET /api/authors`
  - `POST /api/authors`
  - `PUT /api/authors/<user_id>`
  - `DELETE /api/authors/<user_id>`
  - `PUT /api/authors/order`
  - `POST /api/refresh`

### Early-Session Price Bar

- `price_fetcher.py` fetches prices through `yfinance`.
- Prices are stored in `commodity_prices`.
- Homepage shows the latest price cards under `早盘行情`.
- APIs:
  - `GET /api/prices`
  - `GET /api/prices/history`
  - `POST /api/prices/refresh`

### Notifications

- `notifier.py` splits post notifications into batches by rendered markdown size (WeChat Work 3800 bytes, DingTalk 18000, Feishu 20000) so large batches (e.g. first fetch after adding an author) are not rejected by platform limits.
- `_post_with_retry` also checks the response body for `errcode`/`code`, because WeChat Work/DingTalk/Feishu report business failures with HTTP 200.
- New posts are enqueued in `post_notification_outbox` per enabled channel. Successful batches are removed; failed batches retain their attempt count and error and are retried on a later Xueqiu fetch.

### Announcement Tracking

- Watchlist is stored in `announcement_watchlist`.
- Announcements are stored in `announcements`, deduplicated by `(source, ann_id)`.
- Sources:
  - `hkex`: HKEXnews for Hong Kong listed companies.
  - `cninfo`: CNINFO for A-share announcements.
- Default watchlist inserted during `database.init_db()` if the watchlist table is empty:
  - `02400.HK` 心动公司, source `hkex`, stock_id `1000016859`.
  - `601919.SH` 中远海控, source `cninfo`, org_id `9900003201`.
- The announcement page is linked from the homepage early-session price bar as `公告追踪`.
- Page and APIs:
  - `GET /announcements`
  - `GET /api/announcements`
  - `POST /api/announcements/refresh`
  - `GET /api/announcement-stocks`
  - `POST /api/announcement-stocks`
  - `DELETE /api/announcement-stocks/<code>`

Announcement results are intentionally not filtered out yet. The UI displays all fetched announcements and only marks keyword hits. The owner plans to run it for a while before deciding what to suppress.

`fetch_for_watchlist()` returns `(announcements, errors)`: a failure for one stock does not block the others, and `POST /api/announcements/refresh` includes an `errors` list on partial failure.

CNINFO follows numbered result pages until the reported total is loaded. HKEX follows its official load-more protocol by increasing `rowRange` until `hasNextRow` is false. If every watched stock fails, the refresh returns `status=error` without advancing the last-fetch timestamp.

## Configuration

Configuration lives in `.env` and `config.py`.

Known environment variables:

- `XQ_A_TOKEN`: Xueqiu `xq_a_token`.
- `XQ_R_TOKEN`: Xueqiu `xq_r_token`.
- `POST_LOOKBACK_DAYS`: Xueqiu post lookback window, default `7`.
- `POST_FETCH_PAGE_SIZE`: Xueqiu fetch page size, default `20`.
- `FETCH_INTERVAL_MINUTES`: Xueqiu background fetch interval, default `60`.
- `WECHAT_WEBHOOK_URL`: Enterprise WeChat webhook. Empty disables push.
- `FEISHU_WEBHOOK_URL`: Feishu webhook. Empty disables push.
- `DINGTALK_WEBHOOK_URL`: DingTalk webhook. Empty disables push.
- `PRICE_REPORT_HOUR`: Commodity price daily job hour, default `8`.
- `PRICE_REPORT_MINUTE`: Commodity price daily job minute, default `30`.
- `ANNOUNCEMENT_LOOKBACK_DAYS`: Announcement fetch lookback window, default `30`.
- `ANNOUNCEMENT_FETCH_PAGE_SIZE`: Announcement fetch page size per stock, default `50`.
- `ANNOUNCEMENT_FETCH_INTERVAL_MINUTES`: Announcement background fetch interval, default `60`.
- `WEB_HOST`: Flask bind host, default `127.0.0.1`.
- `WEB_PORT`: Flask port, default `5001`.
- `FLASK_DEBUG`: Enables Flask debug mode when set to `1`, `true`, `yes`, or `on`.

Do not commit `.env` or database files.

## Database Design

`database.init_db()` creates and lightly migrates tables on startup.

Tables:

- `posts`: Xueqiu posts. Primary key `id`.
- `authors`: Xueqiu tracked authors. Primary key `user_id`; includes `sort_order`.
- `commodity_prices`: price snapshots.
- `announcement_watchlist`: tracked companies for disclosure monitoring. Primary key `code`.
- `announcements`: fetched announcement records. Primary key `(source, ann_id)`.
- `post_notification_outbox`: pending post Webhook deliveries. Primary key `(post_id, channel)`.

When adding schema changes, prefer idempotent migration logic in `init_db()` or helper functions called by it. Preserve existing user data in `xueqiu_monitor.db`.

### Upsert semantics

- `save_announcements()` upserts on `(source, ann_id)`. `first_seen_at` is written only on the initial insert and is protected from later overwrites.
- `save_posts()` inserts each post individually within one transaction. Duplicate posts (`IntegrityError`) are skipped; other per-post errors are logged and skipped so the rest of the batch still commits.

## Background Jobs

`scheduler.py` uses a shared `BackgroundScheduler`:

- `start_scheduler(do_fetch, FETCH_INTERVAL_MINUTES)`: Xueqiu posts.
- `start_price_scheduler(do_fetch_prices, PRICE_REPORT_HOUR, PRICE_REPORT_MINUTE)`: price daily job.
- `start_announcement_scheduler(do_fetch_announcements, ANNOUNCEMENT_FETCH_INTERVAL_MINUTES)`: announcement tracking.

These start only under `if __name__ == "__main__"` in `app.py` and only outside the Werkzeug reloader child. Importing `app` for tests should not start the background jobs.

`stop_scheduler()` is registered with `atexit` when the scheduler starts, so the daemon thread shuts down gracefully on process exit.

## Announcement Source Notes

For HKEX:

- The public stock code is not the same as HKEX's internal `stockId`.
- `announcements.py` resolves `stockId` from HKEX's active stock JSON when `stock_id` is blank, then persists it back to `announcement_watchlist` so later runs skip the full-list download.

For CNINFO:

- A-share queries need both stock code and CNINFO `orgId`.
- `announcements.py` resolves `orgId` through CNINFO search when `org_id` is blank, then persists it back to `announcement_watchlist` so later runs skip resolution.

## UI Guidelines

- Match the existing dark dashboard style in `static/style.css`.
- Avoid adding a new framework or build step. Current pages are plain Jinja HTML plus vanilla JavaScript.
- Keep cards compact because the dashboard is information-dense.
- The announcement page currently keeps all announcements visible; do not add filtering by default unless the user asks.

## Operational Cautions

- Do not remove or rewrite `.env`, `xueqiu_monitor.db`, or existing user data.
- Do not revert unrelated local changes. The repository may already have untracked files such as `.codex` or `CODE_REVIEW.md`.
- Do not run destructive git commands.
- Do not rely on Xueqiu, HKEX, or CNINFO unofficial endpoints staying stable; keep error handling visible in API responses and logs.
- If Playwright browser binaries are missing for the conda environment, install them explicitly in that environment rather than creating a new project-local virtualenv.
