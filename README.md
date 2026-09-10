# 标定工作站（18005）

交付给客户/现场的唯一入口：一个网页完成 2D 手眼标定（头部 / 腰部 RGB 相机 → `torso_link` 外参 + 内参），
产物按机器人编号归档，后续推送云端平台（Camera-Tools-for-Robot）。

```
浏览器 ──► 18005 calib_workstation（本项目：向导页面 + 编排接口 + 产物归档）
              ├─► 8131  hand_eye_2D      相机枚举/选择、图像流、采集、求解（只读关节，不控制手臂）
              ├─► 18004 calibration_replay 轨迹回放：接管/协力拖动/归位/自动摆位（唯一 rt/arm_sdk 发布者）
              └─► 18000 能力中心          启动前拜访
```

## 一键启动

```bash
cd /home/robot/yx/project/calib/calib_workstation
./start.sh                 # 真机；Ctrl+C 全部退出并恢复推流
./start.sh --arm left      # 8131 初始读左臂（向导里会按计划自动切换）
./start.sh --mock          # 无硬件联调：8131 mock 相机（带可检出的棋盘格）/mock 关节，18004 --mock 但采集仍走 HTTP
./start.sh --dev           # 前端 Vite 开发服务器 5175
```

脚本顺序：找 Python → 读 `config/workstation.yaml` → 检查端口 → 18000 可达（否则拉起 `IK_replay/capability.sh`）
→ `scripts/camera_lock.sh acquire` 停 teleimager 推流 → 8131 → 18004（`replay.sh start`）→ 前端构建（缺失时）→ 18005。
退出时逆序停止，并只在“推流原本在跑”时才 `systemctl start` 恢复。

### 一次性安装：免密停止/恢复推流

```bash
sudo install -m 0440 -o root -g root deploy/sudoers-calib-camera /etc/sudoers.d/calib-camera
sudo visudo -c
```

只放行 `systemctl stop|start teleimager-camera-capture.service` 两条命令。

## 配置 `config/workstation.yaml`

| 键 | 说明 |
|---|---|
| 机器人编号 | **不在配置文件里**。首次打开页面时输入（右上角可切换），保存在 `<data_root>/workstation_state.json`；是产物目录第一层 |
| `services.*` | 8131 / 18004 / 18000 地址 |
| `data_root` | 产物根目录 → `<data_root>/<unit_code>/calibrations/…`；8131 的兜底会话目录在 `<data_root>/_hand_eye_2d_sessions/`（正式采集落回放运行目录） |
| `cameras.head|waist` | 标签、回放计划目标（`hand_eye_2D_head|waist`）、可选序列号（留空则向导里从枚举结果选） |
| `board` | 棋盘格内角点 `11x8`、默认方格边长 |

## 向导流程（页面「开始标定」）

1. 相机与计划：相机位置 → 持板手臂 → 相机序列号（8131 实时枚举，右侧预览即所选相机）→ 采集计划（自动按目标/手臂过滤，草稿不可选）。
2. 接管与归位：接管 → 协力拖动到原点 → 接住保持。
3. 自动采集：回放按计划摆位，静止后 8131 采集（`require_corners` 未检出则拒绝）；可暂停/立即停止。数据直接落 `calibration_replay_data/runs/<arm>/<run_id>/`（`left/ joints/ session_meta.json camera_intrinsics.json run.json`）。
4. 求解：方格边长 → 8131 `solve_handeye.py`。
5. 结果与生效：内点/残差 → 「确认生效并归档」。

任务状态落在 `<unit_root>/state/current_job.json`，刷新页面/重启 18005 会回到原步骤。

## 产物包（与云端契约）

```
<data_root>/<unit_code>/calibrations/
  extrinsic/<head|waist>/<run_id>/manifest.json + handeye_result_left.json + session_meta.json + run.json
  intrinsic/<head|waist>/<run_id>/manifest.json + camera_intrinsics.json
  <type>/<head|waist>/active.json            # 当前生效指向
```

`manifest.json`（`schema: calib-manifest/1`）：`unit_code / vendor / robot_model / type / camera_role / camera_serial / arm /
run_id / tool / tool_version / created_at / source_run_dir / quality{num_samples, num_inliers, residual_*} / files[{name, bytes, sha256}] /
status(draft|active|superseded) / cloud{pushed, pushed_at, remote_id}`。云端 `GET /api/robots/units/{unitCode}/calibrations?type=`
直接返回这些字段即可；推送器（待做）按 `cloud.pushed=false` 入队。

## 接口

`GET /api/config|health|cameras|plans?camera_role_id=&arm=|calibration|artifacts|artifacts/active|runs`
`POST /api/cameras/select|detect`
`POST /api/calibration/prepare|engage|guide|catch|disarm|run|pause|resume|stop|mark-captured|solve|finalize|reset`
`GET /api/calibration/solve/status`，`POST /api/artifacts/{type}/{role}/{run_id}/activate`，`GET …/files/{name}`

下游错误统一为 `{ok:false, error, service, status}`：409 表示对方拒绝（可展示原因），502 表示对方不可达。

## 前端

Vue 3 + Vite + Vue Router，无 UI 库；`src/style.css`、顶部导航与 logo 来自 Camera-Tools-for-Robot，两边手动保持一致。
`cd frontend && npm install && npm run build` 产物由 18005 托管；`start.sh` 在缺少 `dist/` 时自动构建。
