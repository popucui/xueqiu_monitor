"""
推送通知模块
支持：控制台输出、企业微信、飞书、钉钉 Webhook
"""
import time
import requests
from datetime import datetime

import config


def fmt_ts(ts):
    return datetime.fromtimestamp(ts / 1000, tz=config.TZ).strftime("%Y-%m-%d %H:%M") if ts else "?"


def notify_new_posts(posts: list, config: dict = None):
    """输出新帖子并立即尝试配置中的 Webhook，返回各渠道发送结果。"""
    if not posts:
        return {}

    config = config or {}

    # 控制台输出（始终执行）
    _notify_console(posts)

    # Webhook 推送
    wechat_url = config.get("wechat_webhook_url", "")
    feishu_url = config.get("feishu_webhook_url", "")
    dingtalk_url = config.get("dingtalk_webhook_url", "")

    results = {}
    if wechat_url:
        results["wechat"] = deliver_post_notifications(posts, "wechat", wechat_url)
    if feishu_url:
        results["feishu"] = deliver_post_notifications(posts, "feishu", feishu_url)
    if dingtalk_url:
        results["dingtalk"] = deliver_post_notifications(posts, "dingtalk", dingtalk_url)
    return results


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


def _split_by_rendered_size(items, limit_bytes, render_fn):
    """按渲染后的 markdown 字节数分批，保证单条消息不超平台上限。"""
    batches = []
    current = []
    for item in items:
        current.append(item)
        if len(current) > 1 and len(render_fn(current).encode("utf-8")) > limit_bytes:
            current.pop()
            batches.append(current)
            current = [item]
    if current:
        batches.append(current)
    return batches


def _split_posts_by_size(posts, limit_bytes):
    """按渲染后的 markdown 字节数把帖子分批，保证单条消息不超平台上限。

    新作者首次抓取时 7 天帖子全算"新增"，单条消息很容易超限被平台拒收。
    """
    return _split_by_rendered_size(posts, limit_bytes, _build_markdown)


def _post_with_retry(url: str, payload: dict, name: str, max_retries: int = 3):
    """带指数退避重试的 Webhook POST；成功返回 None，失败返回错误文本。"""
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
                err = body.get("errcode", body.get("code"))
                if err not in (None, "", 0, "0"):
                    raise RuntimeError(f"webhook 业务错误: {body}")
            return None
        except Exception as e:
            if attempt == max_retries:
                print(f"  ⚠️ {name}推送失败（已重试 {max_retries} 次）: {e}")
                return str(e)
            else:
                wait = 2 ** (attempt - 1)  # 1s, 2s
                print(f"  ⚠️ {name}推送失败，{wait}s 后重试（{attempt}/{max_retries}）: {e}")
                time.sleep(wait)


def _post_notification_payload(channel, batch):
    md = _build_markdown(batch)
    if channel == "wechat":
        return {"msgtype": "markdown", "markdown": {"content": md}}
    if channel == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"🔔 雪球新动态 ({len(batch)} 条)"}},
                "elements": [{"tag": "markdown", "content": md}],
            },
        }
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": "雪球新动态", "text": md}}
    raise ValueError(f"不支持的通知渠道: {channel}")


def deliver_post_notifications(posts, channel: str, url: str) -> dict:
    """发送一个渠道的帖子通知，按批返回成功 ID 与失败批次。"""
    settings = {
        "wechat": (_WECHAT_MD_LIMIT, "企业微信"),
        "feishu": (_FEISHU_MD_LIMIT, "飞书"),
        "dingtalk": (_DINGTALK_MD_LIMIT, "钉钉"),
    }
    if channel not in settings:
        raise ValueError(f"不支持的通知渠道: {channel}")

    limit_bytes, display_name = settings[channel]
    result = {"sent_ids": [], "failures": []}
    for batch in _split_posts_by_size(posts, limit_bytes):
        post_ids = [str(post.get("id") or "") for post in batch]
        post_ids = [post_id for post_id in post_ids if post_id]
        error = _post_with_retry(
            url,
            _post_notification_payload(channel, batch),
            display_name,
        )
        if error is None:
            result["sent_ids"].extend(post_ids)
        else:
            result["failures"].append({"post_ids": post_ids, "error": error})
    return result


