"""
数据存储模块 — SQLite
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = None


def _get_db_path():
    global DB_PATH
    if DB_PATH is None:
        from config import DB_PATH as cfg_path
        DB_PATH = os.path.join(os.path.dirname(__file__), cfg_path)
    return DB_PATH


def get_conn():
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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


def init_db():
    with _ConnCtx() as conn:
        conn.executescript("""
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
                added_at  TEXT DEFAULT ''
            );
        """)


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
        conn.commit()
    return new_posts


def get_recent_posts(limit: int = 50, author_id: str = None) -> list:
    with _ConnCtx() as conn:
        if author_id:
            rows = conn.execute(
                "SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (author_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_authors_summary() -> list:
    """获取每个作者的帖子统计"""
    with _ConnCtx() as conn:
        rows = conn.execute("""
            SELECT user_id,
                   MAX(user_name) as user_name,
                   COUNT(*) as total,
                   MAX(created_at) as latest_at
            FROM posts GROUP BY user_id ORDER BY latest_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ==================== 作者管理 ====================

def get_db_authors() -> list:
    """从数据库获取作者列表"""
    with _ConnCtx() as conn:
        rows = conn.execute(
            "SELECT user_id, name, added_at FROM authors ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_author(user_id: str, name: str) -> bool:
    """添加作者，成功返回 True，已存在返回 False"""
    if not user_id or not name:
        return False
    now = datetime.now().isoformat()
    with _ConnCtx() as conn:
        try:
            conn.execute(
                "INSERT INTO authors (user_id, name, added_at) VALUES (?, ?, ?)",
                (str(user_id), name.strip(), now),
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


def seed_authors_from_config(authors_list: list):
    """首次启动时从 config.AUTHORS 初始化作者表"""
    existing = get_db_authors()
    if existing:
        return  # 已有作者，跳过
    for a in authors_list:
        add_author(a["id"], a.get("name", a["id"]))
