"""
雪球作者动态监控看板 — Flask Web 应用
"""
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

import config
import announcements
import database
import signals
from fetcher import XueqiuFetcher
from notifier import (deliver_post_notifications, notify_new_posts, notify_prices,
                      notify_signals)
from price_fetcher import fetch_prices
from scheduler import (start_scheduler, start_price_scheduler,
                       start_announcement_scheduler, start_signal_scheduler)

app = Flask(__name__)

# 全局状态
_last_fetch_time = None
_last_announcement_fetch_time = None
_last_signal_scan_time = None
_fetch_lock = threading.Lock()
_price_fetch_lock = threading.Lock()
_announcement_fetch_lock = threading.Lock()
_signal_scan_lock = threading.Lock()
_USER_ID_RE = re.compile(r"^\d{1,32}$")
_ANNOUNCEMENT_CODE_RE = re.compile(r"^\d{5}\.HK$|^\d{6}\.(SH|SZ|BJ)$")
_fetcher_singleton = None
_fetcher_lock = threading.Lock()
# Playwright sync API 通过 greenlet 绑定到创建它的线程，跨线程调用会抛
# `greenlet.error: cannot switch to a different thread`。用单 worker 的
# executor 把所有 fetcher 操作钉在同一线程上执行。
_fetcher_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xq-fetcher")


def _is_valid_user_id(user_id: str) -> bool:
    return bool(_USER_ID_RE.fullmatch(user_id or ""))


def _get_json_payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def _webhook_channels() -> dict[str, str]:
    return {
        "wechat": config.WECHAT_WEBHOOK_URL,
        "feishu": config.FEISHU_WEBHOOK_URL,
        "dingtalk": config.DINGTALK_WEBHOOK_URL,
    }


def _deliver_pending_post_notifications() -> list[str]:
    """发送 outbox 中的帖子通知，失败项保留到后续抓取重试。"""
    errors = []
    for channel, url in _webhook_channels().items():
        if not url:
            continue
        pending_posts = database.get_pending_post_notifications(channel)
        if not pending_posts:
            continue
        delivery = deliver_post_notifications(pending_posts, channel, url)
        database.complete_post_notifications(channel, delivery["sent_ids"])
        for failure in delivery["failures"]:
            expired_ids = database.fail_post_notifications(
                channel,
                failure["post_ids"],
                failure["error"],
                max_attempts=config.NOTIFICATION_MAX_ATTEMPTS,
            )
            errors.append(f"{channel}: {failure['error']}")
            if expired_ids:
                print(
                    f"⚠️ {channel} 通知重试 {config.NOTIFICATION_MAX_ATTEMPTS} 次仍失败，"
                    f"放弃 {len(expired_ids)} 条: {', '.join(expired_ids)}"
                )
    return errors


def _run_on_fetcher_thread(fn, *args, **kwargs):
    """把任意函数派发到固定的 fetcher worker 线程上同步执行。"""
    return _fetcher_executor.submit(fn, *args, **kwargs).result()


def _get_fetcher():
    global _fetcher_singleton
    with _fetcher_lock:
        if _fetcher_singleton is None:
            fetcher = XueqiuFetcher(config.XQ_A_TOKEN, config.XQ_R_TOKEN)
            try:
                _run_on_fetcher_thread(fetcher.start)
            except Exception:
                # start() 半途失败（如 goto 被 WAF 卡住超时）时浏览器可能已经
                # 启动，singleton 未赋值就没人管它了，必须就地清理否则每次
                # 重试都泄漏一个 Chromium 进程
                try:
                    _run_on_fetcher_thread(fetcher.stop)
                except Exception:
                    pass
                raise
            _fetcher_singleton = fetcher
        return _fetcher_singleton


def _stop_fetcher():
    global _fetcher_singleton, _fetcher_executor
    with _fetcher_lock:
        if _fetcher_singleton:
            try:
                _run_on_fetcher_thread(_fetcher_singleton.stop)
            except Exception:
                import traceback
                print("⚠️ 关闭浏览器失败（可能残留 Chromium 进程）:")
                traceback.print_exc()
            _fetcher_singleton = None
        # sync_playwright().start() 会在 executor 线程上创建 asyncio event loop；
        # stop() 未必能完全清理，导致同线程下次 start() 报
        # "using Playwright Sync API inside the asyncio loop"。重建 executor
        # 换一个干净线程即可恢复。
        old_executor = _fetcher_executor
        _fetcher_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xq-fetcher")
        old_executor.shutdown(wait=False)


