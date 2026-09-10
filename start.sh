#!/usr/bin/env bash
# 标定工作站一键启动：18000 能力中心 → 停推流独占相机 → 8131（2D 采集/求解，只读关节）
#                     → 18004（轨迹回放）→ 18005（工作站页面）。Ctrl+C 全部退出并恢复推流。
#
#   ./start.sh                  # 真机
#   ./start.sh --arm left       # 8131 初始读左臂（向导里可按计划切换）
#   ./start.sh --mock           # 无硬件联调：8131 mock 相机/关节，18004 --mock，不碰 18000 与推流
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
HE2D_DIR="$CALIB_ROOT/hand_eye_2D"
REPLAY_DIR="$CALIB_ROOT/calibration_replay"
CONFIG="${WORKSTATION_CONFIG:-$ROOT/config/workstation.yaml}"
IFACE="${NETWORK_INTERFACE:-enp86s0}"
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
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
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
    [ -x "$c" ] && "$c" -c "import cv2, fastapi, numpy, uvicorn, yaml" >/dev/null 2>&1 && { PY="$c"; break; }
  done
fi
if [ -z "$PY" ] || ! "$PY" -c "import cv2, fastapi, numpy, uvicorn, yaml" >/dev/null 2>&1; then
  echo "[start] 找不到含 cv2/fastapi/numpy/uvicorn/yaml 的 Python，请设置 PYTHON=..." >&2; exit 1
fi
echo "[start] Python: $PY"

# ---- 读配置 ----
read -r DATA_ROOT PORT_WS URL_2D URL_REPLAY < <("$PY" - "$CONFIG" <<'EOF'
import sys, yaml, urllib.parse
c = yaml.safe_load(open(sys.argv[1])) or {}
s = c.get("services") or {}
def port(u, d): return urllib.parse.urlparse(u).port or d
print(c.get("data_root", "./calib_workstation_data"), 18005,
      s.get("hand_eye_2d", "http://127.0.0.1:8131"), s.get("replay", "http://127.0.0.1:18004"))
EOF
) || { echo "[start] 读取配置失败: $CONFIG" >&2; exit 1; }
PORT_2D="${URL_2D##*:}"; PORT_REPLAY="${URL_REPLAY##*:}"
echo "[start] 配置 $CONFIG  数据目录 $DATA_ROOT（机器人编号在页面里输入）"

# ---- 端口检查 ----
port_free() { ! ss -ltn 2>/dev/null | awk -v p="$1" '$4 ~ (":" p "$") {f=1} END {exit !f}'; }
for p in "$PORT_2D" "$PORT_WS"; do
  port_free "$p" || { echo "[start] 端口 $p 已被占用，请先结束旧进程" >&2; exit 1; }
done
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
PID_2D=""; PID_WS=""; PID_FE=""; CAMERA_LOCKED=0
cleanup() {
  trap - INT TERM EXIT
  echo ""; echo "[start] 正在退出…"
  [ -n "$PID_FE" ] && kill "$PID_FE" 2>/dev/null
  [ -n "$PID_WS" ] && kill "$PID_WS" 2>/dev/null
  [ "${REPLAY_OWNED:-0}" -eq 1 ] && "$REPLAY_DIR/replay.sh" stop
  if [ -n "$PID_2D" ] && kill -INT "$PID_2D" 2>/dev/null; then
    # uvicorn 会等浏览器的 /ws/stream 连接关闭；最多等 5 秒，然后强制结束以便尽快恢复推流
    for _ in $(seq 1 10); do kill -0 "$PID_2D" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$PID_2D" 2>/dev/null && { kill -TERM "$PID_2D" 2>/dev/null; sleep 1; kill -KILL "$PID_2D" 2>/dev/null; }
    wait "$PID_2D" 2>/dev/null
  fi
  [ "$CAMERA_LOCKED" -eq 1 ] && "$ROOT/scripts/camera_lock.sh" release
  exit 0
}
trap cleanup INT TERM EXIT

# ---- 独占相机 ----
if [ "$MOCK" -eq 0 ]; then
  "$ROOT/scripts/camera_lock.sh" acquire || exit 1
  CAMERA_LOCKED=1
fi

# ---- 8131 ----
# 8131 自己的兜底会话目录；正式采集都由 18004 指定 record_dir 落到回放运行目录
SESSIONS_DIR="$DATA_ROOT/_hand_eye_2d_sessions"
mkdir -p "$SESSIONS_DIR"
ARGS_2D=(--robot h2 --arm "$ARM" --port "$PORT_2D" --save-path "$SESSIONS_DIR" --capability-url "$CAPABILITY_URL")
if [ "$MOCK" -eq 1 ]; then
  ARGS_2D+=(--camera-source mock --joint-source mock --skip-capability)
else
  ARGS_2D+=(--camera-source orbbec --joint-source h2 --network-interface "$IFACE")
fi
echo "[start] 正在启动 8131（首次加载 OpenCV/相机 SDK 可能需要几十秒）…"
(cd "$HE2D_DIR" && exec "$PY" run_server.py "${ARGS_2D[@]}") >>"$LOG_DIR/hand_eye_2d.log" 2>&1 &
PID_2D=$!
for _ in $(seq 1 120); do
  curl -sf --max-time 1 "$URL_2D/api/status" >/dev/null 2>&1 && break
  kill -0 "$PID_2D" 2>/dev/null || { echo "[start] 8131 启动失败，见 $LOG_DIR/hand_eye_2d.log" >&2; tail -n 20 "$LOG_DIR/hand_eye_2d.log" >&2; exit 1; }
  sleep 0.5
done
echo "[start] 8131 就绪（2D 采集/求解，--no arm-control 只读关节）"

# ---- 18004 ----
if [ "${REPLAY_OWNED:-0}" -eq 1 ]; then
  echo "[start] 正在启动 18004 轨迹回放…"
  # mock 下采集仍走 HTTP 到 mock 8131，跑通全链路（会话 → 采集 → 求解 → 归档）
  if [ "$MOCK" -eq 1 ]; then "$REPLAY_DIR/replay.sh" start --mock --capture-http; else "$REPLAY_DIR/replay.sh" start; fi || exit 1
fi

# ---- 前端 ----
if [ "$DEV" -eq 1 ]; then
  (cd "$ROOT/frontend" && exec npx vite --port 5175 --host 0.0.0.0) >>"$LOG_DIR/frontend-dev.log" 2>&1 &
  PID_FE=$!
elif [ ! -f "$ROOT/frontend/dist/index.html" ]; then
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
(cd "$ROOT" && exec "$PY" -m calib_workstation "${WS_ARGS[@]}") >>"$LOG_DIR/workstation.log" 2>&1 &
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
