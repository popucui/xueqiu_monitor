"""
推送通知模块
支持：控制台输出、企业微信、飞书、钉钉 Webhook
"""
import json
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
        title = p.get("title") or "(无标题)"
        text = (p.get("text", "") or "")[:100]
        lines.append(f"**{name}** · {t}")
        lines.append(f"> {title}")
        if text and text != title:
            lines.append(f"> {text}")
        lines.append("")
    return "\n".join(lines)


def _notify_wechat(posts, url):
    md = _build_markdown(posts)
    try:
        requests.post(url, json={"msgtype": "markdown", "markdown": {"content": md}}, timeout=5)
    except Exception as e:
        print(f"  ⚠️ 企业微信推送失败: {e}")


def _notify_feishu(posts, url):
    md = _build_markdown(posts)
    try:
        requests.post(url, json={"msg_type": "interactive", "card": {
            "header": {"title": {"tag": "plain_text", "content": f"🔔 雪球新动态 ({len(posts)} 条)"}},
            "elements": [{"tag": "markdown", "content": md}]
        }}, timeout=5)
    except Exception as e:
        print(f"  ⚠️ 飞书推送失败: {e}")


def _notify_dingtalk(posts, url):
    md = _build_markdown(posts)
    try:
        requests.post(url, json={"msgtype": "markdown", "markdown": {"title": "雪球新动态", "text": md}}, timeout=5)
    except Exception as e:
        print(f"  ⚠️ 钉钉推送失败: {e}")