# ==================== 商品价格推送 ====================

def _fmt_change(pct):
    if pct is None:
        return "—"
    sign = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
    return f"{sign} {abs(pct):.2f}%"


def _build_price_markdown(prices: dict) -> str:
    now = config.now().strftime("%Y-%m-%d %H:%M")
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


# ==================== 公司信号推送 ====================

_SIGNAL_EMOJI = {
    "high_vol_up": "🚀",
    "low_vol_bottom": "🎯",
    "consolidation": "🛡️",
    "low_vol_down": "🔻",
}


def _build_signal_markdown(signals: list) -> str:
    from signals import SIGNAL_LABELS
    now = config.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 📈 公司量价信号 · {now}（{len(signals)} 条，仅重点公司）\n"]
    by_type = {}
    for s in signals:
        by_type.setdefault(s.get("signal_type", ""), []).append(s)
    for signal_type in ("high_vol_up", "low_vol_bottom", "consolidation", "low_vol_down"):
        group = by_type.get(signal_type) or []
        if not group:
            continue
        label = SIGNAL_LABELS.get(signal_type, signal_type)
        lines.append(f"**{_SIGNAL_EMOJI.get(signal_type, '📌')} {label}**")
        for s in group:
            name = s.get("name") or s.get("code")
            lines.append(f"- **{name}** `{s.get('code')}` {s.get('detail', '')}")
        lines.append("")
    return "\n".join(lines)


def notify_signals(signals: list, config: dict = None):
    """输出信号到控制台；若传入 webhook URL 则立即尝试推送（测试/兼容路径）。

    生产路径走 ``deliver_signal_notifications`` + outbox，以便分批与失败重试。
    """
    if not signals:
        return {}
    cfg = config or {}
    md = _build_signal_markdown(signals)
    print(md)

    results = {}
    wechat_url = cfg.get("wechat_webhook_url", "")
    feishu_url = cfg.get("feishu_webhook_url", "")
    dingtalk_url = cfg.get("dingtalk_webhook_url", "")
    if wechat_url:
        results["wechat"] = deliver_signal_notifications(signals, "wechat", wechat_url)
    if feishu_url:
        results["feishu"] = deliver_signal_notifications(signals, "feishu", feishu_url)
    if dingtalk_url:
        results["dingtalk"] = deliver_signal_notifications(signals, "dingtalk", dingtalk_url)
    return results


def _signal_notification_payload(channel, batch):
    md = _build_signal_markdown(batch)
    if channel == "wechat":
        return {"msgtype": "markdown", "markdown": {"content": md}}
    if channel == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"📈 公司量价信号 ({len(batch)} 条)"}},
                "elements": [{"tag": "markdown", "content": md}],
            },
        }
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": "公司量价信号", "text": md}}
    raise ValueError(f"不支持的通知渠道: {channel}")


def deliver_signal_notifications(signals, channel: str, url: str) -> dict:
    """发送一个渠道的信号通知，按批返回成功项与失败批次。"""
    settings = {
        "wechat": (_WECHAT_MD_LIMIT, "企业微信(信号)"),
        "feishu": (_FEISHU_MD_LIMIT, "飞书(信号)"),
        "dingtalk": (_DINGTALK_MD_LIMIT, "钉钉(信号)"),
    }
    if channel not in settings:
        raise ValueError(f"不支持的通知渠道: {channel}")

    limit_bytes, display_name = settings[channel]
    result = {"sent": [], "failures": []}
    for batch in _split_by_rendered_size(signals, limit_bytes, _build_signal_markdown):
        items = [
            {
                "code": str(s.get("code") or "").strip().upper(),
                "date": str(s.get("date") or "").strip(),
                "signal_type": str(s.get("signal_type") or "").strip(),
            }
            for s in batch
        ]
        items = [item for item in items if item["code"] and item["date"] and item["signal_type"]]
        error = _post_with_retry(
            url,
            _signal_notification_payload(channel, batch),
            display_name,
        )
        if error is None:
            result["sent"].extend(items)
        else:
            result["failures"].append({"items": items, "error": error})
    return result
