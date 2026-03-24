"""
雪球抓取核心 — Playwright 无头浏览器
"""
import re
import json
import html
from datetime import datetime
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# 常见雪球表情映射
EMOJI_MAP = {
    '[献花花]': '🌺', '[赞]': '👍', '[大笑]': '😄', '[微笑]': '🙂',
    '[牛]': '🐂', '[熊]': '🐻', '[为什么]': '❓', '[困]': '😪',
    '[笑哭]': '😂', '[加油]': '💪', '[怒]': '😡', '[流泪]': '😭',
    '[鼓掌]': '👏', '[心]': '❤️', '[满仓]': '🈵', '[空仓]': '🈳',
    '[祝涨]': '📈', '[亏大了]': '📉', '[好]': '👌', '[不赞同]': '🙅',
    '[想一下]': '🤔', '[跪了]': '🧎', '[屎]': '💩', '[难过]': '😢',
    '[摊手]': '🤷',
}

def clean_html(text):
    if not text:
        return ""
    
    # 1. 替换 emoji
    def replace_emoji(match):
        alt_text = match.group(1)
        return EMOJI_MAP.get(alt_text, alt_text)
    text = re.sub(r'<img[^>]+alt="([^"]+)"[^>]*>', replace_emoji, text)
    
    # 2. 替换 link
    def replace_link(match):
        url = match.group(1)
        if url.startswith('/'):
            url = 'https://xueqiu.com' + url
        link_text = re.sub(r'<[^>]+>', '', match.group(2))
        
        # 忽略股票、$和@用户的冗余URL，保持纯文本
        if link_text.startswith('$') and link_text.endswith('$'):
            return link_text
        if link_text.startswith('@'):
            return link_text
            
        if url in link_text or link_text in url:
            return f" {url} "
        if link_text in ['网页链接', '查看图片', '查看对话', '点击查看']:
            return f" {url} "
        return f"{link_text} {url}"
        
    text = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', replace_link, text)
    
    # 3. 换行处理
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n')
    
    # 4. 脱除其余 HTML 并转义
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


class XueqiuFetcher:
    """多作者批量抓取器，复用单个浏览器实例"""

    def __init__(self, xq_a_token: str, xq_r_token: str):
        self.xq_a_token = xq_a_token
        self.xq_r_token = xq_r_token
        self._pw = None
        self._browser = None
        self._page = None

    def start(self):
        """启动浏览器并通过 WAF 验证"""
        print("🚀 启动浏览器...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = self._browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN",
            extra_http_headers={
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
            },
        )
        ctx.add_cookies([
            {"name": "xq_a_token", "value": self.xq_a_token, "domain": ".xueqiu.com", "path": "/"},
            {"name": "xqat", "value": self.xq_a_token, "domain": ".xueqiu.com", "path": "/"},
            {"name": "xq_r_token", "value": self.xq_r_token, "domain": ".xueqiu.com", "path": "/"},
            {"name": "xq_is_login", "value": "1", "domain": ".xueqiu.com", "path": "/"},
        ])
        self._page = ctx.new_page()
        self._page.add_init_script("""
            Object.defineProperty(navigator, "webdriver", { get: () => undefined });
            Object.defineProperty(navigator, "userAgentData", {
                get: () => ({
                    brands: [
                        { brand: "Not_A Brand", version: "8" },
                        { brand: "Chromium", version: "120" },
                        { brand: "Google Chrome", version: "120" }
                    ],
                    mobile: false, platform: "Linux"
                })
            });
        """)

        print("📡 通过 WAF 验证...")
        self._page.goto("https://xueqiu.com/", wait_until="load", timeout=60000)
        self._page.wait_for_timeout(3000)
        title = self._page.title()
        print(f"  ✅ {title}")

    def stop(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._browser = None
        self._pw = None
        self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    def _fetch_api(self, path: str, params: dict) -> dict:
        url = f"{path}?{urlencode(params)}"
        try:
            return self._page.evaluate("""
                async (url) => {
                    const resp = await fetch(url, {
                        headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" }
                    });
                    return await resp.json();
                }
            """, url)
        except Exception as e:
            return {"error": str(e)}

    def fetch_user_posts(self, user_id: str, count: int = 10) -> list:
        """获取用户帖子"""
        data = self._fetch_api("/v4/statuses/user_timeline.json", {
            "user_id": user_id, "page": 1, "count": count
        })
        items = data.get("statuses") or data.get("list") or []
        result = []
        for s in items:
            user = s.get("user", {})

            # 优先用 text，fallback 到 description（有些帖子 text 为空但 description 有内容）
            raw_text = s.get("text", "") or s.get("description", "")
            text = clean_html(raw_text)
            
            # 处理转发/引用卡片
            rt = s.get("retweeted_status")
            if rt:
                rt_user = rt.get("user", {}).get("screen_name", "未知用户")
                rt_text = clean_html(rt.get("text", "") or rt.get("description", ""))
                text = f"{text}\n\n[转发] @{rt_user}: \n{rt_text}"

            # 处理 quote_cards（长文/公告转发，带卡片预览）
            quote_cards = s.get("quote_cards")
            if quote_cards and isinstance(quote_cards, list):
                for card in quote_cards:
                    card_text = clean_html(card.get("description", "") or card.get("text", ""))
                    card_url = card.get("url") or card.get("link", "")
                    card_title = card.get("title", "")
                    if card_text and card_url:
                        text = f"{text}\n\n[转发] @{card_title}: \n{card_text}\n{card_url}"
                    elif card_url:
                        text = f"{text}\n\n[转发] @{card_title}: \n{card_url}"

            result.append({
                "id": str(s.get("id", "")),
                "user_id": user_id,
                "user_name": user.get("screen_name", ""),
                "title": s.get("title", "") or "",
                "text": text,
                "created_at": s.get("created_at", 0),
                "reply_count": s.get("reply_count", 0),
                "retweet_count": s.get("retweet_count", 0),
                "like_count": s.get("like_count") or s.get("fav_count", 0),
                "source": s.get("source", ""),
                "target": s.get("target", "") or f"/{user_id}/{s.get('id')}",
            })
        return result

    def fetch_all_authors(self, authors: list, count: int = 10) -> list:
        """批量获取所有作者的帖子"""
        all_posts = []
        for author in authors:
            uid = author["id"]
            name = author.get("name", uid)
            print(f"  📖 获取 {name} ({uid}) 的动态...")
            posts = self.fetch_user_posts(uid, count)
            # 确保 user_name 有值
            for p in posts:
                if not p["user_name"]:
                    p["user_name"] = name
            all_posts.extend(posts)
            print(f"     → {len(posts)} 条")
            self._page.wait_for_timeout(1000)  # 请求间隔
        return all_posts