def do_fetch(wait_timeout: float = None):
    """执行一次抓取任务。

    Parameters
    ----------
    wait_timeout : float, optional
        等待锁的秒数。``None`` 表示永久等待（用于用户手动刷新），
        ``0`` 表示非阻塞立即返回（用于定时调度器跳过）。
    """
    global _last_fetch_time
    if wait_timeout == 0:
        acquired = _fetch_lock.acquire(blocking=False)
    elif wait_timeout is None:
        acquired = _fetch_lock.acquire(blocking=True)
    else:
        acquired = _fetch_lock.acquire(blocking=True, timeout=wait_timeout)
    if not acquired:
        print("⏳ 上一次抓取尚未完成，跳过")
        return {"status": "skipped", "reason": "already running"}

    try:
        db_authors = database.get_db_authors()
        authors = [{"id": a["user_id"], "name": a["name"]} for a in db_authors]
        last_post_at = {
            row["user_id"]: row.get("latest_at") or 0
            for row in database.get_authors_summary()
        }
        since_dt = datetime.now() - timedelta(days=config.POST_LOOKBACK_DAYS)
        since_ms = int(since_dt.timestamp() * 1000)
        try:
            fetcher = _get_fetcher()
            all_posts, fetch_errors = _run_on_fetcher_thread(
                fetcher.fetch_all_authors,
                authors,
                since_ms=since_ms,
                page_size=config.POST_FETCH_PAGE_SIZE,
                last_post_at=last_post_at,
            )
        except Exception:
            # 只有浏览器/抓取器层面的失败才需要重启 singleton；
            # DB、通知等环节的错误与 Chromium 会话无关，重启纯属浪费
            _stop_fetcher()
            raise

        new_posts = database.save_posts(all_posts)
        if new_posts:
            channels = [channel for channel, url in _webhook_channels().items() if url]
            database.enqueue_post_notifications(new_posts, channels)
            notify_new_posts(new_posts)
        notification_errors = _deliver_pending_post_notifications()

        # 全部作者都失败基本可断定是会话失效/WAF 拦截而非个别作者异常，
        # 重启浏览器让下次抓取重新过 WAF；个别作者失败则保留 singleton，
        # 避免一个长期异常的作者导致每轮都重启浏览器。
        if fetch_errors and len(fetch_errors) == len(authors):
            _stop_fetcher()
            result = {
                "status": "error",
                "message": "所有作者抓取失败，未更新最近抓取时间",
                "errors": [f"{e['name']}: {e['error']}" for e in fetch_errors],
            }
            if notification_errors:
                result["notification_errors"] = notification_errors
            return result

        _last_fetch_time = datetime.now().isoformat()

        result = {
            "status": "ok",
            "total_fetched": len(all_posts),
            "new_posts": len(new_posts),
            "lookback_days": config.POST_LOOKBACK_DAYS,
            "time": _last_fetch_time,
        }
        if fetch_errors:
            result["errors"] = [f"{e['name']}: {e['error']}" for e in fetch_errors]
        if notification_errors:
            result["notification_errors"] = notification_errors
        return result
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        _fetch_lock.release()


def start_initial_fetch_background():
    """启动后在后台执行首次抓取，避免阻塞 Web 服务监听"""
    def _run():
        print("📦 首次抓取数据已在后台启动...")
        result = do_fetch()
        if result.get("status") == "ok":
            print(
                "✅ 首次后台抓取完成："
                f"获取 {result.get('total_fetched', 0)} 条，"
                f"新增 {result.get('new_posts', 0)} 条"
            )
        elif result.get("status") == "skipped":
            print("⏳ 首次后台抓取跳过：已有抓取任务在运行")
        else:
            print(f"❌ 首次后台抓取失败：{result.get('message', '未知错误')}")

    thread = threading.Thread(
        target=_run,
        name="initial-xueqiu-fetch",
        daemon=True,
    )
    thread.start()
    return thread


