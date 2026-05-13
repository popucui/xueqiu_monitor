"""
定时任务调度器
"""
import atexit
from apscheduler.schedulers.background import BackgroundScheduler

_scheduler = None


def _get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.running:
        _scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Shanghai")
        _scheduler.start()
        atexit.register(stop_scheduler)
    return _scheduler


def start_scheduler(fetch_func, interval_minutes: int = 60):
    """启动雪球抓取后台定时任务（间隔式）"""
    sched = _get_scheduler()
    sched.add_job(
        fetch_func,
        "interval",
        minutes=interval_minutes,
        id="xueqiu_fetch",
        replace_existing=True,
        max_instances=1,
    )
    print(f"⏰ 雪球抓取任务已启动，每 {interval_minutes} 分钟执行一次")


def start_price_scheduler(price_func, hour: int = 8, minute: int = 30):
    """启动商品价格日报任务（工作日 HH:MM CST）"""
    sched = _get_scheduler()
    sched.add_job(
        price_func,
        "cron",
        day_of_week="mon-fri",
        hour=hour,
        minute=minute,
        id="price_daily",
        replace_existing=True,
        max_instances=1,
    )
    print(f"⏰ 行情日报任务已启动，每个工作日 {hour:02d}:{minute:02d} CST 推送")


def start_announcement_scheduler(fetch_func, interval_minutes: int = 60):
    """启动公告追踪后台定时任务（间隔式）"""
    sched = _get_scheduler()
    sched.add_job(
        fetch_func,
        "interval",
        minutes=interval_minutes,
        id="announcement_fetch",
        replace_existing=True,
        max_instances=1,
    )
    print(f"⏰ 公告追踪任务已启动，每 {interval_minutes} 分钟执行一次")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown()
        _scheduler = None
