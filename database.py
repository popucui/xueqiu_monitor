"""
数据存储模块 — SQLite
"""
import sqlite3
import os

import config

_DB_PATH = os.path.join(os.path.dirname(__file__), config.DB_PATH)


def _get_db_path():
    return _DB_PATH


def get_conn():
    conn = sqlite3.connect(_get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class _ConnCtx:
    """数据库连接上下文管理器，防止连接泄漏"""
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = get_conn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()
        return False


def _ensure_authors_sort_order(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(authors)").fetchall()
    }
    if "sort_order" not in columns:
        conn.execute("ALTER TABLE authors ADD COLUMN sort_order INTEGER")

    rows = conn.execute(
        "SELECT user_id, sort_order FROM authors ORDER BY added_at DESC, user_id"
    ).fetchall()
    if not any(row["sort_order"] is None for row in rows):
        return

    if all(row["sort_order"] is None for row in rows):
        for idx, row in enumerate(rows):
            conn.execute(
                "UPDATE authors SET sort_order = ? WHERE user_id = ?",
                (idx, row["user_id"]),
            )
        return

    next_order_row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM authors"
    ).fetchone()
    next_order = int(next_order_row["next_order"])
    for row in rows:
        if row["sort_order"] is not None:
            continue
        conn.execute(
            "UPDATE authors SET sort_order = ? WHERE user_id = ?",
            (next_order, row["user_id"]),
        )
        next_order += 1


