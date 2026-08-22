# 雪球作者动态监控看板 — 配置文件

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# 抓取窗口、展示时间、调度器统一用北京时间，避免进程 TZ=UTC 时偏 8 小时
TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(TZ)


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
# 单条通知连续发送失败达到该次数后放弃（移出 outbox），避免永久失效的 Webhook 无限重试
NOTIFICATION_MAX_ATTEMPTS = int(os.getenv("NOTIFICATION_MAX_ATTEMPTS", "20"))

# ==================== 商品价格日报 ====================
# 工作日早盘推送时间（北京时间）
PRICE_REPORT_HOUR   = int(os.getenv("PRICE_REPORT_HOUR",   "8"))
PRICE_REPORT_MINUTE = int(os.getenv("PRICE_REPORT_MINUTE", "30"))

# ==================== 公告追踪 ====================
ANNOUNCEMENT_LOOKBACK_DAYS = int(os.getenv("ANNOUNCEMENT_LOOKBACK_DAYS", "30"))
ANNOUNCEMENT_FETCH_PAGE_SIZE = int(os.getenv("ANNOUNCEMENT_FETCH_PAGE_SIZE", "50"))
ANNOUNCEMENT_FETCH_INTERVAL_MINUTES = int(os.getenv("ANNOUNCEMENT_FETCH_INTERVAL_MINUTES", "60"))

# ==================== 公司信号 ====================
# 每个工作日收盘后扫描时刻（北京时间；A股15:00、港股16:10收盘，16:40 数据已落定）
SIGNAL_REPORT_HOUR   = int(os.getenv("SIGNAL_REPORT_HOUR",   "16"))
SIGNAL_REPORT_MINUTE = int(os.getenv("SIGNAL_REPORT_MINUTE", "40"))
# 检测基线：历史K线不足该条数则跳过
SIGNAL_MIN_HISTORY = int(os.getenv("SIGNAL_MIN_HISTORY", "25"))
# 缩量下跌：当日量 / 前20日均量 低于该比例
SIGNAL_LOWVOL_RATIO = float(os.getenv("SIGNAL_LOWVOL_RATIO", "0.6"))
# 横盘企稳：观察窗口（交易日）、单日振幅上限、区间累计涨跌上限、窗口均量/前期均量上限
SIGNAL_CONSOLIDATION_DAYS = int(os.getenv("SIGNAL_CONSOLIDATION_DAYS", "10"))
SIGNAL_CONSOLIDATION_MAX_RANGE = float(os.getenv("SIGNAL_CONSOLIDATION_MAX_RANGE", "0.03"))
SIGNAL_CONSOLIDATION_MAX_DRIFT = float(os.getenv("SIGNAL_CONSOLIDATION_MAX_DRIFT", "0.05"))
SIGNAL_CONSOLIDATION_VOL_RATIO = float(os.getenv("SIGNAL_CONSOLIDATION_VOL_RATIO", "0.8"))
# 放量上涨：当日涨幅下限与量比下限
SIGNAL_UP_MIN_PCT = float(os.getenv("SIGNAL_UP_MIN_PCT", "0.03"))
SIGNAL_UP_VOL_RATIO = float(os.getenv("SIGNAL_UP_VOL_RATIO", "2.0"))
# 缩量下跌·底部：观察窗口（交易日）、距窗口低点上限、距高点回撤下限、单日恐慌跌幅上限
SIGNAL_BOTTOM_RANGE_DAYS = int(os.getenv("SIGNAL_BOTTOM_RANGE_DAYS", "120"))
SIGNAL_BOTTOM_NEAR_LOW_PCT = float(os.getenv("SIGNAL_BOTTOM_NEAR_LOW_PCT", "0.10"))
SIGNAL_BOTTOM_OFF_HIGH_PCT = float(os.getenv("SIGNAL_BOTTOM_OFF_HIGH_PCT", "0.20"))
SIGNAL_BOTTOM_MAX_DROP = float(os.getenv("SIGNAL_BOTTOM_MAX_DROP", "0.05"))
# 同一公司同一形态 N 天内不重复推送（仍照常入库）
SIGNAL_REPEAT_SUPPRESS_DAYS = int(os.getenv("SIGNAL_REPEAT_SUPPRESS_DAYS", "3"))

# ==================== Web 看板 ====================
# 安全建议：默认仅监听本机，如需局域网访问可在 .env 中设置 WEB_HOST=0.0.0.0
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5001"))
DEBUG = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
DB_PATH = "xueqiu_monitor.db"
