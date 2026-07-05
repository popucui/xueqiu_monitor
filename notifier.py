"""
推送通知模块
支持：控制台输出、企业微信、飞书、钉钉 Webhook
"""
import time
import requests
from datetime import datetime


def fmt_ts(ts):
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else "?"


def notify_new_posts(posts: list, config: dict = None):
    """推送新帖子通知"""
    if not posts:
        return

    config = config or {}

    # 控制台输出（始终执行）
    _notify_console(posts)

    # Webhook 推送
    wechat_url = config.get("wechat_webhook_url", "")
    feishu_url = config.get("feishu_webhook_url", "")
    dingtalk_url = config.get("dingtalk_webhook_url", "")

    if wechat_url:
        _notify_wechat(posts, wechat_url)
    if feishu_url:
        _notify_feishu(posts, feishu_url)
    if dingtalk_url:
        _notify_dingtalk(posts, dingtalk_url)


def _notify_console(posts):
    print(f"\n🔔 发现 {len(posts)} 条新动态:")
    for p in posts:
        t = fmt_ts(p.get("created_at"))
        name = p.get("user_name", "")
        title = p.get("title") or p.get("text", "")[:50]
        print(f"  [{t}] {name}: {title}")


def _build_markdown(posts):
    lines = [f"## 🔔 雪球新动态 ({len(posts)} 条)\n"]
    for p in posts:
        t = fmt_ts(p.get("created_at"))
        name = p.get("user_name", "")
        title = (p.get("title") or "(无标题)")[:100]
        text = (p.get("text", "") or "")[:100]
        lines.append(f"**{name}** · {t}")
        lines.append(f"> {title}")
        if text and text != title:
            lines.append(f"> {text}")
        lines.append("")
    return "\n".join(lines)


# 各平台 markdown 消息体上限（UTF-8 字节，留出余量）：
# 企业微信官方上限 4096 字节，钉钉 20000 字符，飞书卡片约 30K
_WECHAT_MD_LIMIT = 3800
_FEISHU_MD_LIMIT = 20000
_DINGTALK_MD_LIMIT = 18000


def _split_posts_by_size(posts, limit_bytes):
    """按渲染后的 markdown 字节数把帖子分批，保证单条消息不超平台上限。

    新作者首次抓取时 7 天帖子全算"新增"，单条消息很容易超限被平台拒收。
    """
    batches = []
    current = []
    for p in posts:
        current.append(p)
        if len(current) > 1 and len(_build_markdown(current).encode("utf-8")) > limit_bytes:
            current.pop()
            batches.append(current)
            current = [p]
    if current:
        batches.append(current)
    return batches


def _post_with_retry(url: str, payload: dict, name: str, max_retries: int = 3):
    """带指数退避重试的 Webhook POST"""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
            # 企业微信/钉钉业务失败时返回 HTTP 200 + body 里的 errcode，
            # 飞书用 code；不检查的话这类失败是静默的
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                err = body.get("errcode") or body.get("code")
                if err:
                    raise RuntimeError(f"webhook 业务错误: {body}")
            return
        except Exception as e:
            if attempt == max_retries:
                print(f"  ⚠️ {name}推送失败（已重试 {max_retries} 次）: {e}")
            else:
                wait = 2 ** (attempt - 1)  # 1s, 2s
                print(f"  ⚠️ {name}推送失败，{wait}s 后重试（{attempt}/{max_retries}）: {e}")
                time.sleep(wait)


def _notify_wechat(posts, url):
    for batch in _split_posts_by_size(posts, _WECHAT_MD_LIMIT):
        md = _build_markdown(batch)
        _post_with_retry(url, {"msgtype": "markdown", "markdown": {"content": md}}, "企业微信")


def _notify_feishu(posts, url):
    for batch in _split_posts_by_size(posts, _FEISHU_MD_LIMIT):
        md = _build_markdown(batch)
        _post_with_retry(url, {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"🔔 雪球新动态 ({len(batch)} 条)"}},
                "elements": [{"tag": "markdown", "content": md}],
            },
        }, "飞书")


def _notify_dingtalk(posts, url):
    for batch in _split_posts_by_size(posts, _DINGTALK_MD_LIMIT):
        md = _build_markdown(batch)
        _post_with_retry(url, {"msgtype": "markdown", "markdown": {"title": "雪球新动态", "text": md}}, "钉钉")


# ==================== 商品价格推送 ====================

def _fmt_change(pct):
    if pct is None:
        return "—"
    sign = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
    return f"{sign} {abs(pct):.2f}%"


def _build_price_markdown(prices: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 🌅 早盘行情速报 · {now}\n"]
    from price_fetcher import _TICKERS
    order = list(_TICKERS.keys())
    for key in order:
        item = prices.get(key)
        if not item:
            continue
        chg = _fmt_change(item.get("change_pct"))
        lines.append(
            f"**{item['name']}** `{item['symbol']}`  "
            f"**{item['price']} {item['unit']}**  {chg}"
        )
    errors = prices.get("_errors", [])
    if errors:
        lines.append(f"\n> ⚠️ 部分数据获取失败: {'; '.join(errors)}")
    lines.append("\n> *数据来源：Yahoo Finance*")
    return "\n".join(lines)


def notify_prices(prices: dict, config: dict = None):
    """推送商品价格到配置的 Webhook"""
    config = config or {}
    md = _build_price_markdown(prices)

    # 控制台输出
    print(md)

    wechat_url   = config.get("wechat_webhook_url", "")
    feishu_url   = config.get("feishu_webhook_url", "")
    dingtalk_url = config.get("dingtalk_webhook_url", "")

    if wechat_url:
        _post_with_retry(
            wechat_url,
            {"msgtype": "markdown", "markdown": {"content": md}},
            "企业微信(价格)",
        )
    if feishu_url:
        _post_with_retry(feishu_url, {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "🌅 早盘行情速报"}},
                "elements": [{"tag": "markdown", "content": md}],
            },
        }, "飞书(价格)")
    if dingtalk_url:
        _post_with_retry(
            dingtalk_url,
            {"msgtype": "markdown", "markdown": {"title": "早盘行情速报", "text": md}},
            "钉钉(价格)",
        )
