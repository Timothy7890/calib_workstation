<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { streamUrl, useConfig } from '../composables/useConfig'
import CameraPreview from '../components/CameraPreview.vue'
import StepBar from '../components/StepBar.vue'
import { fmt } from '../utils/format'

const STEPS = [
  { id: 'setup', label: '相机与计划' },
  { id: 'arm', label: '接管与归位' },
  { id: 'run', label: '自动采集' },
  { id: 'solve', label: '求解' },
  { id: 'result', label: '结果与生效' },
]
const RUNNING_STATES = new Set(['preflight', 'moving', 'settling', 'capturing', 'returning', 'paused'])

const { config } = useConfig()
const step = ref('setup')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const cameraNotice = ref('')
const cameraError = ref('')
const enumerating = ref(false)

async function reenumerate() {
  enumerating.value = true
  cameraError.value = ''
  try {
    await loadCameras()
  } finally {
    enumerating.value = false
  }
}

// ---- 状态 ----
const job = ref({})
const replay = ref(null)
const session = ref(null)
const cameras = ref({ devices: [], roles: {} })
const plans = ref([])
const solve = ref(null)

// ---- 表单 ----
const form = ref({ camera_role: 'head', arm: 'right', camera_serial: '', plan_id: '', run_name: '', on_missing_corners: 'continue' })
const capturedCount = computed(() => captures.value.filter((c) => !c.skipped).length)
const noCornersCount = computed(() => captures.value.filter((c) => c.corners_detected === false).length)
const skippedCount = computed(() => captures.value.filter((c) => c.skipped).length)
const solveForm = ref({ square_size_mm: 20, method: 'park' })

const wsUrl = computed(() => streamUrl(config.value))
const boardSize = computed(() => config.value?.board?.size || '')
const roleOptions = computed(() => Object.entries(config.value?.cameras || {}).map(([id, c]) => ({ id, ...c })))
const roleLabel = computed(() => roleOptions.value.find((r) => r.id === form.value.camera_role)?.label || '该位置')
const rememberedSerial = computed(() => cameras.value.roles?.[form.value.camera_role]?.serial || '')
const isRunning = computed(() => RUNNING_STATES.has(replay.value?.state))
const armEngaged = computed(() => !!replay.value?.arm?.engaged)
const runFinished = computed(() => ['completed', 'stopped', 'fault'].includes(replay.value?.state))
const progress = computed(() => replay.value?.progress || {})
const captures = computed(() => replay.value?.captures || [])
const sampleTotal = computed(() => plans.value.find((p) => p.id === job.value.plan_id)?.sample_count ?? job.value.sample_total ?? null)

function stepFromJob(j) {
  switch (j?.step) {
    case 'prepared':
    case 'engaged':
      return 'arm'
    case 'running':
      return 'run'
    case 'captured':
    case 'solving':
      return 'solve'
    case 'solved':
    case 'finalized':
      return 'result'
    default:
      return 'setup'
  }
}

async function guard(fn, { silent = false } = {}) {
  if (busy.value) return
  busy.value = true
  if (!silent) error.value = ''
  try {
    return await fn()
  } catch (e) {
    error.value = e.message
    return undefined
  } finally {
    busy.value = false
  }
}

async function refresh() {
  try {
    const s = await api.calibration()
    job.value = s.job || {}
    replay.value = s.replay
    session.value = s.session
    if (s.replay_error) notice.value = s.replay_error
    else notice.value = ''
  } catch (e) {
    error.value = e.message
  }
}

function usable(d) {
  return d && !d.busy
}

async function loadCameras() {
  try {
    cameras.value = await api.cameras()
    const devs = cameras.value.devices
    const chosen = devs.find((d) => d.serial === form.value.camera_serial)
    if (!chosen || chosen.busy) {
      const hinted = devs.find((d) => d.role_hint === form.value.camera_role && usable(d))
      form.value.camera_serial =
        hinted?.serial || cameras.value.current_serial || devs.find(usable)?.serial || devs[0]?.serial || ''
    }
  } catch (e) {
    cameraError.value = e.message
  }
}

