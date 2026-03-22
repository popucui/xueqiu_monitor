# 雪球作者动态监控看板 — 配置文件

import os
from dotenv import load_dotenv

load_dotenv()


# ==================== 作者列表 ====================
AUTHORS = [
    {"id": "5074386049", "name": "JackWalk"},
    {"id": "1634692981", "name": "周期小黑马"},
    {"id": "1156379534", "name": "LianDeveloper"},
    {"id": "3350642636", "name": "亲爱的阿兰"},
    {"id": "1890433959", "name": "月色沾衣"},
]


# ==================== 雪球认证 ====================
XQ_A_TOKEN = os.getenv("XQ_A_TOKEN", "")
XQ_R_TOKEN = os.getenv("XQ_R_TOKEN", "")

# ==================== 抓取设置 ====================
FETCH_INTERVAL_MINUTES = 60   # 定时抓取间隔（分钟）
POST_COUNT = 10               # 每个作者获取最近多少条

# ==================== 推送通知 ====================
# 企业微信 Webhook（留空则不推送，仅控制台输出）
WECHAT_WEBHOOK_URL = ""
# 飞书 Webhook
FEISHU_WEBHOOK_URL = ""
# 钉钉 Webhook
DINGTALK_WEBHOOK_URL = ""

# ==================== Web 看板 ====================
WEB_HOST = "0.0.0.0"
WEB_PORT = 5001
DB_PATH = "xueqiu_monitor.db"
