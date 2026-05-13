"""
数据存储模块 — SQLite
"""
import sqlite3
import os
from datetime import datetime

import config

_DB_PATH = os.path.join(os.path.dirname(__file__), config.DB_PATH)


def _get_db_path():
    return _DB_PATH


def get_conn():
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
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
        """)
        _ensure_authors_sort_order(conn)
        _ensure_default_announcement_watchlist(conn)
        conn.commit()


def _ensure_default_announcement_watchlist(conn):
    rows = conn.execute(
        "SELECT COUNT(*) AS total FROM announcement_watchlist"
    ).fetchone()
    if int(rows["total"] or 0) > 0:
        return

    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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
                    matched_keywords, fetched_at, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, ann_id) DO UPDATE SET
                       stock_code = excluded.stock_code,
                       stock_name = excluded.stock_name,
                       title = excluded.title,
                       published_at = excluded.published_at,
                       url = excluded.url,
                       matched_keywords = excluded.matched_keywords,
                       fetched_at = excluded.fetched_at,
                       first_seen_at = COALESCE(announcements.first_seen_at, excluded.first_seen_at)""",
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
