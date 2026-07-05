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
        errors = []
        if self._browser:
            try:
                self._browser.close()
            except Exception as e:
                errors.append(e)
        if self._pw:
            try:
                self._pw.stop()
            except Exception as e:
                errors.append(e)
        self._browser = None
        self._pw = None
        self._page = None
        if errors:
            raise RuntimeError(f"浏览器/Playwright 清理过程中出现 {len(errors)} 个异常: {errors}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    def _fetch_api(self, path: str, params: dict) -> tuple:
        """调用雪球 API。

        - 5xx 或 body 非 JSON（多半是 WAF 重挑战返回的 HTML）：抛 RuntimeError，
          上层 `do_fetch` 会重启 singleton。
        - 其它情况（含 4xx + error_code 的合法错误 JSON）：把 ``(status, data)``
          返回，由调用方根据上下文判断。例如 `user_timeline.json` 在 page>1 时
          常态化返回 400 + 10022 "请登录雪球查看更多内容"，那是分页边界不是失败。
          调用方也需要处理"4xx 但 body 里没有 error_code"这种不认识的异常形态，
          不能默认当成分页正常结束。
        """
        url = f"{path}?{urlencode(params)}"
        payload = self._page.evaluate("""
            async (url) => {
                const resp = await fetch(url, {
                    headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" }
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            }
        """, url)
        status = payload.get("status")
        body = payload.get("body") or ""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"xueqiu api {path} status={status} returned non-json (likely WAF), body[:200]={body[:200]!r}"
            ) from e
        if status is None or status >= 500:
            raise RuntimeError(f"xueqiu api {path} server error status={status} body={data}")
        return status, data

    def _status_to_post(self, user_id: str, status: dict) -> dict:
        user = status.get("user", {})

        # 优先用 text，fallback 到 description（有些帖子 text 为空但 description 有内容）
        raw_text = status.get("text", "") or status.get("description", "")
        text = clean_html(raw_text)

        # 处理转发/引用卡片
        rt = status.get("retweeted_status")
        if rt:
            rt_user = rt.get("user", {}).get("screen_name", "未知用户")
            rt_text = clean_html(rt.get("text", "") or rt.get("description", ""))
            text = f"{text}\n\n[转发] @{rt_user}: \n{rt_text}"

        # 处理 quote_cards（长文/公告转发，带卡片预览）
        quote_cards = status.get("quote_cards")
        if quote_cards and isinstance(quote_cards, list):
            for card in quote_cards:
                card_text = clean_html(card.get("description", "") or card.get("text", ""))
                card_url = card.get("url") or card.get("link", "")
                card_title = card.get("title", "")
                if card_text and card_url:
                    text = f"{text}\n\n[转发] @{card_title}: \n{card_text}\n{card_url}"
                elif card_url:
                    text = f"{text}\n\n[转发] @{card_title}: \n{card_url}"

        status_id = str(status.get("id", ""))
        like_count = status.get("like_count")
        if like_count is None:
            like_count = status.get("fav_count", 0)
        return {
            "id": status_id,
            "user_id": user_id,
            "user_name": user.get("screen_name", ""),
            "title": status.get("title", "") or "",
            "text": text,
            "created_at": status.get("created_at", 0),
            "reply_count": status.get("reply_count", 0),
            "retweet_count": status.get("retweet_count", 0),
            "like_count": like_count,
            "source": status.get("source", ""),
            "target": status.get("target", "") or f"/{user_id}/{status_id}",
        }

    def fetch_user_posts(self, user_id: str, since_ms: int = None, page_size: int = 20) -> list:
        """获取用户从 since_ms 至今的帖子，按页抓到越过时间窗口为止"""
        result = []
        seen_ids = set()
        page = 1

        while True:
            status_code, data = self._fetch_api("/v4/statuses/user_timeline.json", {
                "user_id": user_id, "page": page, "count": page_size
            })
            # 雪球对未登录/非关注用户的 user_timeline 通常只放 page=1，page>1
            # 直接返回 4xx + error_code=10022 "请登录雪球查看更多内容"。这是
            # 分页边界，不是会话失效。但 page=1 就拿到 error_code 则确实是
            # cookie 失效或 WAF 拦截，必须抛出让 singleton 重启。
            err_code = data.get("error_code")
            if err_code:
                if page == 1:
                    raise RuntimeError(
                        f"xueqiu user_timeline user={user_id} page=1 error={err_code} desc={data.get('error_description')!r}"
                    )
                break
            # 4xx 但 body 里没有已知的 error_code：这不是文档记录过的分页边界
            # 形态，可能是接口改版或限流的新错误结构。page=1 直接当失败抛出
            # 重启 singleton；page>1 保守起见仍按分页结束处理，但打印明确的
            # 异常告警而不是当成正常空分页悄悄放过。
            if status_code is not None and status_code >= 400:
                if page == 1:
                    raise RuntimeError(
                        f"xueqiu user_timeline user={user_id} page=1 status={status_code} "
                        f"无 error_code，响应异常 body={data!r}"
                    )
                print(
                    f"     ⚠️ {user_id} page={page} 返回 status={status_code} 且无 error_code，"
                    f"按分页结束处理但该响应形态异常，可能存在静默丢失"
                )
                break
            items = data.get("statuses") or data.get("list") or []
            if not items:
                break

            page_added = 0
            stop_for_time = False
            for status in items:
                status_id = str(status.get("id", ""))
                try:
                    created_at = int(status.get("created_at") or 0)
                except (TypeError, ValueError):
                    created_at = 0
                # 置顶帖 (mark=1) 不参与按时间停止判定，且超出窗口直接跳过；
                # 否则会因为返回顺序里置顶帖排第一而提前终止分页。
                is_pinned = status.get("mark") == 1
                if since_ms is not None and created_at and created_at < since_ms:
                    if is_pinned:
                        continue
                    stop_for_time = True
                    break
                if since_ms is not None and not created_at:
                    continue
                if status_id and status_id in seen_ids:
                    continue

                if status_id:
                    seen_ids.add(status_id)
                result.append(self._status_to_post(user_id, status))
                page_added += 1

            if stop_for_time or len(items) < page_size or page_added == 0:
                break
            page += 1
            self._page.wait_for_timeout(300)

        return result

    def fetch_all_authors(self, authors: list, since_ms: int = None, page_size: int = 20,
                          last_post_at: dict = None) -> tuple:
        """批量获取所有作者在时间窗口内的帖子，返回 ``(posts, errors)``。

        单个作者抓取失败只记入 ``errors``（元素为 ``{"user_id", "name",
        "error"}``）并继续下一个作者，避免一个作者出错丢掉整批已抓到的
        帖子。是否需要重启 singleton 由调用方根据 errors 数量判断。

        ``last_post_at`` 可选 ``{user_id: created_at_ms}``，用于在抓到 0 条时打印
        warning —— 抓不到不代表用户没发帖，这是静默失败的早期信号。
        """
        all_posts = []
        errors = []
        if since_ms is not None:
            since_text = datetime.fromtimestamp(since_ms / 1000).strftime("%Y-%m-%d %H:%M")
            print(f"  ⏱️ 抓取时间窗口: {since_text} 至今")
        now_ms = int(datetime.now().timestamp() * 1000)
        stale_threshold_ms = 6 * 3600 * 1000
        for author in authors:
            uid = author["id"]
            name = author.get("name", uid)
            print(f"  📖 获取 {name} ({uid}) 的动态...")
            try:
                posts = self.fetch_user_posts(uid, since_ms=since_ms, page_size=page_size)
            except Exception as e:
                print(f"     ❌ {name} 抓取失败: {e}")
                errors.append({"user_id": uid, "name": name, "error": str(e)})
                self._page.wait_for_timeout(1000)
                continue
            for p in posts:
                if not p["user_name"]:
                    p["user_name"] = name
            all_posts.extend(posts)
            print(f"     → {len(posts)} 条")
            if not posts and last_post_at:
                last_ts = last_post_at.get(uid) or 0
                if last_ts and (now_ms - last_ts) > stale_threshold_ms:
                    last_text = datetime.fromtimestamp(last_ts / 1000).strftime("%Y-%m-%d %H:%M")
                    print(f"     ⚠️ {name} 抓到 0 条，DB 最新一条停留在 {last_text}，可能静默失败")
            self._page.wait_for_timeout(1000)
        return all_posts, errors
