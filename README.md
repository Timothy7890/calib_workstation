# 标定工作站（18005）

交付给客户/现场的唯一入口：一个网页完成 2D 手眼标定（头部 / 腰部 RGB 相机 → `torso_link` 外参 + 内参）和
3D 手安装 / TCP 标定，
产物按机器人编号归档，后续推送云端平台（Camera-Tools-for-Robot）。

```
浏览器 ──► 18005 calib_workstation（本项目：向导页面 + 编排接口 + 产物归档）
              ├─► 原生 2D 引擎          相机枚举/选择、图像流、采集、求解（18005进程内）
              ├─► 原生 3D 引擎          RGB-D点选、手安装/TCP解算（18005进程内）
              ├─► 内置 3D 操作台       点云标注与手模型核对（18005同源页面）
              ├─► 18004 calibration_replay 轨迹回放：接管/协力拖动/归位/自动摆位（唯一 rt/arm_sdk 发布者）
              └─► 18000 能力中心          启动前拜访
```

## 一键启动

```bash
cd /home/robot/yx/project/calib/calib_workstation
./start.sh                 # 真机；Ctrl+C 全部退出并恢复推流
./start.sh --arm left      # 初始选择左臂（向导里会按计划自动切换）
./start.sh --mock          # 无硬件联调：18005 mock 相机（带可检出的棋盘格），18004 --mock 但采集仍走 HTTP
./start.sh --dev           # 前端 Vite 开发服务器 5175
```

2D 与 3D 始终随 `./start.sh` 同时可用，不再需要额外的 3D 启动参数。

脚本顺序：找 Python → 读 `config/workstation.yaml` → 检查端口 → 18000 可达（否则拉起 `IK_replay/capability.sh`）
→ `scripts/camera_lock.sh acquire` 释放相机 → 18004（`replay.sh start`）→ 前端构建（缺失时）→ 18005（含原生2D/3D引擎）。
退出时逆序停止，并只在“推流原本在跑”时才 `systemctl start` 恢复。

工作站的目标相机架构是单一所有者：一个物理序列号只允许一个 SDK Pipeline，
2D/3D 引擎以具名 consumer 共享同一份同步帧。consumer 结束不会释放设备；只有
18005 整体退出才关闭相机，再由 `camera_lock.sh release` 恢复启动前正在运行的推流。
这避免在两个内部流程切换的间隙被外部进程抢占相机。

`camera_lock.sh acquire` 会释放两个占用 Orbbec 的程序：

- 容器 `robot_control_node_all` 内的 ROS `orbbec_camera` 节点（占头部相机 `CP0X663000B7`）：直接 `pkill`，
  **退出时不恢复**（该容器为旧版本、暂不维护；需要时手动进容器 `roslaunch orbbec_camera gemini_330_series.launch`）。
  用 `CAMERA_ROS_CONTAINER=` 置空可跳过这一步。
- `teleimager-camera-capture.service` 推流：停止并记录状态，退出时按原状态恢复。

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
| `services.*` | 18005 原生 2D/3D、18004、18000 地址 |
| `data_root` | 产物根目录 → `<data_root>/<unit_code>/calibrations/…`；2D 兜底会话目录在 `<data_root>/_hand_eye_2d_sessions/`（正式采集落回放运行目录） |
| `cameras.head|waist` | 标签、回放计划目标（`hand_eye_2D_head|waist`）、可选序列号（留空则向导里从枚举结果选） |
| `board` | 棋盘格内角点 `11x8`、默认方格边长 |

选择机器人编号时，18005 会同时把 `unit_code + vendor + model` 登记到本机 18000；重启后也会用已保存编号自动补登记。
18000 一旦登记便拒绝静默切换编号或型号，且只接受与该整机身份一致的标定 manifest。需要把一台电脑改配给另一台机器人时，
应先备份原机器数据并显式清理/迁移 18000 注册表，不能只改工作站编号。

## 向导流程（页面「开始标定」）

1. 相机与计划：相机位置 → 持板手臂 → 相机序列号（18005 实时枚举，右侧预览即所选相机）→ 采集计划（自动按目标/手臂过滤，草稿不可选）。
2. 接管与归位：接管 → 协力拖动到原点 → 接住保持。
3. 自动采集：回放按计划摆位，静止后由 18005 原生引擎采集（`require_corners` 未检出则拒绝）；可暂停/立即停止。数据直接落 `calibration_replay_data/runs/<arm>/<run_id>/`（`left/ joints/ session_meta.json camera_intrinsics.json run.json`）。
4. 求解：方格边长 → 18005 原生 `solve_handeye.py`。
5. 结果与生效：内点/残差 → 「确认生效并归档」。

任务状态落在 `<unit_root>/state/current_job.json`，刷新页面/重启 18005 会回到原步骤。

## 3D 手安装 / TCP 流程（页面「3D 手/TCP」）

自动采集 → 手动选点（选择标定对象）→ 按模式求解 → 检查与归档。