# ==================== 路由 ====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/posts")
def api_posts():
    author_id = request.args.get("author_id", None)
    if author_id and not _is_valid_user_id(author_id):
        return jsonify({"status": "error", "message": "author_id 格式非法"}), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0

    rows = database.get_recent_posts(limit + 1, author_id, offset)
    posts = rows[:limit]
    has_more = len(rows) > limit
    total = database.count_posts(author_id)
    # 转换时间戳为可读格式
    for p in posts:
        ts = p.get("created_at", 0)
        if ts:
            p["created_at_fmt"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        else:
            p["created_at_fmt"] = ""
    return jsonify({
        "posts": posts,
        "last_fetch": _last_fetch_time,
        "has_more": has_more,
        "next_offset": offset + len(posts),
        "total": total,
    })


@app.route("/api/authors")
def api_authors():
    summary = database.get_authors_summary()
    for s in summary:
        ts = s.get("latest_at", 0)
        if ts:
            s["latest_at_fmt"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        else:
            s["latest_at_fmt"] = ""
    db_authors = database.get_db_authors()
    return jsonify({
        "authors": summary,
        "db_authors": db_authors,
        "total_posts": database.count_posts(),
    })


@app.route("/api/authors", methods=["POST"])
def api_add_author():
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    user_id = str(data.get("user_id", "")).strip()
    name = str(data.get("name", "")).strip()
    if not user_id or not name:
        return jsonify({"status": "error", "message": "user_id 和 name 不能为空"}), 400
    if not _is_valid_user_id(user_id):
        return jsonify({"status": "error", "message": "user_id 必须为纯数字"}), 400
    ok = database.add_author(user_id, name)
    if ok:
        return jsonify({"status": "ok", "user_id": user_id, "name": name})
    return jsonify({"status": "error", "message": "作者已存在"}), 409


@app.route("/api/authors/order", methods=["PUT"])
def api_reorder_authors():
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    user_ids = data.get("user_ids", [])
    if not isinstance(user_ids, list) or not user_ids:
        return jsonify({"status": "error", "message": "user_ids 必须是非空数组"}), 400
    user_ids = [str(user_id).strip() for user_id in user_ids]
    if any(not _is_valid_user_id(user_id) for user_id in user_ids):
        return jsonify({"status": "error", "message": "user_ids 必须均为纯数字"}), 400
    if not database.update_authors_order(user_ids):
        return jsonify({"status": "error", "message": "作者顺序保存失败"}), 400
    return jsonify({"status": "ok"})


@app.route("/api/authors/<user_id>", methods=["DELETE"])
def api_delete_author(user_id):
    if not _is_valid_user_id(user_id):
        return jsonify({"status": "error", "message": "user_id 必须为纯数字"}), 400
    database.delete_author(user_id)
    return jsonify({"status": "ok"})


@app.route("/api/authors/<user_id>", methods=["PUT"])
def api_update_author(user_id):
    if not _is_valid_user_id(user_id):
        return jsonify({"status": "error", "message": "user_id 必须为纯数字"}), 400
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"status": "error", "message": "name 不能为空"}), 400
    database.update_author(user_id, name)
    return jsonify({"status": "ok"})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    result = do_fetch(wait_timeout=120)
    return jsonify(result)


# ==================== 商品价格 ====================

