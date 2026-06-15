# CLAUDE.md

This file gives Claude Code the project context for `/home/cuijie/workspace/web_dev/xueqiu_monitor`.

## Project Summary

`xueqiu_monitor` is a local Flask dashboard for investment monitoring. It tracks Xueqiu author posts, commodity/market prices, and official company announcements for A/H shares.

The owner uses the conda environment named `xueqiu_monitor`.

```bash
cd /home/cuijie/workspace/web_dev/xueqiu_monitor
conda run -n xueqiu_monitor python app.py
```

Use this conda environment for validation. Do not introduce or rely on a project `.venv`.

## Important Files

- `app.py`: Flask app, route handlers, refresh functions, startup job registration.
- `config.py`: `.env`-driven runtime configuration.
- `database.py`: SQLite schema and persistence helpers.
- `fetcher.py`: Playwright Xueqiu post fetcher.
- `price_fetcher.py`: `yfinance` commodity/market price fetcher.
- `announcements.py`: HKEXnews and CNINFO announcement fetcher.
- `scheduler.py`: APScheduler job setup.
- `notifier.py`: Webhook push integration.
- `templates/index.html`: Main Xueqiu dashboard.
- `templates/announcements.html`: Announcement tracking page and watchlist management UI.
- `static/style.css`: Shared UI styles.
- `xueqiu_monitor.db`: Local SQLite database; do not delete.

## Validation Commands

Run:

```bash
conda run -n xueqiu_monitor python -m py_compile app.py database.py fetcher.py notifier.py price_fetcher.py scheduler.py announcements.py config.py
conda run -n xueqiu_monitor flask --app app routes
```

For route tests that should not start background jobs, import the Flask app with `flask --app app ...` or use `app.test_client()`. The scheduled jobs only start in `app.py` under `if __name__ == "__main__"`.

If a temporary server is needed, use another port when `5001` is already running, and stop the process after testing:

```bash
conda run -n xueqiu_monitor flask --app app run --host 127.0.0.1 --port 5002
```

## Configuration

Configuration is loaded from `.env` by `config.py`.

Key variables:

- `XQ_A_TOKEN`, `XQ_R_TOKEN`: Xueqiu cookies/tokens.
- `POST_LOOKBACK_DAYS`: Xueqiu fetch lookback, default `7`.
- `POST_FETCH_PAGE_SIZE`: Xueqiu page size, default `20`.
- `FETCH_INTERVAL_MINUTES`: Xueqiu fetch interval, default `60`.
- `WECHAT_WEBHOOK_URL`, `FEISHU_WEBHOOK_URL`, `DINGTALK_WEBHOOK_URL`: notification webhooks.
- `PRICE_REPORT_HOUR`, `PRICE_REPORT_MINUTE`: weekday price report time, defaults `08:30`.
- `ANNOUNCEMENT_LOOKBACK_DAYS`: announcement fetch lookback, default `30`.
- `ANNOUNCEMENT_FETCH_PAGE_SIZE`: announcement page size per stock, default `50`.
- `ANNOUNCEMENT_FETCH_INTERVAL_MINUTES`: announcement background fetch interval, default `60`.
- `WEB_HOST`, `WEB_PORT`: Flask bind host/port, defaults `127.0.0.1:5001`.
- `FLASK_DEBUG`: debug toggle.

Never commit `.env`.

## Database

SQLite is managed in `database.py`.

Tables:

- `authors`: watched Xueqiu authors.
- `posts`: fetched Xueqiu posts.
- `commodity_prices`: price snapshots.
- `announcement_watchlist`: watched companies for announcement tracking.
- `announcements`: fetched announcements, deduplicated by `(source, ann_id)`.

`database.init_db()` creates missing tables and lightweight migrations. Keep migrations idempotent and preserve existing data.

### Author summary

`get_authors_summary()` uses `authors LEFT JOIN posts` so that newly added authors with zero posts still appear in the sidebar.

### Announcement upsert semantics

`save_announcements()` uses `INSERT ... ON CONFLICT(source, ann_id) DO UPDATE`. The `first_seen_at` column is preserved from the original insert (`COALESCE(announcements.first_seen_at, excluded.first_seen_at)`) and is never overwritten on subsequent updates.

### Post save resilience

`save_posts()` inserts each post individually inside a single transaction. `IntegrityError` (duplicate post) is silently skipped; other per-post exceptions are logged and skipped so that one bad record does not roll back the entire batch.

## Main Workflows

### Xueqiu Posts

`do_fetch()` in `app.py`:

1. Acquires a shared `XueqiuFetcher` singleton (lazy-started once, reused across calls).
2. Loads authors from `authors`.
3. Fetches recent posts.
4. Saves new posts in `posts`.
5. Sends webhook notification for new posts.

The fetcher singleton is started on first use and only stopped on fetch failure or process exit. This avoids the 5–10 second Chromium startup cost on every scheduled run.

`_stop_fetcher()` also replaces `_fetcher_executor` with a fresh `ThreadPoolExecutor`. This is necessary because `sync_playwright().start()` leaves an asyncio event loop on the executor thread; if the same thread is reused, the next `start()` raises `"using Playwright Sync API inside the asyncio loop"`. A fresh executor gets a clean thread and recovers automatically.

### Prices

`do_fetch_prices()` in `app.py`:

1. Calls `fetch_prices()`.
2. Saves rows to `commodity_prices`.
3. Sends price webhook notification.

### Announcements

`do_fetch_announcements()` in `app.py`:

1. Reads `announcement_watchlist`.
2. Calls `announcements.fetch_for_watchlist()`.
3. Saves all fetched announcements in `announcements`.
4. Returns total and newly inserted counts.

Announcement page:

- `/announcements`: UI page.
- `/api/announcements`: list announcements and watchlist.
- `/api/announcements/refresh`: manual fetch.
- `/api/announcement-stocks`: list/add watched companies.
- `/api/announcement-stocks/<code>`: delete watched company.

Current behavior intentionally shows all announcements. Keyword matching is only marked visually; it is not used to filter announcements yet.

Default announcement watchlist:

- `02400.HK` 心动公司, `source=hkex`, `stock_id=1000016859`.
- `601919.SH` 中远海控, `source=cninfo`, `org_id=9900003201`.

For new companies:

- Hong Kong stocks use `source=hkex` and codes like `00700.HK`. `stock_id` may be blank; the fetcher resolves it.
- A shares use `source=cninfo` and codes like `600519.SH`, `000001.SZ`, or `8xxxxx.BJ`. `org_id` may be blank; the fetcher resolves it.

## Development Rules

- Keep the app dependency-light: Flask, SQLite, vanilla JavaScript, no frontend build step.
- Follow existing dark dashboard styling in `static/style.css`.
- Do not remove existing user data in `xueqiu_monitor.db`.
- Do not overwrite `.env`.
- Do not add filtering to announcement results unless requested; the current goal is collecting enough raw announcements to judge noise later.
- Prefer official disclosure sources over finance community sites:
  - HKEXnews for HK stocks.
  - CNINFO for A shares.
- Avoid destructive git operations and avoid reverting unrelated local changes.
