#!/usr/bin/env bash
# 标定工作站一键启动：18000 能力中心 → 停推流独占相机
#                     → 18004（轨迹回放）→ 18005（统一页面与原生 2D/3D）。
#
#   ./start.sh                  # 真机
#   ./start.sh --arm left       # 向导初始选择左臂（向导里可按计划切换）
#   ./start.sh --mock           # 无硬件联调：18005 mock 相机，18004 --mock，不碰 18000 与推流
#   ./start.sh --dev            # 前端用 Vite 开发服务器（5175）代替构建产物
#
# 环境变量：PYTHON、NETWORK_INTERFACE（默认 enp86s0）、WORKSTATION_CONFIG、CAPABILITY_SH
set -u
# 各服务之间全是本机 HTTP 调用，终端里若设置了 http_proxy 会把 127.0.0.1 的请求发到代理机器，全部清掉
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="127.0.0.1,localhost" NO_PROXY="127.0.0.1,localhost"
cd "$(dirname "$0")"
ROOT="$PWD"
CALIB_ROOT="$(cd .. && pwd)"
REPLAY_DIR="$CALIB_ROOT/calibration_replay"
CONFIG="${WORKSTATION_CONFIG:-$ROOT/config/workstation.yaml}"
CAPABILITY_URL="${CAPABILITY_URL:-http://127.0.0.1:18000}"
CAPABILITY_SH="${CAPABILITY_SH:-/home/robot/yx/project/IK_replay/capability.sh}"
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"

MOCK=0; DEV=0; ARM="right"
while [ $# -gt 0 ]; do
  case "$1" in
    --mock) MOCK=1 ;;
    --dev) DEV=1 ;;
    --arm) shift; ARM="${1:-right}" ;;
    --arm=*) ARM="${1#--arm=}" ;;
    -h|--help) sed -n 2,15p "$0"; exit 0 ;;
    *) echo "[start] 未知参数 $1" >&2; exit 2 ;;
  esac
  shift
done
case "$ARM" in left|right) ;; *) echo "[start] --arm 只能是 left/right" >&2; exit 1 ;; esac

# ---- Python ----
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in "$HOME/miniconda3/envs/fastapi/bin/python" "$HOME/anaconda3/envs/fastapi/bin/python" \
           "${CONDA_PREFIX:-/nonexistent}/bin/python"; do
    [ -x "$c" ] && "$c" -c "import cv2, fastapi, numpy, uvicorn, yaml, zmq" >/dev/null 2>&1 && { PY="$c"; break; }
  done
fi
if [ -z "$PY" ] || ! "$PY" -c "import cv2, fastapi, numpy, uvicorn, yaml, zmq" >/dev/null 2>&1; then
  echo "[start] 找不到含 cv2/fastapi/numpy/uvicorn/yaml/zmq 的 Python，请设置 PYTHON=..." >&2; exit 1
fi
echo "[start] Python: $PY"

# ---- 读配置 ----
read -r DATA_ROOT PORT_WS URL_2D URL_REPLAY < <("$PY" - "$CONFIG" <<'EOF'
import sys, yaml, urllib.parse
c = yaml.safe_load(open(sys.argv[1])) or {}
s = c.get("services") or {}
def port(u, d): return urllib.parse.urlparse(u).port or d
print(c.get("data_root", "./calib_workstation_data"), 18005,
      s.get("hand_eye_2d", "http://127.0.0.1:18005"),
      s.get("replay", "http://127.0.0.1:18004"))
EOF
) || { echo "[start] 读取配置失败: $CONFIG" >&2; exit 1; }
PORT_REPLAY="${URL_REPLAY##*:}"
echo "[start] 配置 $CONFIG  数据目录 $DATA_ROOT（机器人编号在页面里输入）"

# ---- 残留进程与端口检查 ----
port_free() { ! ss -ltn 2>/dev/null | awk -v p="$1" '$4 ~ (":" p "$") {f=1} END {exit !f}'; }

process_alive() {
  [ -r "/proc/$1/stat" ] || return 1
  [ "$(awk '{print $3}' "/proc/$1/stat" 2>/dev/null)" != "Z" ]
}

