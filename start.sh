#!/usr/bin/env bash
# ============================================================
# xueqiu_monitor 本地启动脚本
#
# 用法:
#   ./start.sh              前台启动（默认，等同 activate + python app.py）
#   ./start.sh -d           后台启动（日志: tmp/app.log, PID: tmp/app.pid）
#   ./start.sh stop         停止后台实例
#   ./start.sh restart      重启后台实例
#   ./start.sh status       查看运行状态
#   ./start.sh --help       帮助
#
# 端口/地址从 .env 的 WEB_HOST / WEB_PORT 读取，默认 127.0.0.1:5001
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
ENV_FILE="$PROJECT_DIR/.env"
PID_FILE="$PROJECT_DIR/tmp/app.pid"
LOG_FILE="$PROJECT_DIR/tmp/app.log"

# ---------- 从 .env 读配置（缺省与 config.py 一致） ----------
env_val() { # $1=变量名 $2=默认值
    local v=""
    # 去掉行尾空白和 " # ..." 行内注释（# 前无空格的视为值的一部分，与 dotenv 一致）
    [ -f "$ENV_FILE" ] && v="$(sed -n -E "s/^[[:space:]]*$1=[[:space:]]*//p" "$ENV_FILE" | tail -1 | tr -d '"' | sed -E -e 's/[[:space:]]+#.*$//' -e 's/[[:space:]]+$//')"
    echo "${v:-$2}"
}

WEB_HOST="$(env_val WEB_HOST 127.0.0.1)"
WEB_PORT="$(env_val WEB_PORT 5001)"
# status 探测用：0.0.0.0 不能直接 curl，换成 127.0.0.1
PROBE_HOST="127.0.0.1"

# ---------- 工具函数 ----------
port_busy() { # 端口是否有进程监听
    (exec 3<>"/dev/tcp/$PROBE_HOST/$WEB_PORT") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
    return 1
}

pid_alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

_collect_descendants() { # $1=pid，递归输出全部后代 PID（每行一个）
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        echo "$child"
        _collect_descendants "$child"
    done
}

ensure_venv() {
    if [ ! -x "$PYTHON" ]; then
        echo "❌ 未找到 .venv，正在创建（Python 3.12）..."
        python3 -m venv "$VENV_DIR"
    fi
    # 依赖快速体检，缺了就按 requirements.txt 补装
    if ! "$PYTHON" -c "import flask, apscheduler, requests, dotenv, yfinance" >/dev/null 2>&1; then
        echo "📦 检测到依赖缺失，正在安装 requirements.txt ..."
        "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"
    fi
}

ensure_chromium() {
    if ! ls "$HOME/.cache/ms-playwright/"chromium-* >/dev/null 2>&1; then
        echo "🌐 未检测到 Playwright Chromium，正在下载（可能需要几分钟）..."
        "$VENV_DIR/bin/playwright" install chromium || {
            echo "⚠️  Chromium 安装失败。如是系统库缺失，请执行:"
            echo "    .venv/bin/playwright install-deps chromium   （需要 sudo）"
            exit 1
        }
    fi
}

start_foreground() {
    cd "$PROJECT_DIR"
    ensure_venv
    ensure_chromium
    if port_busy; then
        echo "❌ 端口 $WEB_PORT 已被占用。"
        echo "   可能已有实例在运行: ./start.sh status"
        echo "   或在 .env 里改 WEB_PORT 换端口后重试。"
        exit 1
    fi
    echo "🚀 前台启动 xueqiu_monitor -> http://$WEB_HOST:$WEB_PORT/"
    echo "   （Ctrl+C 停止；后台模式用 ./start.sh -d）"
    exec "$PYTHON" app.py
}