async function loadPlans() {
  try {
    const r = await api.plans(form.value.camera_role, form.value.arm)
    plans.value = r.plans
    if (!plans.value.find((p) => p.id === form.value.plan_id)) {
      form.value.plan_id = plans.value.find((p) => !p.draft)?.id || ''
    }
  } catch (e) {
    error.value = e.message
  }
}

watch(() => [form.value.camera_role, form.value.arm], loadPlans)
watch(
  () => form.value.camera_role,
  (role) => {
    const hinted = cameras.value.devices.find((d) => d.role_hint === role)
    if (hinted) form.value.camera_serial = hinted.serial
  },
)

// 预览时就切到所选相机（首个样本前允许随时切）
async function previewCamera() {
  const wanted = form.value.camera_serial
  if (!wanted) return
  cameraNotice.value = ''
  cameraError.value = ''
  if (wanted === cameras.value.current_serial) {
    cameraNotice.value = `右侧画面就是 ${wanted}。`
    return
  }
  busy.value = true
  try {
    const r = await api.selectCamera(wanted)
    const cam = r.camera || {}
    cameraNotice.value = `已切到 ${cam.serial || wanted}（${cam.name || ''} ${cam.width || ''}×${cam.height || ''}），右侧画面即该相机。`
  } catch (e) {
    cameraError.value = e.message
  } finally {
    busy.value = false
  }
  // 无论成败都以 8131 实际连着的相机为准，避免表单和画面对不上
  await loadCameras()
  if (cameraError.value && cameras.value.current_serial) form.value.camera_serial = cameras.value.current_serial
}

// ---- 各步动作 ----
async function doPrepare() {
  const r = await guard(() => api.prepare({ ...form.value }))
  if (r) {
    job.value = r.job
    step.value = 'arm'
    await refresh()
  }
}

const doEngage = () => guard(async () => { await api.engage(); await refresh() })
const doGuide = () => guard(async () => { await api.guide(); await refresh() })
const doCatch = () => guard(async () => { await api.catchHold(); await refresh() })
const doDisarm = () => guard(async () => { await api.disarm(); await refresh() })

async function doRun() {
  const r = await guard(() => api.run(form.value.run_name))
  if (r) {
    step.value = 'run'
    await refresh()
  }
}
const doPause = () => guard(async () => { await api.pause(); await refresh() })
const doResume = () => guard(async () => { await api.resume(); await refresh() })
const doStop = () => guard(async () => { await api.stop(); await refresh() })

async function doMarkCaptured() {
  const r = await guard(() => api.markCaptured())
  if (r) {
    job.value = r.job
    step.value = 'solve'
  }
}

async function doSolve() {
  const r = await guard(() => api.solve(solveForm.value))
  if (r) await pollSolve()
}

async function pollSolve() {
  try {
    solve.value = await api.solveStatus()
    job.value = solve.value.job || job.value
    if (job.value.step === 'solved') step.value = 'result'
  } catch (e) {
    error.value = e.message
  }
}

async function doFinalize(activate) {
  const r = await guard(() => api.finalize({ activate }))
  if (r) job.value = r.job
}

async function doReset() {
  if (isRunning.value) {
    error.value = '轨迹正在运行，请先停止'
    return
  }
  await guard(() => api.resetJob())
  job.value = {}
  solve.value = null
  step.value = 'setup'
}

// ---- 轮询 ----
let timer = null
function tick() {
  if (step.value === 'arm' || step.value === 'run') refresh()
  else if (step.value === 'solve' && job.value.step === 'solving') pollSolve()
}

onMounted(async () => {
  await refresh()
  step.value = stepFromJob(job.value)
  if (job.value.camera_role) {
    form.value.camera_role = job.value.camera_role
    form.value.arm = job.value.arm
    form.value.camera_serial = job.value.camera_serial
    form.value.plan_id = job.value.plan_id
  }
  if (job.value.square_size_mm) solveForm.value.square_size_mm = job.value.square_size_mm
  else if (config.value?.board?.square_size_mm) solveForm.value.square_size_mm = config.value.board.square_size_mm
  await loadCameras()
  await loadPlans()
  if (job.value.step === 'solving' || job.value.step === 'solved') await pollSolve()
  timer = setInterval(tick, 1000)
})
onUnmounted(() => clearInterval(timer))

</script>