stop_bounded() {
  local pid="$1" label="$2" waited=0
  process_alive "$pid" || return 0
  echo "[start] 正在停止 $label（pid $pid）…"
  kill -TERM "$pid" 2>/dev/null || true
  while process_alive "$pid" && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  if process_alive "$pid"; then
    echo "[start] $label 在5秒内未退出，强制清理 pid $pid"
    kill -KILL "$pid" 2>/dev/null || true
    waited=0
    while process_alive "$pid" && [ "$waited" -lt 20 ]; do
      sleep 0.1
      waited=$((waited + 1))
    done
  fi
  wait "$pid" 2>/dev/null || true
  ! process_alive "$pid"
}

orphaned_workstations() {
  local proc pid ppid cwd cmd
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$$" ] || continue
    ppid="$(awk '{print $4}' "$proc/stat" 2>/dev/null)" || continue
    [ "$ppid" = "1" ] || continue
    cwd="$(readlink -f "$proc/cwd" 2>/dev/null)" || continue
    [ "$cwd" = "$ROOT" ] || continue
    cmd=" $(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null) "
    if [[ "$cmd" == *"-m calib_workstation"* && "$cmd" == *"--port $PORT_WS"* ]]; then
      echo "$pid"
    fi
  done
}

orbbec_holders() {
  command -v lsusb >/dev/null 2>&1 || return 0
  command -v fuser >/dev/null 2>&1 || return 0
  lsusb -d 2bc5: 2>/dev/null | while read -r _ bus _ device _; do
    device="${device%:}"
    fuser "/dev/bus/usb/$bus/$device" 2>/dev/null || true
  done | tr ' ' '\n' | awk '/^[0-9]+$/ && !seen[$0]++'
}

STALE_WS="$(orphaned_workstations)"
if ! port_free "$PORT_WS"; then
  if [ -n "$STALE_WS" ]; then
    echo "[start] 端口 $PORT_WS 已被同项目18005进程占用（pid: $(echo "$STALE_WS" | tr '\n' ' ')），请复用当前服务或先正常结束它" >&2
  else
    echo "[start] 端口 $PORT_WS 已被占用，请先结束对应进程" >&2
  fi
  exit 1
fi
if [ -n "$STALE_WS" ]; then
  echo "[start] 发现同项目残留的18005进程: $(echo "$STALE_WS" | tr '\n' ' ')"
  for p in $STALE_WS; do
    stop_bounded "$p" "残留18005" || {
      echo "[start] 无法清理残留18005（pid $p），中止启动" >&2
      exit 1
    }
  done
fi
port_free "$PORT_WS" || { echo "[start] 清理后端口 $PORT_WS 仍被占用，中止启动" >&2; exit 1; }

if ! port_free "$PORT_REPLAY"; then
  if "$REPLAY_DIR/replay.sh" status | grep -q "运行中"; then
    echo "[start] 18004 已由 replay.sh 启动，复用（本脚本退出时不会停止它）"; REPLAY_OWNED=0
  else
    echo "[start] 端口 $PORT_REPLAY 被占用且不是 replay.sh 启动的" >&2; exit 1
  fi
else
  REPLAY_OWNED=1
fi

# ---- 18000 能力中心 ----
if [ "$MOCK" -eq 0 ]; then
  if curl -sf --max-time 2 "$CAPABILITY_URL/api/capability/registry" >/dev/null 2>&1; then
    echo "[start] 18000 能力中心可达"
  else
    echo "[start] 18000 未运行，自动拉起: $CAPABILITY_SH"
    if [ ! -f "$CAPABILITY_SH" ] || ! bash "$CAPABILITY_SH"; then
      echo "[start] 18000 拉起失败，中止" >&2; exit 1
    fi
  fi
fi