start_daemon() {
    mkdir -p "$PROJECT_DIR/tmp"
    if pid_alive; then
        echo "✅ 已在后台运行 (PID $(cat "$PID_FILE"))，无需重复启动。"
        echo "   看板地址: http://$PROBE_HOST:$WEB_PORT/"
        exit 0
    fi
    if port_busy; then
        echo "❌ 端口 $WEB_PORT 已被占用（不是本脚本管理的实例）。"
        echo "   可执行 ./start.sh status 查看端口详情。"
        exit 1
    fi
    ensure_venv
    ensure_chromium
    echo "🚀 后台启动 xueqiu_monitor -> http://$PROBE_HOST:$WEB_PORT/"
    # setsid 让 app 拥有独立进程组：stop 时按组回收，即使 Chromium 已被
    # init 收养也不会误伤其他项目的 Playwright 进程
    setsid nohup "$PYTHON" app.py >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 2
    if pid_alive; then
        echo "✅ 启动成功 (PID $(cat "$PID_FILE"))"
        echo "   日志: $LOG_FILE   停止: ./start.sh stop"
    else
        echo "❌ 进程启动后立即退出，最近日志如下:"
        tail -20 "$LOG_FILE" || true
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_app() {
    if ! [ -f "$PID_FILE" ]; then
        echo "ℹ️  没有本脚本启动的后台实例 ($PID_FILE 不存在)。"
        return 0
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ℹ️  PID $pid 已不在运行，清理残留 pid 文件。"
        rm -f "$PID_FILE"
        return 0
    fi
    # Chromium 会自建 session/进程组（即使 --no-sandbox 也是如此），组杀覆盖
    # 不到它；趁进程树还完整先把全部后代 PID 记下来，杀完再补刀，既清理
    # 残留又不碰其他项目的 Playwright 进程
    local descendants=""
    descendants="$(_collect_descendants "$pid")"
    # 优先按进程组回收（setsid 启动的实例，pgid==pid）
    local pgid=""
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
        kill -TERM -- "-$pgid" 2>/dev/null || true
    else
        # 旧版脚本启动的实例没有独立进程组，退化为只杀本进程树
        pkill -TERM -P "$pid" 2>/dev/null || true
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
            kill -KILL -- "-$pgid" 2>/dev/null || true
        else
            pkill -KILL -P "$pid" 2>/dev/null || true
            kill -KILL "$pid" 2>/dev/null || true
        fi
        echo "⛔ 强制终止 (PID $pid)"
    else
        echo "🛑 已停止 (PID $pid)"
    fi
    rm -f "$PID_FILE"
    # 补刀：杀掉树内幸存的 Chromium（它自建了进程组，组杀覆盖不到）
    local survivors=()
    local child
    for child in $descendants; do
        if kill -0 "$child" 2>/dev/null; then
            survivors+=("$child")
        fi
    done
    if [ "${#survivors[@]}" -gt 0 ]; then
        kill -TERM "${survivors[@]}" 2>/dev/null || true
        sleep 1
        for child in "${survivors[@]}"; do
            kill -KILL "$child" 2>/dev/null || true
        done
        echo "🧹 已清理本实例残留 Playwright Chromium"
    fi
}

show_status() {
    echo "════════ xueqiu_monitor 状态 ════════"
    if pid_alive; then
        echo "  进程   : 运行中 (PID $(cat "$PID_FILE"))"
    else
        [ -f "$PID_FILE" ] && echo "  进程   : pid 文件存在但进程已死（可用 stop 清理）" || echo "  进程   : 未在后台运行"
    fi
    if port_busy; then
        echo "  端口   : $WEB_PORT 监听中"
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$PROBE_HOST:$WEB_PORT/" || echo '??')"
        echo "  HTTP   : $code  (http://$PROBE_HOST:$WEB_PORT/)"
    else
        echo "  端口   : $WEB_PORT 空闲"
    fi
    [ -f "$LOG_FILE" ] && echo "  日志   : $LOG_FILE (末尾 3 行: $(tail -3 "$LOG_FILE" | tr '\n' ' '))"
    echo "══════════════════════════════════════"
}

show_help() {
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------- 入口 ----------
case "${1:-}" in
    ""|-f|--foreground) start_foreground ;;
    -d|--daemon)        start_daemon ;;
    stop)               stop_app ;;
    restart)            stop_app; sleep 1; start_daemon ;;
    status)             show_status ;;
    -h|--help|help)     show_help ;;
    *) echo "未知参数: $1"; show_help; exit 1 ;;
esac
