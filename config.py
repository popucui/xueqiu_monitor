# 雪球作者动态监控看板 — 配置文件

import os
from dotenv import load_dotenv

load_dotenv()


# 作者列表已移至数据库管理，请通过 Web UI（⚙️ 管理作者）添加初始作者


# ==================== 雪球认证 ====================
XQ_A_TOKEN = os.getenv("XQ_A_TOKEN", "")
XQ_R_TOKEN = os.getenv("XQ_R_TOKEN", "")

# ==================== 抓取设置 ====================
FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "60"))  # 定时抓取间隔（分钟）
POST_LOOKBACK_DAYS = int(os.getenv("POST_LOOKBACK_DAYS", "7"))  # 每次抓取近 N 天动态
POST_FETCH_PAGE_SIZE = int(os.getenv("POST_FETCH_PAGE_SIZE", "20"))  # 雪球接口每页抓取条数

# ==================== 推送通知 ====================
# 企业微信 Webhook（留空则不推送，仅控制台输出）
WECHAT_WEBHOOK_URL = os.getenv("WECHAT_WEBHOOK_URL", "")
# 飞书 Webhook
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
# 钉钉 Webhook
DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL", "")

# ==================== 商品价格日报 ====================
# 工作日早盘推送时间（北京时间）
PRICE_REPORT_HOUR   = int(os.getenv("PRICE_REPORT_HOUR",   "8"))
PRICE_REPORT_MINUTE = int(os.getenv("PRICE_REPORT_MINUTE", "30"))

# ==================== 公告追踪 ====================
ANNOUNCEMENT_LOOKBACK_DAYS = int(os.getenv("ANNOUNCEMENT_LOOKBACK_DAYS", "30"))
ANNOUNCEMENT_FETCH_PAGE_SIZE = int(os.getenv("ANNOUNCEMENT_FETCH_PAGE_SIZE", "50"))
ANNOUNCEMENT_FETCH_INTERVAL_MINUTES = int(os.getenv("ANNOUNCEMENT_FETCH_INTERVAL_MINUTES", "60"))

# ==================== Web 看板 ====================
# 安全建议：默认仅监听本机，如需局域网访问可在 .env 中设置 WEB_HOST=0.0.0.0
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5001"))
DEBUG = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
DB_PATH = "xueqiu_monitor.db"
