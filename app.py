"""
雪球作者动态监控看板 — Flask Web 应用
"""
import os
import re
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request

import config
import database
from fetcher import XueqiuFetcher
from notifier import notify_new_posts
from scheduler import start_scheduler

app = Flask(__name__)

# 全局状态
_last_fetch_time = None
_fetch_lock = threading.Lock()
_USER_ID_RE = re.compile(r"^\d{1,32}$")


def _is_valid_user_id(user_id: str) -> bool:
    return bool(_USER_ID_RE.fullmatch(user_id or ""))


def _get_json_payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def do_fetch():
    """执行一次抓取任务"""
    global _last_fetch_time
    if not _fetch_lock.acquire(blocking=False):
        print("⏳ 上一次抓取尚未完成，跳过")
        return {"status": "skipped", "reason": "already running"}

    fetcher = None
    try:
        fetcher = XueqiuFetcher(config.XQ_A_TOKEN, config.XQ_R_TOKEN)
        fetcher.start()
        db_authors = database.get_db_authors()
        authors = [{"id": a["user_id"], "name": a["name"]} for a in db_authors]
        all_posts = fetcher.fetch_all_authors(authors, config.POST_COUNT)

        new_posts = database.save_posts(all_posts)
        _last_fetch_time = datetime.now().isoformat()

        if new_posts:
            notify_new_posts(new_posts, {
                "wechat_webhook_url": config.WECHAT_WEBHOOK_URL,
                "feishu_webhook_url": config.FEISHU_WEBHOOK_URL,
                "dingtalk_webhook_url": config.DINGTALK_WEBHOOK_URL,
            })

        return {
            "status": "ok",
            "total_fetched": len(all_posts),
            "new_posts": len(new_posts),
            "time": _last_fetch_time,
        }
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": "抓取失败，请查看服务端日志"}
    finally:
        if fetcher is not None:
            try:
                fetcher.stop()
            except Exception as stop_error:
                print(f"⚠️ 关闭浏览器失败: {stop_error}")
        _fetch_lock.release()


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
    posts = database.get_recent_posts(limit, author_id)
    # 转换时间戳为可读格式
    for p in posts:
        ts = p.get("created_at", 0)
        if ts:
            p["created_at_fmt"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        else:
            p["created_at_fmt"] = ""
    return jsonify({"posts": posts, "last_fetch": _last_fetch_time})


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
    return jsonify({"authors": summary, "db_authors": db_authors})


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
    result = do_fetch()
    return jsonify(result)


# ==================== 启动 ====================

if __name__ == "__main__":
    database.init_db()

    # 仅在主进程（非 reloader 子进程）中执行首次抓取和调度器
    # WERKZEUG_RUN_MAIN 环境变量在 reloader 子进程中被设为 "true"
    is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

    if not is_reloader_child:
        print("📦 首次抓取数据...")
        do_fetch()
        start_scheduler(do_fetch, config.FETCH_INTERVAL_MINUTES)
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