自动采集仅依赖相机、当前生效的2D外参、18000激活臂及有效采集计划，不要求登记几何手模型。
也可载入已有采集目录直接进入选点。可选的“灵巧手零位保持”默认关闭，勾选后通过18000的设备映射控制手，
与几何模型目录无关。采集期间目标必须相对腕部保持刚性；活动手指需要保持固定姿态。
机械臂接管、归位、轨迹校验及人工监护要求不变，软件停止不能代替实体急停。

选点阶段选择三种模式之一：

- 已知手模型：选择与实物相符的几何模型，使用点云操作台求手安装位姿与TCP。模型点默认使用零位FK，活动手指选点要求实物姿态与模型一致。
  几何模型ID独立于18000设备手ID保存；归档仍按18000当前实体手生成 `hand_mount` 和 `tcp_profile`，绑定生效并按配置推送云端。
- 普通刚性工具：无需URDF，为工具输入独立编号，重复选取原点（TCP）、X正方向点、XY平面正Y侧点。
  每点至少覆盖3个不同姿态；拒绝共线或间距过近的方向点，输出腕部到工具的位姿及残差。
- 仅求TCP：无需URDF，在至少3个不同姿态中选取同一实体点，输出腕坐标系中的TCP位置及残差，不定义朝向。

通用工具和单点TCP目前以独立工具身份本地归档，可在“标定记录”下载，包含结果和选点观测。
18000及云端尚未支持通用工具绑定，因此这些产物明确标记 `local_only`，不会推送或冒充手型号生效。
修改选点、切换模式或对象后必须重新求解；不会自动删除原始采集数据。

强脑模型统一命名为 `qiangnao-revo2-left` / `qiangnao-revo2-right`（强脑-Revo2-左/右）。
旧 `qiangnao-1-left` / `qiangnao-1-right` 作为读取别名兼容，不改写原始采集文件及18000历史绑定。
“下一步”上方显示18000当前工具类型；选点确认对象后，任务摘要显示本次选定的模型或通用工具类型。

## 产物包（与云端契约）

```
<data_root>/<unit_code>/calibrations/
  extrinsic/<head|waist>/<run_id>/manifest.json + handeye_result_left.json + session_meta.json + run.json
  intrinsic/<head|waist>/<run_id>/manifest.json + camera_intrinsics.json
  hand_mount/<arm>__<hand_id>/<run_id>/manifest.json + mount_result.json
  tcp_profile/<arm>__<hand_id>/<run_id>/manifest.json + tcp_profile.json
  <type>/<head|waist>/active.json            # 当前生效指向
```

`manifest.json` 新产物使用 `schema: calib-manifest/2`，旧的 `calib-manifest/1` 继续兼容读取。v2 在原字段之外增加稳定的
`artifact_id`、标定对象 `subject`、分区键 `subject_key`、输入关系 `dependencies` 与运行兼容条件 `compatibility`。
相机产物的 subject 是机器人编号 + 相机位置/序列号；后续 3D 产物的 subject 是机器人编号 + 手臂 + 实体手，避免把
`T_wrist2hand` 错绑到某个相机位置。其余字段为：`unit_code / vendor / robot_model / type / camera_role / camera_serial / arm /
run_id / tool / tool_version / created_at / source_run_dir / quality{num_samples, num_inliers, residual_*} / files[{name, bytes, sha256}] /
status(draft|active|superseded) / cloud{pushed, pushed_at, remote_id, pushed_status, url}`。

### 云端推送

云端平台（Camera-Tools-for-Robot 后端）的地址与 token 在「标定记录」页的「云端同步」卡片里填写，
保存到 `<data_root>/cloud.json`（0600），不进配置文件。推送单位是一个产物包目录，走云端
`POST /api/robots/units/{unit_code}/calibrations`（multipart，同 run 重传 = 覆盖，幂等）；成功后回写
`cloud.pushed / pushed_at / remote_id / pushed_status`。判定：从没推过 = 未推送；`pushed_status != status`
（之后被设为生效 / 被替代）= 状态待同步；两者都由「立即同步」或归档 / 切换生效后的自动推送（可关）补齐。
本地删除产物会顺带删云端副本（失败只提示）。

## 接口

`GET /api/config|health|cameras|plans?camera_role_id=&arm=|calibration|hand-calibration|artifacts|artifacts/active|runs`
`POST /api/cameras/select|detect`
`POST /api/calibration/prepare|engage|guide|catch|disarm|run|pause|resume|stop|mark-captured|solve|finalize|reset`
`POST /api/hand-calibration/solve|finalize`
`GET /api/calibration/solve/status`，`POST /api/artifacts/{type}/{role}/{run_id}/activate|push`，`DELETE …/{run_id}`，`GET …/files/{name}`
`GET|PUT /api/cloud`，`POST /api/cloud/test|sync?force=`

下游错误统一为 `{ok:false, error, service, status}`：409 表示对方拒绝（可展示原因），502 表示对方不可达。

## 前端

Vue 3 + Vite + Vue Router，无 UI 库；`src/style.css`、顶部导航与 logo 来自 Camera-Tools-for-Robot，两边手动保持一致。
`cd frontend && npm install && npm run build` 产物由 18005 托管；`start.sh` 在缺少 `dist/` 时自动构建。
