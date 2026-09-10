#!/usr/bin/env bash
# Orbbec 相机独占：标定期间停掉 teleimager 推流服务，结束后恢复到原状态。
#
#   scripts/camera_lock.sh acquire   # 若推流在跑：记录“原本在跑”并停止；已停则什么都不做
#   scripts/camera_lock.sh release   # 只在 acquire 时是“原本在跑”才重新启动（不擅自拉起别人手动停掉的服务）
#   scripts/camera_lock.sh status
#
# 需要 deploy/sudoers-calib-camera 免密规则；没有规则时会给出明确提示并失败。
set -u

SERVICE="${CAMERA_SERVICE:-teleimager-camera-capture.service}"
STATE_FILE="${CAMERA_LOCK_STATE:-/tmp/calib-camera-lock.state}"
SETTLE_S="${CAMERA_RELEASE_SETTLE_S:-2}"

have_sudo() {
    sudo -n /usr/bin/systemctl stop "$SERVICE" --dry-run >/dev/null 2>&1 && return 0
    # --dry-run 不一定被 sudoers 精确匹配；退而检查 sudo -l
    sudo -n -l /usr/bin/systemctl stop "$SERVICE" >/dev/null 2>&1
}

acquire() {
    if ! systemctl is-active --quiet "$SERVICE"; then
        echo "[camera] 推流服务 $SERVICE 未运行，无需停止"
        echo "was_active=0" >"$STATE_FILE"
        return 0
    fi
    if ! have_sudo; then
        echo "[camera] 需要免密停止 $SERVICE，但 sudo 规则未安装。请执行一次：" >&2
        echo "         sudo install -m 0440 -o root -g root $(dirname "$0")/../deploy/sudoers-calib-camera /etc/sudoers.d/calib-camera" >&2
        return 1
    fi
    echo "[camera] 停止推流服务 $SERVICE（标定结束后自动恢复）"
    if ! sudo -n /usr/bin/systemctl stop "$SERVICE"; then
        echo "[camera] 停止 $SERVICE 失败" >&2
        return 1
    fi
    echo "was_active=1" >"$STATE_FILE"
    # 等 USB 设备释放
    for _ in $(seq 1 20); do
        systemctl is-active --quiet "$SERVICE" || break
        sleep 0.25
    done
    sleep "$SETTLE_S"
    return 0
}

release() {
    local was_active=0
    if [ -f "$STATE_FILE" ]; then
        # shellcheck disable=SC1090
        . "$STATE_FILE"
        rm -f "$STATE_FILE"
    fi
    if [ "${was_active:-0}" != "1" ]; then
        echo "[camera] 推流服务原本未运行，不恢复"
        return 0
    fi
    echo "[camera] 恢复推流服务 $SERVICE"
    sudo -n /usr/bin/systemctl start "$SERVICE" || {
        echo "[camera] 恢复 $SERVICE 失败，请手动: sudo systemctl start $SERVICE" >&2
        return 1
    }
}

status() {
    echo "service=$SERVICE active=$(systemctl is-active "$SERVICE" 2>/dev/null)"
    [ -f "$STATE_FILE" ] && echo "lock: $(cat "$STATE_FILE")" || echo "lock: none"
}

case "${1:-}" in
    acquire) acquire ;;
    release) release ;;
    status)  status ;;
    *) echo "用法: $0 acquire|release|status" >&2; exit 2 ;;
esac
