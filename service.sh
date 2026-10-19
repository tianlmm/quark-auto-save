#!/bin/sh
# service.sh — fnOS 下的启动外壳：带自动重启
#
# 退出码 75 由 app/updater.py 约定：内部更新完成，需要重启进程。
# 其余退出码视为真实退出，不再拉起，交给 fnOS 的 start/stop 控制。
set -u

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VAR_DIR="${TRIM_PKGVAR:-/var/apps/quark-auto-save/var}"
LOG="${VAR_DIR}/app.log"
ENV_FILE="${VAR_DIR}/env.sh"

mkdir -p "${VAR_DIR}/config" 2>/dev/null || true
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"

# 运行时环境（环境文件可覆盖）
export PORT="${PORT:-${TRIM_SERVICE_PORT:-5005}}"
export HOST="${HOST:-0.0.0.0}"
export CONFIG_PATH="${CONFIG_PATH:-${VAR_DIR}/config/quark_config.json}"
export WEBUI_AUTH="${WEBUI_AUTH:-off}"
export TZ="${TZ:-Asia/Shanghai}"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

PY="${APP_DIR}/venv/bin/python"
[ -x "${PY}" ] || PY="$(command -v python3 || echo python3)"

cd "${APP_DIR}" || exit 1

echo "[supervisor] $(date '+%F %T') 启动，python=${PY} port=${PORT} auth=${WEBUI_AUTH}" >> "${LOG}"

CHILD=""
# 收到 stop 时连同子进程一起退出，避免 python 变成孤儿继续占用端口
trap 'echo "[supervisor] $(date "+%F %T") 收到停止信号" >> "${LOG}"; [ -n "${CHILD}" ] && kill "${CHILD}" 2>/dev/null; exit 0' TERM INT HUP

while :; do
  # 用绝对路径启动，便于 pkill -f 精确定位
  "${PY}" -u "${APP_DIR}/app/run.py" &
  CHILD=$!
  wait "${CHILD}"
  rc=$?
  CHILD=""
  if [ "${rc}" -eq 75 ]; then
    echo "[supervisor] $(date '+%F %T') 内部更新完成，重启服务" >> "${LOG}"
    sleep 1
    continue
  fi
  echo "[supervisor] $(date '+%F %T') 服务退出 rc=${rc}" >> "${LOG}"
  exit "${rc}"
done