<template>
  <section class="page">
    <div class="page-inner">
      <h1 class="page-title">2D 手眼标定</h1>
      <p class="page-desc">
        棋盘格固定在手臂上，机器人按预设轨迹自动摆位并采集图像与关节角，求解相机相对
        <code>torso_link</code> 的外参。全程需操作员在急停位置监护。
      </p>

      <StepBar :steps="STEPS" :current="step" />

      <div v-if="error" class="alert" style="margin-bottom: 16px">{{ error }}</div>
      <div v-if="notice" class="alert warn" style="margin-bottom: 16px">{{ notice }}</div>

      <div class="grid">
        <!-- 左：步骤内容 -->
        <div class="main">
          <!-- 1 相机与计划 -->
          <div v-if="step === 'setup'" class="card">
            <h2 class="card-title">选择相机位置、手臂与采集计划</h2>
            <div class="form-row">
              <label class="field">
                相机位置
                <select v-model="form.camera_role">
                  <option v-for="r in roleOptions" :key="r.id" :value="r.id">{{ r.label }}</option>
                </select>
              </label>
              <label class="field">
                持板手臂
                <select v-model="form.arm">
                  <option value="right">右臂</option>
                  <option value="left">左臂</option>
                </select>
              </label>
            </div>
            <div class="form-row">
              <label class="field" style="flex: 1">
                相机序列号
                <select v-model="form.camera_serial" @change="previewCamera">
                  <option v-if="!cameras.devices.length" value="">（未发现 Orbbec 相机）</option>
                  <option v-for="d in cameras.devices" :key="d.serial" :value="d.serial" :disabled="d.busy">
                    {{ d.serial }} · {{ d.name
                    }}{{ d.role_hint ? ` · 上次用作${cameras.roles[d.role_hint]?.label}` : ''
                    }}{{ d.busy ? ' · 被其他程序占用' : d.current ? ' · 当前画面' : '' }}
                  </option>
                </select>
              </label>
              <button class="btn ghost" style="align-self: flex-end" :disabled="busy || enumerating" @click="reenumerate">{{ enumerating ? '枚举中…' : '重新枚举' }}</button>
              <button class="btn ghost" style="align-self: flex-end" :disabled="busy || !form.camera_serial" @click="previewCamera">
                预览此相机
              </button>
            </div>
            <div v-if="cameraError" class="alert warn" style="margin-top: 10px">{{ cameraError }}</div>
            <div v-else-if="!cameras.connected && cameras.last_error" class="alert warn" style="margin-top: 10px">
              相机未连接：{{ cameras.last_error }}
            </div>
            <div v-else-if="cameraNotice" class="alert info" style="margin-top: 10px">{{ cameraNotice }}</div>
            <div v-if="cameras.devices.some((d) => d.busy)" class="muted" style="margin-top: 6px; font-size: 12px">
              标注"被其他程序占用"的相机当前无法打开（通常是另一套程序在用它），停掉那个程序后点「重新枚举」。
            </div>
            <p class="muted" style="margin: 8px 0 0; font-size: 12px">
              <template v-if="rememberedSerial && rememberedSerial === form.camera_serial">
                已记住：{{ roleLabel }} = <span class="mono">{{ rememberedSerial }}</span>。想换相机就在上面重选，进入下一步时会覆盖。
              </template>
              <template v-else-if="rememberedSerial">
                {{ roleLabel }}上次用的是 <span class="mono">{{ rememberedSerial }}</span>，本次改选 <span class="mono">{{ form.camera_serial || '—' }}</span>，进入下一步后将记住新的。
              </template>
              <template v-else>
                第一次标定{{ roleLabel }}：不确定是哪台就逐个「预览此相机」看画面，选定后会记住，下次自动选中。
              </template>
            </p>

            <div class="form-row" style="margin-top: 14px">
              <label class="field" style="flex: 1">
                采集计划（{{ form.camera_role === 'head' ? '头部' : '腰部' }} · {{ form.arm === 'right' ? '右臂' : '左臂' }}）
                <select v-model="form.plan_id">
                  <option v-if="!plans.length" value="">（没有匹配的计划，请到「采集计划」创建）</option>
                  <option v-else-if="!form.plan_id" value="">（请选择）</option>
                  <option v-for="p in plans" :key="p.id" :value="p.id" :disabled="p.draft">
                    {{ p.name }} · {{ p.sample_count }} 个采样点{{ p.draft ? '（草稿，不可运行）' : '' }}
                  </option>
                </select>
              </label>
            </div>
            <div class="form-row" style="margin-top: 14px">
              <label class="field" style="flex: 1">
                采样点未检出棋盘格时（图像和关节角都会保存，求解会自动剔除无角点的图）
                <select v-model="form.on_missing_corners">
                  <option value="continue">继续采集后面的点（推荐）</option>
                  <option value="abort">停止采样，沿剩余过渡点走到最后再回原点</option>
                </select>
              </label>
            </div>
            <div v-if="plans.length && plans.every((p) => p.draft)" class="alert warn" style="margin-top: 10px">
              匹配的计划都还是草稿（{{ plans.map((p) => p.name).join('、') }}）：草稿缺少原点或未通过校验，不能运行。
              请到「采集计划」页为其记录原点并完成校验后再回来。
            </div>
            <div class="actions">
              <button class="btn lg" :disabled="busy || !form.camera_serial || !form.plan_id" @click="doPrepare">下一步：接管手臂</button>
            </div>
          </div>

          <!-- 2 接管与归位 -->
          <div v-else-if="step === 'arm'" class="card">
            <h2 class="card-title">接管 {{ job.arm === 'left' ? '左' : '右' }}臂并放到原点</h2>
            <ol class="howto">
              <li>确认没有其他程序在控制手臂，点击「接管」；机器人会保持当前姿态。</li>
              <li>点击「协力拖动」，用手把持板手臂拖到计划的原点姿态附近（手臂会变软，请全程扶住）。</li>
              <li>点击「接住保持」，手臂锁定当前姿态。原点偏差过大时运行会被拒绝，回到第 2 步微调。</li>
              <li>确认棋盘格在画面中，点击「开始自动采集」。</li>
            </ol>
            <div class="state">
              <span class="tag" :class="armEngaged ? 'ok' : ''">{{ armEngaged ? '已接管' : '未接管' }}</span>
              <span class="tag" v-if="replay?.arm?.guide">协力拖动中</span>
              <span class="muted">{{ replay?.message }}</span>
            </div>
            <div class="actions wrap">
              <button class="btn" :disabled="busy || armEngaged" @click="doEngage">接管</button>
              <button class="btn ghost" :disabled="busy || !armEngaged" @click="doGuide">协力拖动</button>
              <button class="btn ghost" :disabled="busy || !armEngaged" @click="doCatch">接住保持</button>
              <button class="btn ghost" :disabled="busy || !armEngaged" @click="doDisarm">解除接管</button>
            </div>
            <div class="form-row" style="margin-top: 18px">
              <label class="field" style="flex: 1">
                运行名称（可选，字母数字 . _ -）
                <input v-model="form.run_name" placeholder="留空自动生成：编号_相机_手臂_时间" />
              </label>
            </div>
            <div class="actions">
              <button class="btn lg" :disabled="busy || !armEngaged" @click="doRun">开始自动采集</button>
              <button class="btn ghost" :disabled="busy" @click="doReset">返回重选</button>
            </div>
          </div>

          <!-- 3 自动采集 -->
          <div v-else-if="step === 'run'" class="card">
            <h2 class="card-title">自动采集中 · {{ job.run_id }}</h2>
            <div class="state">
              <span class="tag" :class="isRunning ? 'warn' : runFinished ? (replay?.state === 'completed' ? 'ok' : 'bad') : ''">
                {{ replay?.state || '—' }}
              </span>
              <span class="muted">{{ replay?.message }}</span>
            </div>
            <div class="progress">
              <div class="bar">
                <div
                  class="fill"
                  :style="{ width: progress.route_count ? `${Math.round(((progress.route_index ?? 0) + 1) / progress.route_count * 100)}%` : '0%' }"
                ></div>
              </div>
              <div class="muted">
                节点 {{ progress.route_index != null ? progress.route_index + 1 : '—' }} / {{ progress.route_count ?? '—' }}
                · 当前 {{ progress.node_name || '—' }}
                · 已采集 {{ capturedCount }}<span v-if="sampleTotal"> / {{ sampleTotal }}</span> 张<span v-if="noCornersCount" class="warn-text">，其中 {{ noCornersCount }} 张未检出棋盘格</span><span v-if="progress.sampling_aborted" class="warn-text">，已在 {{ progress.sampling_aborted }} 停止采样、正在返回</span><span v-if="skippedCount" class="warn-text">，{{ skippedCount }} 个点采集失败</span>
              </div>
            </div>
            <div class="actions wrap">
              <button class="btn danger lg" :disabled="busy || !isRunning" @click="doStop">立即停止</button>
              <button class="btn warn" :disabled="busy || !isRunning || replay?.state === 'paused' || replay?.pause_requested" @click="doPause">
                当前节点后暂停
              </button>
              <button class="btn ghost" :disabled="busy || replay?.state !== 'paused'" @click="doResume">继续</button>
            </div>
            <div v-if="runFinished" class="actions">
              <button class="btn lg" :disabled="busy" @click="doMarkCaptured">
                {{ replay?.state === 'completed' ? '采集完成，去求解' : '用已采集的样本去求解' }}
              </button>
              <button class="btn ghost" :disabled="busy" @click="doDisarm">解除接管</button>
            </div>
            <details class="logs" v-if="replay?.logs?.length">
              <summary>运行日志</summary>
              <pre class="mono">{{ replay.logs.slice(-30).map((l) => `${(l.at || '').slice(11, 19)} ${l.state || ''}  ${l.message || ''}`).join('\n') }}</pre>
            </details>
          </div>

          <!-- 4 求解 -->
          <div v-else-if="step === 'solve'" class="card">
            <h2 class="card-title">求解外参</h2>
            <p class="muted">
              样本 {{ job.sample_count ?? '—' }} 张<span v-if="job.no_corners_count">，其中 {{ job.no_corners_count }} 张未检出棋盘格（求解时自动剔除）</span><span v-if="job.sampling_aborted_at">；在 {{ job.sampling_aborted_at }} 停止采样返回</span><span v-if="job.skipped_count">，{{ job.skipped_count }} 个点采集失败</span>
              · 数据目录 <span class="mono">{{ job.run_dir }}</span>
            </p>
            <div v-if="(job.usable_count ?? job.sample_count ?? 0) < 6" class="alert warn" style="margin-top: 10px">
              检出棋盘格的有效样本少于 6 张，求解可能失败或精度很差。可在「标定记录」查看本次保存的图片，确认棋盘格是否在相机视野内，再调整计划重跑。
            </div>
            <div class="form-row">
              <label class="field">
                方格边长（mm）
                <input v-model.number="solveForm.square_size_mm" type="number" step="0.1" min="1" />
              </label>
              <label class="field">
                方法
                <select v-model="solveForm.method">
                  <option value="park">park（默认）</option>
                  <option value="tsai">tsai</option>
                  <option value="horaud">horaud</option>
                  <option value="andreff">andreff</option>
                  <option value="daniilidis">daniilidis</option>
                </select>
              </label>
            </div>
            <div v-if="job.solve_error" class="alert" style="margin-top: 12px">上次求解失败：{{ job.solve_error }}</div>
            <div class="actions">
              <button class="btn lg" :disabled="busy || job.step === 'solving' || !solveForm.square_size_mm" @click="doSolve">
                {{ job.step === 'solving' ? '求解中…' : '开始求解' }}
              </button>
              <button class="btn ghost" :disabled="busy" @click="doDisarm">解除接管</button>
            </div>
            <details class="logs" v-if="solve?.log?.length" :open="job.step === 'solving'">
              <summary>求解输出</summary>
              <pre class="mono">{{ solve.log.slice(-40).join('\n') }}</pre>
            </details>
          </div>

          <!-- 5 结果 -->
          <div v-else-if="step === 'result'" class="card">
            <h2 class="card-title">求解结果</h2>
            <table class="plain" v-if="job.result_summary">
              <tbody>
                <tr><th>样本 / 内点</th><td>{{ job.result_summary.num_samples }} / {{ job.result_summary.num_inliers }}</td></tr>
                <tr>
                  <th>平移残差（mm）</th>
                  <td>均值 {{ fmt(job.result_summary.residual_translation_mm?.mean) }} · 最大 {{ fmt(job.result_summary.residual_translation_mm?.max) }}</td>
                </tr>
                <tr>
                  <th>旋转残差（°）</th>
                  <td>均值 {{ fmt(job.result_summary.residual_rotation_deg?.mean, 3) }} · 最大 {{ fmt(job.result_summary.residual_rotation_deg?.max, 3) }}</td>
                </tr>
                <tr><th>相机位置 t（m）</th><td class="mono">{{ (job.result_summary.t_cam2base_m || []).map((v) => fmt(v, 4)).join(', ') }}</td></tr>
                <tr><th>相机姿态 rpy（rad）</th><td class="mono">{{ (job.result_summary.rpy_rad || []).map((v) => fmt(v, 4)).join(', ') }}</td></tr>
              </tbody>
            </table>
            <div class="alert info" style="margin-top: 14px" v-if="job.result_summary && (job.result_summary.num_inliers ?? 0) < 8">
              内点少于 8 个，结果可能不稳定；建议补充更多姿态差异大的采样点后重新采集。
            </div>
            <div v-if="job.finalized" class="alert info" style="margin-top: 14px">
              已归档{{ job.activated ? '并设为当前生效' : '（未生效）' }}：外参 + 内参产物已写入机器人 {{ config?.robot.unit_code }} 的标定目录。
            </div>
            <div class="actions wrap">
              <button class="btn lg" :disabled="busy || job.finalized" @click="doFinalize(true)">确认生效并归档</button>
              <button class="btn ghost" :disabled="busy || job.finalized" @click="doFinalize(false)">仅归档，不生效</button>
              <button class="btn ghost" :disabled="busy" @click="doDisarm">解除接管</button>
              <RouterLink class="btn ghost" :to="{ name: 'history' }">查看标定记录</RouterLink>
              <button class="btn ghost" :disabled="busy" @click="doReset">开始新的标定</button>
            </div>
          </div>
        </div>

        <!-- 右：实时画面与当前任务 -->
        <aside class="side">
          <CameraPreview :url="wsUrl" :board-size="boardSize" />
          <div class="card" style="margin-top: 14px">
            <h2 class="card-title">当前任务</h2>
            <table class="plain">
              <tbody>
                <tr><th>机器人</th><td>{{ config?.robot.unit_code }}</td></tr>
                <tr><th>相机</th><td>{{ job.camera_label || roleOptions.find((r) => r.id === form.camera_role)?.label }} · <span class="mono">{{ job.camera_serial || form.camera_serial || '—' }}</span></td></tr>
                <tr><th>手臂</th><td>{{ (job.arm || form.arm) === 'left' ? '左臂' : '右臂' }}</td></tr>
                <tr><th>计划</th><td>{{ job.plan_name || plans.find((p) => p.id === form.plan_id)?.name || '—' }}</td></tr>
                <tr><th>会话</th><td class="mono">{{ session?.run_id || '—' }} · {{ session?.count ?? 0 }} 张</td></tr>
              </tbody>
            </table>
          </div>
        </aside>
      </div>
    </div>
  </section>
</template>

<style scoped>
.warn-text {
  color: #b26a00;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 20px;
  align-items: start;
}

.form-row {
  display: flex;
  gap: 14px;
  align-items: flex-end;
}

.form-row .field {
  min-width: 160px;
}

.form-row + .form-row {
  margin-top: 12px;
}

.actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
}

.actions.wrap {
  flex-wrap: wrap;
}

.howto {
  margin: 0 0 14px;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.8;
  color: #444;
}

.state {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
}

.progress {
  margin-top: 14px;
}

.bar {
  height: 8px;
  background: #e4e4e4;
  border-radius: 999px;
  overflow: hidden;
  margin-bottom: 8px;
}

.fill {
  height: 100%;
  background: #1a1a1a;
  transition: width 0.4s;
}

.logs {
  margin-top: 16px;
  font-size: 13px;
  color: #555;
}

.logs pre {
  margin: 8px 0 0;
  max-height: 220px;
  overflow: auto;
  padding: 10px;
  background: #fff;
  border-radius: 4px;
  white-space: pre-wrap;
}

@media (max-width: 960px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
