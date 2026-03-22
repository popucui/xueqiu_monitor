"""
定时任务调度器
"""
from apscheduler.schedulers.background import BackgroundScheduler


_scheduler = None


def start_scheduler(fetch_func, interval_minutes: int = 60):
    """启动后台定时任务"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        fetch_func,
        "interval",
        minutes=interval_minutes,
        id="xueqiu_fetch",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    print(f"⏰ 定时任务已启动，每 {interval_minutes} 分钟执行一次")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown()
        _scheduler = None