def do_fetch_prices():
    """执行一次价格抓取并推送"""
    if not _price_fetch_lock.acquire(blocking=False):
        print("⏳ 上一次价格抓取尚未完成，跳过")
        return {"status": "skipped", "reason": "already running"}
    try:
        prices = fetch_prices()
        database.save_prices(prices)
        notify_prices(prices, {
            "wechat_webhook_url":   config.WECHAT_WEBHOOK_URL,
            "feishu_webhook_url":   config.FEISHU_WEBHOOK_URL,
            "dingtalk_webhook_url": config.DINGTALK_WEBHOOK_URL,
        })
        visible_prices = {k: v for k, v in prices.items() if not k.startswith("_")}
        fetch_errors = prices.get("_errors", [])
        if not visible_prices:
            return {
                "status": "error",
                "message": "所有行情数据获取失败，保留原有行情",
                "errors": fetch_errors,
            }
        result = {"status": "ok", "prices": visible_prices}
        if fetch_errors:
            result["errors"] = fetch_errors
        return result
    except Exception as e:
        print(f"❌ 价格抓取失败: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        _price_fetch_lock.release()


def _sync_focus_to_announcement_watchlist() -> int:
    """把 company_watchlist 的重点公司自动纳入公告追踪（单向增加，不自动移除）。"""
    existing = {item["code"] for item in database.get_announcement_watchlist()}
    added = 0
    for company in database.get_company_watchlist():
        if not company.get("is_focus") or company["code"] in existing:
            continue
        source = "hkex" if company["market"] == "HK" else "cninfo"
        if database.add_announcement_stock(company["code"], company["name"], source):
            added += 1
    if added:
        print(f"📌 已将 {added} 家重点公司自动加入公告追踪")
    return added


def do_fetch_announcements():
    """执行一次公告抓取"""
    global _last_announcement_fetch_time
    if not _announcement_fetch_lock.acquire(blocking=False):
        print("⏳ 上一次公告抓取尚未完成，跳过")
        return {"status": "skipped", "reason": "already running"}

    try:
        _sync_focus_to_announcement_watchlist()
        watchlist = database.get_announcement_watchlist()
        rows, fetch_errors = announcements.fetch_for_watchlist(
            watchlist,
            days_back=config.ANNOUNCEMENT_LOOKBACK_DAYS,
            page_size=config.ANNOUNCEMENT_FETCH_PAGE_SIZE,
        )
        new_rows = database.save_announcements(rows)
        if fetch_errors and len(fetch_errors) == len(watchlist):
            return {
                "status": "error",
                "message": "所有关注公司公告抓取失败，未更新最近抓取时间",
                "errors": fetch_errors,
            }
        _last_announcement_fetch_time = datetime.now().isoformat()
        result = {
            "status": "ok",
            "total_fetched": len(rows),
            "new_announcements": len(new_rows),
            "lookback_days": config.ANNOUNCEMENT_LOOKBACK_DAYS,
            "time": _last_announcement_fetch_time,
        }
        if fetch_errors:
            result["errors"] = fetch_errors
        return result
    except Exception as e:
        print(f"❌ 公告抓取失败: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        _announcement_fetch_lock.release()


@app.route("/api/prices")
def api_prices():
    """返回最新一次各品种价格"""
    rows = database.get_latest_prices()
    return jsonify({"prices": rows})


@app.route("/api/prices/history")
def api_prices_history():
    symbol = request.args.get("symbol", "")
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except (ValueError, TypeError):
        limit = 30
    rows = database.get_price_history(symbol, limit)
    return jsonify({"history": rows})


@app.route("/api/prices/refresh", methods=["POST"])
def api_prices_refresh():
    result = do_fetch_prices()
    return jsonify(result)


# ==================== 公告追踪 ====================

@app.route("/announcements")
def announcements_page():
    return render_template("announcements.html")


@app.route("/api/announcements")
def api_announcements():
    stock_code = request.args.get("stock_code", "").strip().upper() or None
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (ValueError, TypeError):
        limit = 100
    rows = database.get_recent_announcements(limit, stock_code)
    return jsonify({
        "announcements": rows,
        "summary": database.get_announcements_summary(),
        "watchlist": database.get_announcement_watchlist(),
        "last_fetch": _last_announcement_fetch_time or database.get_latest_announcement_fetch_time(),
    })


@app.route("/api/announcements/refresh", methods=["POST"])
def api_announcements_refresh():
    result = do_fetch_announcements()
    return jsonify(result)


@app.route("/api/announcement-stocks", methods=["GET"])
def api_announcement_stocks():
    return jsonify({"stocks": database.get_announcement_watchlist()})


@app.route("/api/announcement-stocks", methods=["POST"])
def api_add_announcement_stock():
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    code = str(data.get("code", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    source = str(data.get("source", "")).strip().lower()
    if not code or not name or not source:
        return jsonify({"status": "error", "message": "代码、名称、来源不能为空"}), 400
    if not _ANNOUNCEMENT_CODE_RE.fullmatch(code):
        return jsonify({"status": "error", "message": "代码格式应为 02400.HK 或 601919.SH"}), 400
    if source not in {"hkex", "cninfo"}:
        return jsonify({"status": "error", "message": "来源只能是 hkex 或 cninfo"}), 400
    if source == "hkex" and not code.endswith(".HK"):
        return jsonify({"status": "error", "message": "港股请使用 hkex 来源"}), 400
    if source == "cninfo" and code.endswith(".HK"):
        return jsonify({"status": "error", "message": "A 股请使用 cninfo 来源"}), 400
    keywords = data.get("keywords")
    if isinstance(keywords, str):
        keywords = [item.strip() for item in keywords.split(",") if item.strip()]
        if not keywords:
            keywords = None
    elif keywords is not None and not isinstance(keywords, list):
        return jsonify({"status": "error", "message": "keywords 必须是数组或逗号分隔字符串"}), 400

    ok = database.add_announcement_stock(
        code,
        name,
        source,
        keywords=keywords,
        org_id=str(data.get("org_id", "")).strip(),
        stock_id=str(data.get("stock_id", "")).strip(),
    )
    if ok:
        return jsonify({"status": "ok", "code": code, "name": name, "source": source})
    return jsonify({"status": "error", "message": "关注公司已存在或参数无效"}), 409


@app.route("/api/announcement-stocks/<path:code>", methods=["DELETE"])
def api_delete_announcement_stock(code):
    code = str(code or "").strip().upper()
    if not _ANNOUNCEMENT_CODE_RE.fullmatch(code):
        return jsonify({"status": "error", "message": "代码格式非法"}), 400
    database.delete_announcement_stock(code)
    return jsonify({"status": "ok"})


# ==================== 公司信号 ====================

def _signal_params() -> dict:
    return {
        "min_history": config.SIGNAL_MIN_HISTORY,
        "lowvol_ratio": config.SIGNAL_LOWVOL_RATIO,
        "consolidation_days": config.SIGNAL_CONSOLIDATION_DAYS,
        "consolidation_max_range": config.SIGNAL_CONSOLIDATION_MAX_RANGE,
        "consolidation_max_drift": config.SIGNAL_CONSOLIDATION_MAX_DRIFT,
        "consolidation_vol_ratio": config.SIGNAL_CONSOLIDATION_VOL_RATIO,
        "up_min_pct": config.SIGNAL_UP_MIN_PCT,
        "up_vol_ratio": config.SIGNAL_UP_VOL_RATIO,
        "bottom_range_days": config.SIGNAL_BOTTOM_RANGE_DAYS,
        "bottom_near_low_pct": config.SIGNAL_BOTTOM_NEAR_LOW_PCT,
        "bottom_off_high_pct": config.SIGNAL_BOTTOM_OFF_HIGH_PCT,
        "bottom_max_drop": config.SIGNAL_BOTTOM_MAX_DROP,
    }


def do_scan_signals():
    """批量抓日K线、检测量价形态；信号全部入库，只推送重点公司。"""
    global _last_signal_scan_time
    if not _signal_scan_lock.acquire(blocking=False):
        print("⏳ 上一次信号扫描尚未完成，跳过")
        return {"status": "skipped", "reason": "already running"}

    try:
        watchlist = database.get_company_watchlist()
        if not watchlist:
            return {"status": "ok", "message": "watchlist 为空，无需扫描",
                    "new_signals": 0, "pushed": 0}

        name_map = {c["code"]: c["name"] for c in watchlist}
        focus_codes = {c["code"] for c in watchlist if c.get("is_focus")}
        codes = [c["code"] for c in watchlist]

        # 历史不足 SIGNAL_MIN_HISTORY 的先预热（近一年），其余只增量补最近
        counts = database.get_kline_counts(codes)
        warmup_codes = [c for c in codes if counts.get(c, 0) < config.SIGNAL_MIN_HISTORY]
        incr_codes = [c for c in codes if counts.get(c, 0) >= config.SIGNAL_MIN_HISTORY]

        fetch_errors = []
        fetched = set()
        if warmup_codes:
            print(f"🔥 预热 {len(warmup_codes)} 家公司的历史K线...")
            warm_klines, warm_errors = signals.fetch_daily_klines(warmup_codes, period_days=260)
            fetch_errors.extend(warm_errors)
            for code, klines in warm_klines.items():
                database.upsert_klines(code, klines)
                fetched.add(code)
        if incr_codes:
            incr_klines, incr_errors = signals.fetch_daily_klines(incr_codes, period_days=12)
            fetch_errors.extend(incr_errors)
            for code, klines in incr_klines.items():
                database.upsert_klines(code, klines)
                fetched.add(code)

        if codes and not fetched:
            return {
                "status": "error",
                "message": "所有公司日K线抓取失败，未执行信号检测",
                "errors": fetch_errors,
            }

        params = _signal_params()
        all_signals = []
        skipped = []
        for code in codes:
            klines = database.get_klines(code, limit=150)
            sigs, skip_reason = signals.detect_signals(code, klines, params)
            if skip_reason:
                skipped.append(f"{name_map.get(code, code)}: {skip_reason}")
            all_signals.extend(sigs)

        new_signals = database.save_signals(all_signals)
        for s in new_signals:
            s["name"] = name_map.get(s["code"], s["code"])

        # 推送抑制窗口内重复形态；只推重点公司
        to_push = []
        for s in new_signals:
            if s["code"] not in focus_codes:
                continue
            if database.has_recent_signal(
                s["code"], s["signal_type"],
                config.SIGNAL_REPEAT_SUPPRESS_DAYS, s["date"],
            ):
                continue
            to_push.append(s)
        if to_push:
            notify_signals(to_push, {
                "wechat_webhook_url":   config.WECHAT_WEBHOOK_URL,
                "feishu_webhook_url":   config.FEISHU_WEBHOOK_URL,
                "dingtalk_webhook_url": config.DINGTALK_WEBHOOK_URL,
            })

        _last_signal_scan_time = datetime.now().isoformat()
        result = {
            "status": "ok",
            "companies": len(codes),
            "fetched": len(fetched),
            "new_signals": len(new_signals),
            "pushed": len(to_push),
            "time": _last_signal_scan_time,
        }
        if fetch_errors:
            result["errors"] = fetch_errors[:20]
        if skipped:
            result["skipped"] = skipped[:20]
        return result
    except Exception as e:
        print(f"❌ 信号扫描失败: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        _signal_scan_lock.release()


@app.route("/companies")
def companies_page():
    return render_template("companies.html")


@app.route("/api/companies")
def api_companies():
    watchlist = database.get_company_watchlist()
    latest = database.get_latest_klines()
    for item in watchlist:
        k = latest.get(item["code"]) or {}
        item["last_close"] = k.get("close")
        item["last_change_pct"] = k.get("change_pct")
        item["last_date"] = k.get("date")
    return jsonify({
        "companies": watchlist,
        "last_scan": _last_signal_scan_time,
    })


@app.route("/api/company-stocks", methods=["GET"])
def api_company_stocks():
    return jsonify({"stocks": database.get_company_watchlist()})


@app.route("/api/company-stocks", methods=["POST"])
def api_add_company_stock():
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    code = str(data.get("code", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    market = str(data.get("market", "")).strip().upper()
    if not code or not name or not market:
        return jsonify({"status": "error", "message": "代码、名称、市场不能为空"}), 400
    if not _ANNOUNCEMENT_CODE_RE.fullmatch(code):
        return jsonify({"status": "error", "message": "代码格式应为 02400.HK 或 601919.SH"}), 400
    if market not in {"A", "HK"}:
        return jsonify({"status": "error", "message": "市场只能是 A 或 HK"}), 400
    if (market == "HK") != code.endswith(".HK"):
        return jsonify({"status": "error", "message": "市场与代码后缀不匹配"}), 400
    ok = database.add_company_stock(code, name, market, bool(data.get("is_focus")))
    if ok:
        return jsonify({"status": "ok", "code": code, "name": name})
    return jsonify({"status": "error", "message": "公司已存在或参数无效"}), 409


@app.route("/api/company-stocks/import", methods=["POST"])
def api_import_company_stocks():
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"status": "error", "message": "items 必须是非空数组"}), 400
    added, skipped, invalid = 0, 0, []
    for item in items:
        if not isinstance(item, dict):
            invalid.append(str(item))
            continue
        code = str(item.get("code", "")).strip().upper()
        name = str(item.get("name", "")).strip()
        market = str(item.get("market", "")).strip().upper()
        if (not code or not name or market not in {"A", "HK"}
                or not _ANNOUNCEMENT_CODE_RE.fullmatch(code)
                or (market == "HK") != code.endswith(".HK")):
            invalid.append(f"{code or name or item}")
            continue
        if database.add_company_stock(code, name, market, bool(item.get("is_focus"))):
            added += 1
        else:
            skipped += 1
    return jsonify({"status": "ok", "added": added, "skipped": skipped, "invalid": invalid})


@app.route("/api/company-stocks/<path:code>", methods=["DELETE"])
def api_delete_company_stock(code):
    code = str(code or "").strip().upper()
    if not _ANNOUNCEMENT_CODE_RE.fullmatch(code):
        return jsonify({"status": "error", "message": "代码格式非法"}), 400
    database.delete_company_stock(code)
    return jsonify({"status": "ok"})


@app.route("/api/company-stocks/<path:code>/focus", methods=["PUT"])
def api_set_company_focus(code):
    code = str(code or "").strip().upper()
    if not _ANNOUNCEMENT_CODE_RE.fullmatch(code):
        return jsonify({"status": "error", "message": "代码格式非法"}), 400
    data = _get_json_payload()
    if data is None:
        return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
    if not database.set_company_focus(code, bool(data.get("is_focus"))):
        return jsonify({"status": "error", "message": "公司不存在"}), 404
    return jsonify({"status": "ok"})


@app.route("/api/signals")
def api_signals():
    date = request.args.get("date", "").strip() or None
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 500))
    except (ValueError, TypeError):
        limit = 200
    rows = database.get_signals(date, limit)
    latest = database.get_latest_klines()
    for row in rows:
        k = latest.get(row["code"]) or {}
        row["last_close"] = k.get("close")
        row["last_change_pct"] = k.get("change_pct")
        row["last_date"] = k.get("date")
    return jsonify({
        "signals": rows,
        "dates": database.get_signal_dates(),
        "labels": signals.SIGNAL_LABELS,
        "last_scan": _last_signal_scan_time,
    })


@app.route("/api/signals/refresh", methods=["POST"])
def api_signals_refresh():
    result = do_scan_signals()
    return jsonify(result)


# ==================== 启动 ====================

if __name__ == "__main__":
    database.init_db()

    # 仅在主进程（非 reloader 子进程）中执行首次抓取和调度器
    # WERKZEUG_RUN_MAIN 环境变量在 reloader 子进程中被设为 "true"
    is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

    if not is_reloader_child:
        start_initial_fetch_background()
        start_scheduler(lambda: do_fetch(wait_timeout=0), config.FETCH_INTERVAL_MINUTES)
        start_price_scheduler(do_fetch_prices,
                              hour=config.PRICE_REPORT_HOUR,
                              minute=config.PRICE_REPORT_MINUTE)
        start_announcement_scheduler(
            do_fetch_announcements,
            config.ANNOUNCEMENT_FETCH_INTERVAL_MINUTES,
        )
        start_signal_scheduler(
            do_scan_signals,
            hour=config.SIGNAL_REPORT_HOUR,
            minute=config.SIGNAL_REPORT_MINUTE,
        )
    else:
        print("🔁 Reloader 子进程已启动")

    # 启动 Web 服务；debug 需显式开启，避免误暴露调试器
    print(f"\n🌐 看板地址: http://{config.WEB_HOST}:{config.WEB_PORT}/")
    app.run(
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        debug=config.DEBUG,
        reloader_type="stat" if config.DEBUG else "auto",
    )