def init_db():
    with _ConnCtx() as conn:
        # WAL 模式只需设置一次（持久化），放在初始化阶段
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commodity_prices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                name        TEXT DEFAULT '',
                unit        TEXT DEFAULT '',
                price       REAL,
                prev_close  REAL,
                change_pct  REAL,
                fetched_at  TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_prices_symbol ON commodity_prices(symbol);
            CREATE INDEX IF NOT EXISTS idx_prices_time   ON commodity_prices(fetched_at DESC);

            CREATE TABLE IF NOT EXISTS posts (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                user_name     TEXT DEFAULT '',
                title         TEXT DEFAULT '',
                text          TEXT DEFAULT '',
                created_at    INTEGER DEFAULT 0,
                reply_count   INTEGER DEFAULT 0,
                retweet_count INTEGER DEFAULT 0,
                like_count    INTEGER DEFAULT 0,
                source        TEXT DEFAULT '',
                target        TEXT DEFAULT '',
                fetched_at    TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
            CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(created_at DESC);

            CREATE TABLE IF NOT EXISTS post_notification_outbox (
                post_id     TEXT NOT NULL,
                channel     TEXT NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT DEFAULT '',
                created_at  TEXT DEFAULT '',
                PRIMARY KEY (post_id, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_post_notification_outbox_channel
                ON post_notification_outbox(channel, created_at);

            CREATE TABLE IF NOT EXISTS authors (
                user_id   TEXT PRIMARY KEY,
                name      TEXT NOT NULL DEFAULT '',
                added_at  TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS announcement_watchlist (
                code       TEXT PRIMARY KEY,
                name       TEXT NOT NULL DEFAULT '',
                source     TEXT NOT NULL DEFAULT '',
                org_id     TEXT DEFAULT '',
                stock_id   TEXT DEFAULT '',
                keywords   TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                added_at   TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS announcements (
                source           TEXT NOT NULL,
                ann_id           TEXT NOT NULL,
                stock_code       TEXT NOT NULL,
                stock_name       TEXT NOT NULL DEFAULT '',
                title            TEXT NOT NULL DEFAULT '',
                published_at     TEXT DEFAULT '',
                url              TEXT DEFAULT '',
                matched_keywords TEXT DEFAULT '',
                fetched_at       TEXT DEFAULT '',
                first_seen_at    TEXT DEFAULT '',
                PRIMARY KEY (source, ann_id)
            );
            CREATE INDEX IF NOT EXISTS idx_announcements_stock ON announcements(stock_code);
            CREATE INDEX IF NOT EXISTS idx_announcements_time ON announcements(published_at DESC);

            CREATE TABLE IF NOT EXISTS company_watchlist (
                code       TEXT PRIMARY KEY,
                name       TEXT NOT NULL DEFAULT '',
                market     TEXT NOT NULL DEFAULT '',
                is_focus   INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                added_at   TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS daily_klines (
                code       TEXT NOT NULL,
                date       TEXT NOT NULL,
                open       REAL,
                high       REAL,
                low        REAL,
                close      REAL,
                volume     REAL,
                fetched_at TEXT DEFAULT '',
                PRIMARY KEY (code, date)
            );
            CREATE INDEX IF NOT EXISTS idx_klines_date ON daily_klines(date);

            CREATE TABLE IF NOT EXISTS daily_signals (
                code        TEXT NOT NULL,
                date        TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                created_at  TEXT DEFAULT '',
                PRIMARY KEY (code, date, signal_type)
            );
            CREATE INDEX IF NOT EXISTS idx_signals_date ON daily_signals(date DESC);

            CREATE TABLE IF NOT EXISTS signal_notification_outbox (
                code        TEXT NOT NULL,
                date        TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                channel     TEXT NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT DEFAULT '',
                created_at  TEXT DEFAULT '',
                PRIMARY KEY (code, date, signal_type, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_signal_notification_outbox_channel
                ON signal_notification_outbox(channel, created_at);
        """)
        _ensure_authors_sort_order(conn)
        _ensure_default_announcement_watchlist(conn)
        _ensure_announcements_sentiment(conn)
        conn.commit()


def _ensure_announcements_sentiment(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(announcements)").fetchall()
    }
    if "sentiment" not in columns:
        conn.execute("ALTER TABLE announcements ADD COLUMN sentiment TEXT DEFAULT ''")


def _ensure_default_announcement_watchlist(conn):
    rows = conn.execute(
        "SELECT COUNT(*) AS total FROM announcement_watchlist"
    ).fetchone()
    if int(rows["total"] or 0) > 0:
        return

    now = config.now().isoformat()
    defaults = [
        (
            "02400.HK",
            "心动公司",
            "hkex",
            "",
            "1000016859",
            "Annual Results,Interim Results,Quarterly,Monthly Return,Results Announcement,年报,中期,季度,月报",
            0,
            now,
        ),
        (
            "601919.SH",
            "中远海控",
            "cninfo",
            "9900003201",
            "",
            "年度报告,半年度报告,季度报告,业绩,主要经营数据,经营简报,月报,证券变动月报表",
            1,
            now,
        ),
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO announcement_watchlist
           (code, name, source, org_id, stock_id, keywords, sort_order, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        defaults,
    )


def save_posts(posts: list) -> list:
    """保存帖子列表，返回新增的帖子"""
    if not posts:
        return []
    new_posts = []
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        for p in posts:
            pid = str(p.get("id", ""))
            if not pid:
                continue
            try:
                conn.execute(
                    """INSERT INTO posts (id, user_id, user_name, title, text,
                       created_at, reply_count, retweet_count, like_count, source, target, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid,
                        str(p.get("user_id", "")),
                        p.get("user_name", ""),
                        p.get("title", ""),
                        p.get("text", ""),
                        p.get("created_at", 0),
                        p.get("reply_count", 0),
                        p.get("retweet_count", 0),
                        p.get("like_count", 0),
                        p.get("source", ""),
                        p.get("target", ""),
                        now,
                    ),
                )
                new_posts.append(p)
            except sqlite3.IntegrityError:
                # 已存在，跳过
                pass
            except Exception as e:
                # 非主键冲突异常（如字段类型错误），记录并继续处理下一条
                print(f"⚠️ 保存帖子 {pid} 失败: {e}")
        conn.commit()
    return new_posts


def enqueue_post_notifications(posts: list, channels: list[str]) -> None:
    """将新帖子加入各通知渠道的持久化待发送队列。"""
    post_ids = [str(post.get("id") or "") for post in posts]
    post_ids = [post_id for post_id in post_ids if post_id]
    channel_names = sorted({str(channel).strip() for channel in channels if str(channel).strip()})
    if not post_ids or not channel_names:
        return

    now = config.now().isoformat()
    rows = [
        (post_id, channel, now)
        for post_id in post_ids
        for channel in channel_names
    ]
    with _ConnCtx() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO post_notification_outbox
               (post_id, channel, created_at)
               VALUES (?, ?, ?)""",
            rows,
        )
        conn.commit()


def get_pending_post_notifications(channel: str, limit: int = 500) -> list:
    """返回指定渠道尚未发送成功的帖子。"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT p.*, o.attempts,
                      o.last_error AS notification_last_error
               FROM post_notification_outbox o
               INNER JOIN posts p ON p.id = o.post_id
               WHERE o.channel = ?
               ORDER BY o.created_at ASC, p.created_at ASC, p.id ASC
               LIMIT ?""",
            (str(channel), max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def complete_post_notifications(channel: str, post_ids: list[str]) -> None:
    """删除已成功发送的 outbox 项。"""
    ids = [str(post_id) for post_id in post_ids if str(post_id)]
    if not ids:
        return
    with _ConnCtx() as conn:
        conn.executemany(
            "DELETE FROM post_notification_outbox WHERE post_id = ? AND channel = ?",
            [(post_id, str(channel)) for post_id in ids],
        )
        conn.commit()


def fail_post_notifications(channel: str, post_ids: list[str], error: str,
                            max_attempts: int = None) -> list[str]:
    """记录发送失败，保留 outbox 项供后续抓取重试。

    传入 ``max_attempts`` 时，重试次数达到上限的项（含此前积压的旧项）
    会被移出队列，返回这些 post_id 供调用方记录日志。
    """
    ids = [str(post_id) for post_id in post_ids if str(post_id)]
    if not ids:
        return []
    with _ConnCtx() as conn:
        conn.executemany(
            """UPDATE post_notification_outbox
               SET attempts = attempts + 1, last_error = ?
               WHERE post_id = ? AND channel = ?""",
            [(str(error)[:1000], post_id, str(channel)) for post_id in ids],
        )
        expired_ids = []
        if max_attempts is not None:
            rows = conn.execute(
                """SELECT post_id FROM post_notification_outbox
                   WHERE channel = ? AND attempts >= ?""",
                (str(channel), int(max_attempts)),
            ).fetchall()
            expired_ids = [row["post_id"] for row in rows]
            if expired_ids:
                conn.executemany(
                    "DELETE FROM post_notification_outbox WHERE post_id = ? AND channel = ?",
                    [(post_id, str(channel)) for post_id in expired_ids],
                )
        conn.commit()
    return expired_ids


def _signal_keys(signals: list) -> list[tuple[str, str, str]]:
    keys = []
    for signal in signals:
        if isinstance(signal, (tuple, list)) and len(signal) >= 3:
            code, date, signal_type = signal[0], signal[1], signal[2]
        else:
            code = str(signal.get("code") or "").strip().upper()
            date = str(signal.get("date") or "").strip()
            signal_type = str(signal.get("signal_type") or "").strip()
        if code and date and signal_type:
            keys.append((code, date, signal_type))
    return keys


def enqueue_signal_notifications(signals: list, channels: list[str]) -> None:
    """将待推送的重点公司信号加入各渠道 outbox。"""
    keys = _signal_keys(signals)
    channel_names = sorted({str(channel).strip() for channel in channels if str(channel).strip()})
    if not keys or not channel_names:
        return

    now = config.now().isoformat()
    rows = [
        (code, date, signal_type, channel, now)
        for code, date, signal_type in keys
        for channel in channel_names
    ]
    with _ConnCtx() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO signal_notification_outbox
               (code, date, signal_type, channel, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()


def get_pending_signal_notifications(channel: str, limit: int = 500) -> list:
    """返回指定渠道尚未发送成功的信号（带公司名称）。"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT s.*, w.name, w.is_focus,
                      o.attempts,
                      o.last_error AS notification_last_error
               FROM signal_notification_outbox o
               INNER JOIN daily_signals s
                 ON s.code = o.code AND s.date = o.date AND s.signal_type = o.signal_type
               LEFT JOIN company_watchlist w ON w.code = s.code
               WHERE o.channel = ?
               ORDER BY o.created_at ASC, s.date ASC, s.code ASC, s.signal_type ASC
               LIMIT ?""",
            (str(channel), max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def complete_signal_notifications(channel: str, signals: list) -> None:
    """删除已成功发送的信号 outbox 项。"""
    keys = _signal_keys(signals)
    if not keys:
        return
    with _ConnCtx() as conn:
        conn.executemany(
            """DELETE FROM signal_notification_outbox
               WHERE code = ? AND date = ? AND signal_type = ? AND channel = ?""",
            [(code, date, signal_type, str(channel)) for code, date, signal_type in keys],
        )
        conn.commit()


def fail_signal_notifications(channel: str, signals: list, error: str,
                              max_attempts: int = None) -> list[tuple[str, str, str]]:
    """记录信号发送失败；达到上限的项移出队列。"""
    keys = _signal_keys(signals)
    if not keys:
        return []
    with _ConnCtx() as conn:
        conn.executemany(
            """UPDATE signal_notification_outbox
               SET attempts = attempts + 1, last_error = ?
               WHERE code = ? AND date = ? AND signal_type = ? AND channel = ?""",
            [(str(error)[:1000], code, date, signal_type, str(channel))
             for code, date, signal_type in keys],
        )
        expired = []
        if max_attempts is not None:
            rows = conn.execute(
                """SELECT code, date, signal_type FROM signal_notification_outbox
                   WHERE channel = ? AND attempts >= ?""",
                (str(channel), int(max_attempts)),
            ).fetchall()
            expired = [(row["code"], row["date"], row["signal_type"]) for row in rows]
            if expired:
                conn.executemany(
                    """DELETE FROM signal_notification_outbox
                       WHERE code = ? AND date = ? AND signal_type = ? AND channel = ?""",
                    [(code, date, signal_type, str(channel))
                     for code, date, signal_type in expired],
                )
        conn.commit()
    return expired


def get_recent_posts(limit: int = 50, author_id: str = None, offset: int = 0) -> list:
    with _ConnCtx() as conn:
        if author_id:
            rows = conn.execute(
                """SELECT * FROM posts
                   WHERE user_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT ? OFFSET ?""",
                (author_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM posts
                   ORDER BY created_at DESC, id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def count_posts(author_id: str = None) -> int:
    """统计可查询帖子总数，供前端分页展示进度使用"""
    with _ConnCtx() as conn:
        if author_id:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM posts WHERE user_id = ?",
                (author_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS total FROM posts").fetchone()
    return int(row["total"] or 0)


def get_authors_summary() -> list:
    """获取每个作者的帖子统计（包含无帖子的作者）"""
    with _ConnCtx() as conn:
        rows = conn.execute("""
            SELECT a.user_id,
                   a.name as user_name,
                   COUNT(p.id) as total,
                   MAX(p.created_at) as latest_at
            FROM authors a
            LEFT JOIN posts p ON a.user_id = p.user_id
            GROUP BY a.user_id
            ORDER BY COALESCE(MAX(p.created_at), 0) DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ==================== 作者管理 ====================

def get_db_authors() -> list:
    """从数据库获取作者列表"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT user_id, name, added_at, sort_order
               FROM authors
               ORDER BY sort_order ASC, added_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def add_author(user_id: str, name: str) -> bool:
    """添加作者，成功返回 True，已存在返回 False"""
    if not user_id or not name:
        return False
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        order_row = conn.execute(
            "SELECT MIN(sort_order) AS min_order FROM authors"
        ).fetchone()
        min_order = order_row["min_order"]
        sort_order = 0 if min_order is None else int(min_order) - 1
        try:
            conn.execute(
                """INSERT INTO authors (user_id, name, added_at, sort_order)
                   VALUES (?, ?, ?, ?)""",
                (str(user_id), name.strip(), now, sort_order),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def delete_author(user_id: str) -> bool:
    """删除作者，返回 True"""
    if not user_id:
        return False
    with _ConnCtx() as conn:
        conn.execute("DELETE FROM authors WHERE user_id = ?", (str(user_id),))
        conn.commit()
    return True


def update_author(user_id: str, name: str) -> bool:
    """更新作者名称"""
    if not user_id or not name:
        return False
    with _ConnCtx() as conn:
        conn.execute(
            "UPDATE authors SET name = ? WHERE user_id = ?",
            (name.strip(), str(user_id)),
        )
        conn.commit()
    return True


def update_authors_order(user_ids: list) -> bool:
    """按传入 user_id 顺序持久化作者列表排序"""
    ordered_ids = [str(user_id) for user_id in user_ids if str(user_id)]
    if len(ordered_ids) != len(set(ordered_ids)):
        return False

    with _ConnCtx() as conn:
        current_rows = conn.execute(
            "SELECT user_id FROM authors ORDER BY sort_order ASC, added_at DESC"
        ).fetchall()
        current_ids = [row["user_id"] for row in current_rows]
        current_set = set(current_ids)
        if any(user_id not in current_set for user_id in ordered_ids):
            return False

        final_ids = ordered_ids + [
            user_id for user_id in current_ids if user_id not in set(ordered_ids)
        ]
        for idx, user_id in enumerate(final_ids):
            conn.execute(
                "UPDATE authors SET sort_order = ? WHERE user_id = ?",
                (idx, user_id),
            )
        conn.commit()
    return True


# ==================== 商品价格 ====================

def save_prices(prices: dict) -> None:
    """保存一次抓取的所有品种价格（不含 _errors 键）"""
    rows = [v for k, v in prices.items() if not k.startswith("_") and isinstance(v, dict)]
    if not rows:
        return
    with _ConnCtx() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO commodity_prices
                   (symbol, name, unit, price, prev_close, change_pct, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["symbol"], r["name"], r["unit"],
                 r["price"], r.get("prev_close"), r.get("change_pct"),
                 r["fetched_at"]),
            )
        conn.commit()


def get_latest_prices() -> list:
    """返回每个 symbol 最新一条价格记录"""
    with _ConnCtx() as conn:
        rows = conn.execute("""
            SELECT p.*
            FROM commodity_prices p
            INNER JOIN (
                SELECT symbol, MAX(fetched_at) AS max_at
                FROM commodity_prices
                GROUP BY symbol
            ) latest ON p.symbol = latest.symbol AND p.fetched_at = latest.max_at
            ORDER BY p.id
        """).fetchall()
    return [dict(r) for r in rows]


def get_price_history(symbol: str, limit: int = 30) -> list:
    """返回某品种最近 N 条历史价格"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            "SELECT * FROM commodity_prices WHERE symbol = ? ORDER BY fetched_at DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ==================== 公告追踪 ====================

def get_announcement_watchlist() -> list:
    """返回公告关注股票列表"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT code, name, source, org_id, stock_id, keywords, sort_order, added_at
               FROM announcement_watchlist
               ORDER BY sort_order ASC, code ASC"""
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["keywords"] = [
            keyword.strip()
            for keyword in (item.get("keywords") or "").split(",")
            if keyword.strip()
        ]
        item["org_id"] = item.get("org_id") or None
        item["stock_id"] = item.get("stock_id") or None
        result.append(item)
    return result


def _keywords_to_text(keywords) -> str:
    if isinstance(keywords, str):
        return ",".join(keyword.strip() for keyword in keywords.split(",") if keyword.strip())
    if isinstance(keywords, list):
        return ",".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
    return ""


def add_announcement_stock(
    code: str,
    name: str,
    source: str,
    keywords=None,
    org_id: str = "",
    stock_id: str = "",
) -> bool:
    """添加公告关注股票，成功返回 True，已存在返回 False"""
    code = (code or "").strip().upper()
    name = (name or "").strip()
    source = (source or "").strip().lower()
    if not code or not name or source not in {"hkex", "cninfo"}:
        return False

    if keywords is None:
        if source == "hkex":
            keywords = [
                "Annual Results", "Interim Results", "Quarterly", "Monthly Return",
                "Results Announcement", "年报", "中期", "季度", "月报",
            ]
        else:
            keywords = [
                "年度报告", "半年度报告", "季度报告", "业绩",
                "主要经营数据", "经营简报", "月报",
            ]
    keywords_text = _keywords_to_text(keywords)
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        order_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM announcement_watchlist"
        ).fetchone()
        try:
            conn.execute(
                """INSERT INTO announcement_watchlist
                   (code, name, source, org_id, stock_id, keywords, sort_order, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    name,
                    source,
                    (org_id or "").strip(),
                    (stock_id or "").strip(),
                    keywords_text,
                    int(order_row["next_order"]),
                    now,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def update_announcement_stock_ids(code: str, org_id: str = "", stock_id: str = "") -> None:
    """回写解析出的 org_id / stock_id，后续抓取免去重复解析请求"""
    if not code or not (org_id or stock_id):
        return
    with _ConnCtx() as conn:
        if org_id:
            conn.execute(
                "UPDATE announcement_watchlist SET org_id = ? WHERE code = ?",
                (str(org_id), code),
            )
        if stock_id:
            conn.execute(
                "UPDATE announcement_watchlist SET stock_id = ? WHERE code = ?",
                (str(stock_id), code),
            )
        conn.commit()


def delete_announcement_stock(code: str) -> bool:
    """删除公告关注股票。历史公告保留，返回 True。"""
    code = (code or "").strip().upper()
    if not code:
        return False
    with _ConnCtx() as conn:
        conn.execute("DELETE FROM announcement_watchlist WHERE code = ?", (code,))
        conn.commit()
    return True


def save_announcements(announcements: list) -> list:
    """保存公告列表，返回新增公告"""
    if not announcements:
        return []
    new_rows = []
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        for ann in announcements:
            source = ann.source
            ann_id = ann.ann_id
            existing = conn.execute(
                "SELECT 1 FROM announcements WHERE source = ? AND ann_id = ?",
                (source, ann_id),
            ).fetchone()
            first_seen_at = now if existing is None else None
            conn.execute(
                """INSERT INTO announcements
                   (source, ann_id, stock_code, stock_name, title, published_at, url,
                    matched_keywords, fetched_at, first_seen_at, sentiment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, ann_id) DO UPDATE SET
                       stock_code = excluded.stock_code,
                       stock_name = excluded.stock_name,
                       title = excluded.title,
                       published_at = excluded.published_at,
                       url = excluded.url,
                       matched_keywords = excluded.matched_keywords,
                       fetched_at = excluded.fetched_at,
                       first_seen_at = COALESCE(announcements.first_seen_at, excluded.first_seen_at),
                       sentiment = excluded.sentiment""",
                (
                    source,
                    ann_id,
                    ann.stock_code,
                    ann.stock_name,
                    ann.title,
                    ann.published_at,
                    ann.url,
                    ann.matched_keywords,
                    now,
                    first_seen_at,
                    getattr(ann, "sentiment", "") or "",
                ),
            )
            if existing is None:
                new_rows.append(ann)
        conn.commit()
    return new_rows


def get_recent_announcements(limit: int = 100, stock_code: str = None) -> list:
    with _ConnCtx() as conn:
        if stock_code:
            rows = conn.execute(
                """SELECT * FROM announcements
                   WHERE stock_code = ?
                   ORDER BY published_at DESC, source ASC, ann_id DESC
                   LIMIT ?""",
                (stock_code, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM announcements
                   ORDER BY published_at DESC, source ASC, ann_id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_announcements_summary() -> list:
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT stock_code,
                      MAX(stock_name) AS stock_name,
                      COUNT(*) AS total,
                      MAX(published_at) AS latest_at
               FROM announcements
               GROUP BY stock_code
               ORDER BY latest_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_latest_announcement_fetch_time() -> str:
    with _ConnCtx() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS latest_fetch FROM announcements"
        ).fetchone()
    return row["latest_fetch"] or ""


# ==================== 公司信号 ====================

def get_company_watchlist() -> list:
    """返回公司 watchlist，重点公司优先、其余按添加顺序"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT code, name, market, is_focus, sort_order, added_at
               FROM company_watchlist
               ORDER BY is_focus DESC, sort_order ASC, code ASC"""
        ).fetchall()
    return [dict(row) for row in rows]


def add_company_stock(code: str, name: str, market: str, is_focus: bool = False) -> bool:
    """添加 watchlist 公司，成功返回 True，已存在返回 False"""
    code = (code or "").strip().upper()
    name = (name or "").strip()
    market = (market or "").strip().upper()
    if not code or not name or market not in {"A", "HK"}:
        return False
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        order_row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM company_watchlist"
        ).fetchone()
        try:
            conn.execute(
                """INSERT INTO company_watchlist
                   (code, name, market, is_focus, sort_order, added_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, name, market, 1 if is_focus else 0,
                 int(order_row["next_order"]), now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def delete_company_stock(code: str) -> bool:
    """删除 watchlist 公司。历史 K 线与信号保留，返回 True。"""
    code = (code or "").strip().upper()
    if not code:
        return False
    with _ConnCtx() as conn:
        conn.execute("DELETE FROM company_watchlist WHERE code = ?", (code,))
        conn.commit()
    return True


def set_company_focus(code: str, is_focus: bool) -> bool:
    """设置/取消重点标记，公司不存在返回 False"""
    code = (code or "").strip().upper()
    if not code:
        return False
    with _ConnCtx() as conn:
        cursor = conn.execute(
            "UPDATE company_watchlist SET is_focus = ? WHERE code = ?",
            (1 if is_focus else 0, code),
        )
        conn.commit()
        return cursor.rowcount > 0


def upsert_klines(code: str, klines: list) -> int:
    """按 (code, date) upsert 日 K 线，返回写入条数。

    klines 元素为 {"date", "open", "high", "low", "close", "volume"}，
    date 为 YYYY-MM-DD 字符串。
    """
    if not code or not klines:
        return 0
    now = config.now().isoformat()
    written = 0
    with _ConnCtx() as conn:
        for k in klines:
            date = str(k.get("date") or "").strip()
            if not date:
                continue
            conn.execute(
                """INSERT INTO daily_klines
                   (code, date, open, high, low, close, volume, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code, date) DO UPDATE SET
                       open = excluded.open,
                       high = excluded.high,
                       low = excluded.low,
                       close = excluded.close,
                       volume = excluded.volume,
                       fetched_at = excluded.fetched_at""",
                (code.strip().upper(), date,
                 _round_price(k.get("open")), _round_price(k.get("high")),
                 _round_price(k.get("low")), _round_price(k.get("close")),
                 k.get("volume"), now),
            )
            written += 1
        conn.commit()
    return written


def get_klines(code: str, limit: int = 250) -> list:
    """返回某公司最近 N 根日 K 线，按日期升序（旧→新）"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT code, date, open, high, low, close, volume
               FROM daily_klines WHERE code = ?
               ORDER BY date DESC LIMIT ?""",
            ((code or "").strip().upper(), max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _round_price(value, ndigits: int = 3):
    """行情展示用：去掉 float32 残留（如 23.399999618530273 → 23.4）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, ndigits)


def get_latest_klines() -> dict:
    """返回 {code: 最新K线}，含 date/close/change_pct（相对前收盘）"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            """SELECT k.code, k.date, k.close, p.close AS prev_close
               FROM daily_klines k
               INNER JOIN (
                   SELECT code, MAX(date) AS max_date FROM daily_klines GROUP BY code
               ) latest ON k.code = latest.code AND k.date = latest.max_date
               LEFT JOIN daily_klines p
                   ON p.code = k.code
                  AND p.date = (SELECT MAX(date) FROM daily_klines
                                WHERE code = k.code AND date < k.date)"""
        ).fetchall()
    result = {}
    for row in rows:
        close, prev_close = _round_price(row["close"]), _round_price(row["prev_close"])
        change_pct = None
        if close and prev_close:
            change_pct = round((close - prev_close) / prev_close * 100, 2)
        result[row["code"]] = {
            "date": row["date"],
            "close": close,
            "change_pct": change_pct,
        }
    return result


def get_kline_counts(codes: list) -> dict:
    """返回 {code: 已存K线条数}，用于判断哪些公司需要预热"""
    wanted = [(code or "").strip().upper() for code in codes if str(code).strip()]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    with _ConnCtx() as conn:
        rows = conn.execute(
            f"SELECT code, COUNT(*) AS total FROM daily_klines "
            f"WHERE code IN ({placeholders}) GROUP BY code",
            wanted,
        ).fetchall()
    return {row["code"]: int(row["total"]) for row in rows}


def save_signals(signals: list) -> list:
    """保存信号列表，返回首次入库的信号。

    signals 元素为 {"code", "date", "signal_type", "detail"}；
    (code, date, signal_type) 冲突视为已存在，不重复计入新增。
    """
    if not signals:
        return []
    new_rows = []
    now = config.now().isoformat()
    with _ConnCtx() as conn:
        for s in signals:
            code = str(s.get("code") or "").strip().upper()
            date = str(s.get("date") or "").strip()
            signal_type = str(s.get("signal_type") or "").strip()
            if not code or not date or not signal_type:
                continue
            existing = conn.execute(
                "SELECT 1 FROM daily_signals WHERE code = ? AND date = ? AND signal_type = ?",
                (code, date, signal_type),
            ).fetchone()
            try:
                conn.execute(
                    """INSERT INTO daily_signals
                       (code, date, signal_type, detail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (code, date, signal_type, s.get("detail", ""), now),
                )
            except sqlite3.IntegrityError:
                continue
            if existing is None:
                new_rows.append(dict(s, code=code))
        conn.commit()
    return new_rows


def get_signals(date: str = None, limit: int = 200) -> list:
    """返回信号列表（带公司名称与重点标记）。

    排序：新日期在前 → 重点公司在前 → 形态优先级
    （放量上涨 → 缩量下跌·底部 → 横盘企稳 → 缩量下跌）→ 代码。
    同公司同日多形态会相邻，方便前端合成一张卡。
    """
    order_case = ("CASE s.signal_type WHEN 'high_vol_up' THEN 0 "
                  "WHEN 'low_vol_bottom' THEN 1 "
                  "WHEN 'consolidation' THEN 2 ELSE 3 END")
    with _ConnCtx() as conn:
        if date:
            rows = conn.execute(
                f"""SELECT s.*, w.name, w.is_focus
                   FROM daily_signals s
                   LEFT JOIN company_watchlist w ON w.code = s.code
                   WHERE s.date = ?
                   ORDER BY s.date DESC, w.is_focus DESC, {order_case}, s.code ASC
                   LIMIT ?""",
                (date, max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT s.*, w.name, w.is_focus
                   FROM daily_signals s
                   LEFT JOIN company_watchlist w ON w.code = s.code
                   ORDER BY s.date DESC, w.is_focus DESC, {order_case}, s.code ASC
                   LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
    return [dict(row) for row in rows]


def get_signal_dates(limit: int = 30) -> list:
    """返回最近有信号的日期列表（降序），供页面日期筛选"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_signals ORDER BY date DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [row["date"] for row in rows]


def has_recent_signal(code: str, signal_type: str, days: int, before_date: str) -> bool:
    """判断 (before_date 之前 days 天内) 是否已出现过同公司同形态信号，
    用于推送去重抑制（before_date 当天不计入）"""
    code = (code or "").strip().upper()
    if not code or days <= 0:
        return False
    with _ConnCtx() as conn:
        row = conn.execute(
            """SELECT 1 FROM daily_signals
               WHERE code = ? AND signal_type = ?
                 AND date < ? AND date >= date(?, ?)
               LIMIT 1""",
            (code, signal_type, before_date, before_date, f"-{int(days)} days"),
        ).fetchone()
    return row is not None