# ---- 收尾 ----
PID_WS=""; PID_FE=""; CAMERA_LOCKED=0
cleanup() {
  trap - INT TERM EXIT
  echo ""; echo "[start] 正在退出…"
  [ -n "$PID_FE" ] && stop_bounded "$PID_FE" "前端开发服务"
  [ -n "$PID_WS" ] && stop_bounded "$PID_WS" "18005工作站"
  [ "${REPLAY_OWNED:-0}" -eq 1 ] && "$REPLAY_DIR/replay.sh" stop
  [ "$CAMERA_LOCKED" -eq 1 ] && "$ROOT/scripts/camera_lock.sh" release
  exit 0
}
trap cleanup INT TERM EXIT

# ---- 独占相机 ----
if [ "$MOCK" -eq 0 ]; then
  "$ROOT/scripts/camera_lock.sh" acquire || exit 1
  CAMERA_LOCKED=1
  CAMERA_HOLDERS="$(orbbec_holders)"
  if [ -n "$CAMERA_HOLDERS" ]; then
    echo "[start] Orbbec仍被以下进程占用，未启动18005：" >&2
    ps -o pid,ppid,stat,cmd -p "$(echo "$CAMERA_HOLDERS" | paste -sd, -)" >&2 || true
    exit 1
  fi
fi

# ---- 18004 ----
if [ "${REPLAY_OWNED:-0}" -eq 1 ]; then
  echo "[start] 正在启动 18004 轨迹回放…"
  # mock 下采集仍走 HTTP 到 18005 原生 mock 引擎，跑通全链路。
  if [ "$MOCK" -eq 1 ]; then
    BASE_URL_2D="$URL_2D" BASE_URL_3D="$URL_2D/three-d" \
      CALIB_WORKSTATION_URL="$URL_2D" HAND_EYE_3D_PROJECT="$ROOT" \
      "$REPLAY_DIR/replay.sh" start --mock --capture-http
  else
    BASE_URL_2D="$URL_2D" BASE_URL_3D="$URL_2D/three-d" \
      CALIB_WORKSTATION_URL="$URL_2D" HAND_EYE_3D_PROJECT="$ROOT" \
      "$REPLAY_DIR/replay.sh" start
  fi || exit 1
fi

# ---- 前端 ----
if [ "$DEV" -eq 1 ]; then
  (cd "$ROOT/frontend" && exec npx vite --port 5175 --host 0.0.0.0) >>"$LOG_DIR/frontend-dev.log" 2>&1 &
  PID_FE=$!
elif [ ! -f "$ROOT/frontend/dist/index.html" ] || [ ! -f "$ROOT/frontend/dist/three-d-ui/index.html" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "[start] 前端未构建，正在 npm install && npm run build …"
    (cd "$ROOT/frontend" && npm install --silent && npm run build --silent) || { echo "[start] 前端构建失败" >&2; exit 1; }
  else
    echo "[start] 未安装 npm，18005 只提供 API；请在有 node 的机器上执行 cd frontend && npm run build" >&2
  fi
fi

# ---- 18005 ----
WS_ARGS=(--config "$CONFIG" --port "$PORT_WS")
[ "$MOCK" -eq 1 ] && WS_ARGS+=(--mock)
(cd "$ROOT" && CALIB_DEFAULT_ARM="$ARM" exec "$PY" -m calib_workstation "${WS_ARGS[@]}") >>"$LOG_DIR/workstation.log" 2>&1 &
PID_WS=$!
sleep 1.5
kill -0 "$PID_WS" 2>/dev/null || { echo "[start] 18005 启动失败：" >&2; tail -n 20 "$LOG_DIR/workstation.log" >&2; exit 1; }

echo ""
echo "=================================================================="
echo "  标定工作站已启动 $([ "$MOCK" -eq 1 ] && echo '[mock 联调]')"
for IP in $(hostname -I 2>/dev/null || echo 127.0.0.1); do
  if [ "$DEV" -eq 1 ]; then echo "  页面   http://${IP}:5175   （开发模式，API 走 ${IP}:$PORT_WS）"
  else                      echo "  页面   http://${IP}:$PORT_WS"; fi
done
echo "  日志   $LOG_DIR/"
echo "  Ctrl+C 退出并恢复推流"
echo "=================================================================="
wait "$PID_WS"
