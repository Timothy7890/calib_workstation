<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { streamUrl, useConfig } from '../composables/useConfig'
import CameraPreview from '../components/CameraPreview.vue'
import StepBar from '../components/StepBar.vue'

const STEPS = [
  { id: 'setup', label: '数据与计划' },
  { id: 'arm', label: '接管与归位' },
  { id: 'run', label: '自动采集' },
  { id: 'annotate', label: '手动选点' },
  { id: 'solve', label: '求解校验' },
  { id: 'result', label: '归档与生效' },
]
const RUNNING = new Set(['preflight', 'moving', 'settling', 'capturing', 'returning', 'paused'])
const { config } = useConfig()

const step = ref('setup')
const state = ref(null)
const job = ref({})
const replay = ref(null)
const cameras = ref({ devices: [], roles: {} })
const plans = ref([])
const tasks = ref([])
const episodes = ref([])
const annotation = ref({})
const error = ref('')
const notice = ref('')
const busy = ref('')
const iframeKey = ref(0)
const publishResult = ref(null)
const handSerial = ref('')
const archiveRunId = ref(defaultRunId())
const form = ref({
  source: 'capture',
  camera_role: 'head',
  arm: 'right',
  camera_serial: '',
  plan_id: '',
  run_name: '',
  task_path: '',
  hold_hand_zero: false,
})

function defaultRunId() {
  const d = new Date()
  const p = (value) => String(value).padStart(2, '0')
  return `mount-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

function stepFromJob(value) {
  if (value?.calibration_kind !== '3d') return 'setup'
  if (['prepared', 'engaged'].includes(value.step)) return 'arm'
  if (value.step === 'running') return 'run'
  if (value.step === 'annotating') return 'annotate'
  if (value.step === 'annotated') return 'solve'
  if (['solved', 'finalized'].includes(value.step)) return 'result'
  return 'setup'
}

const active = computed(() => state.value?.active || {})
const wsUrl = computed(() => streamUrl(config.value))
const boardSize = computed(() => config.value?.board?.size || '')
const activeHand = computed(() =>
  (state.value?.hands || []).find((item) => item.id === active.value.hand_id),
)
const extrinsic = computed(() => state.value?.camera_artifacts?.extrinsic || null)
const mount = computed(() => state.value?.mount || {})
const result = computed(() => mount.value?.result || null)
const isRunning = computed(() => RUNNING.has(replay.value?.state))
const runFinished = computed(() => ['completed', 'stopped', 'fault'].includes(replay.value?.state))
const armEngaged = computed(() => !!replay.value?.arm?.engaged)
const progress = computed(() => replay.value?.progress || {})
const annotatedCount = computed(() => episodes.value.filter(
  (item) => Number(item.mount_sample_count || 0) > 0,
).length)
const annotationReady = computed(() =>
  Number(annotation.value.usable_point_count || 0) >= Number(annotation.value.min_points || 3),
)
const sampleTotal = computed(() =>
  plans.value.find((item) => item.id === job.value.plan_id)?.sample_count
  ?? job.value.sample_total
  ?? null,
)
const roleOptions = computed(() =>
  Object.entries(config.value?.cameras || {}).map(([id, value]) => ({ id, ...value })),
)
const currentRoleLabel = computed(() =>
  roleOptions.value.find((item) => item.id === (job.value.camera_role || form.value.camera_role))?.label
  || job.value.camera_label
  || form.value.camera_role,
)
const hasReadyPlan = computed(() => plans.value.some((item) => !item.draft))

async function guard(name, fn) {
  if (busy.value) return undefined
  busy.value = name
  error.value = ''
  try {
    return await fn()
  } catch (exception) {
    error.value = exception.message
    return undefined
  } finally {
    busy.value = ''
  }
}

async function loadPlans() {
  try {
    plans.value = (await api.handCalibrationPlans(form.value.arm)).plans || []
    if (!plans.value.some((item) => item.id === form.value.plan_id)) {
      form.value.plan_id = plans.value.find((item) => !item.draft)?.id || ''
    }
  } catch (exception) {
    error.value = exception.message
  }
}

function syncForm() {
  const activeArm = String(active.value.arm || '').replace('_arm', '')
  if (activeArm === 'left' || activeArm === 'right') form.value.arm = activeArm
  if (job.value.camera_role) form.value.camera_role = job.value.camera_role
  if (job.value.camera_serial) form.value.camera_serial = job.value.camera_serial
  if (job.value.plan_id) form.value.plan_id = job.value.plan_id
  if (state.value?.camera_role) form.value.camera_role = state.value.camera_role
  const remembered = cameras.value.roles?.[form.value.camera_role]?.serial
  if (!form.value.camera_serial && remembered) form.value.camera_serial = remembered
  if (!form.value.task_path && tasks.value.length) {
    form.value.task_path = tasks.value.find((item) => item.current)?.path || tasks.value[0].path
  }
}

async function refresh({ keepStep = false } = {}) {
  try {
    const response = await api.handCalibration()
    state.value = response
    job.value = response.job || {}
    replay.value = response.replay || null
    tasks.value = response.tasks || []
    episodes.value = response.episodes || []
    annotation.value = response.annotation || {}
    syncForm()
    if (!keepStep) step.value = stepFromJob(job.value)
  } catch (exception) {
    error.value = exception.message
  }
}

async function loadOptions() {
  try {
    cameras.value = await api.cameras()
    syncForm()
    await loadPlans()
  } catch (exception) {
    error.value = exception.message
  }
}

async function previewCamera() {
  const serial = form.value.camera_serial
  if (!serial || serial === cameras.value.current_serial) return
  const response = await guard('camera', () => api.selectCamera(serial, form.value.camera_role))
  if (!response) return
  cameras.value = await api.cameras()
}

async function prepareCapture() {
  const response = await guard('prepare', () => api.prepareHandCalibration({
    camera_role: form.value.camera_role,
    arm: form.value.arm,
    camera_serial: form.value.camera_serial,
    plan_id: form.value.plan_id,
    hold_hand_zero: form.value.hold_hand_zero,
  }))
  if (!response) return
  job.value = response.job
  step.value = 'arm'
  await refresh({ keepStep: true })
}

async function loadExisting() {
  const response = await guard('load', () => api.loadHandCalibrationTask({
    path: form.value.task_path,
    camera_role: form.value.camera_role,
  }))
  if (!response) return
  job.value = response.job
  episodes.value = response.episodes || []
  annotation.value = response.annotation || {}
  iframeKey.value += 1
  step.value = 'annotate'
  notice.value = '数据检查通过，已直接进入手动选点。'
}

const engage = () => guard('engage', async () => { await api.engage(); await refresh({ keepStep: true }) })
const guide = () => guard('guide', async () => { await api.guide(); await refresh({ keepStep: true }) })
const catchHold = () => guard('catch', async () => { await api.catchHold(); await refresh({ keepStep: true }) })
const disarm = () => guard('disarm', async () => { await api.disarm(); await refresh({ keepStep: true }) })
const pause = () => guard('pause', async () => { await api.pause(); await refresh({ keepStep: true }) })
const resume = () => guard('resume', async () => { await api.resume(); await refresh({ keepStep: true }) })
const stop = () => guard('stop', async () => { await api.stop(); await refresh({ keepStep: true }) })

async function runCapture() {
  const armName = job.value.arm === 'left' ? '左臂' : '右臂'
  if (!confirm(`即将开始3D自动采集：${armName}将按计划「${job.value.plan_name}」运动。\n请确认周围无人，并在急停位置全程监护。\n\n开始？`)) return
  const response = await guard('run', () => api.run(form.value.run_name))
  if (!response) return
  step.value = 'run'
  await refresh({ keepStep: true })
}

async function enterAnnotation() {
  const response = await guard('captured', () => api.markCaptured())
  if (!response) return
  job.value = response.job
  iframeKey.value += 1
  step.value = 'annotate'
  await refresh({ keepStep: true })
}

async function finishAnnotation() {
  const response = await guard('annotate', () => api.finishHandAnnotation())
  if (!response) return
  job.value = response.job
  step.value = 'solve'
  await refresh({ keepStep: true })
}

async function solve() {
  const response = await guard('solve', () =>
    api.solveHandCalibration(job.value.camera_role || form.value.camera_role),
  )
  if (!response) return
  await refresh({ keepStep: true })
  step.value = 'result'
  notice.value = '解算结果已生成，请检查质量后归档生效。'
}

async function finalize() {
  if (!confirm('确认当前3D解算结果正确，并归档为当前手安装与TCP标定？')) return
  const response = await guard('finalize', () => api.finalizeHandCalibration({
    run_id: archiveRunId.value,
    camera_role: job.value.camera_role || form.value.camera_role,
    hand_serial: handSerial.value,
  }))
  if (!response) return
  publishResult.value = response
  notice.value = '已归档 hand_mount 与 tcp_profile，并绑定至18000当前组合。'
  await refresh({ keepStep: true })
}

async function reset() {
  if (isRunning.value) {
    error.value = '轨迹正在运行，请先停止。'
    return
  }
  const response = await guard('reset', () => api.resetJob())
  if (response === undefined) return
  job.value = {}
  publishResult.value = null
  notice.value = ''
  archiveRunId.value = defaultRunId()
  step.value = 'setup'
  await refresh({ keepStep: true })
}

watch(() => form.value.arm, loadPlans)
watch(() => form.value.camera_role, () => {
  const remembered = cameras.value.roles?.[form.value.camera_role]?.serial
  if (remembered && cameras.value.devices.some((item) => item.serial === remembered)) {
    form.value.camera_serial = remembered
    previewCamera()
  }
})

let timer = null
onMounted(async () => {
  await refresh()
  await loadOptions()
  timer = setInterval(() => {
    if (['arm', 'run', 'annotate'].includes(step.value)) refresh({ keepStep: true })
  }, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <section class="page hand-wizard">
    <div class="page-inner hand-inner">
      <div class="page-heading">
        <div>
          <h1 class="page-title">3D 手安装 / TCP 标定</h1>
          <p class="page-desc">与2D相同的自动轨迹采集流程；采集后增加点云手动选点，再统一求解、归档并绑定至18000。</p>
        </div>
        <button v-if="job.calibration_kind === '3d'" class="btn ghost" :disabled="!!busy || isRunning" @click="reset">重新开始</button>
      </div>

      <StepBar :steps="STEPS" :current="step" />
      <div v-if="error" class="alert page-message">{{ error }}</div>
      <div v-if="notice" class="alert info page-message">{{ notice }}</div>
      <div v-if="state && !state.service?.ok" class="alert warn page-message">
        3D相机暂不可用：{{ state.service?.error }}。请关闭占用相机的程序后刷新页面；已有数据仍可继续浏览。
      </div>
      <div v-if="publishResult?.cloud_error" class="alert warn page-message">本地归档及18000绑定成功，但云端同步失败：{{ publishResult.cloud_error }}</div>

      <div class="wizard-grid">
        <main>
          <article v-if="step === 'setup'" class="card">
            <div class="setup-heading">
              <h2 class="card-title">数据来源</h2>
              <div class="source-tabs">
                <button :class="{ active: form.source === 'capture' }" @click="form.source = 'capture'">重新采集</button>
                <button :class="{ active: form.source === 'existing' }" @click="form.source = 'existing'">使用已有数据</button>
              </div>
            </div>

            <div class="identity-grid">
              <label class="field">借助相机的位置
                <select v-model="form.camera_role"><option v-for="role in roleOptions" :key="role.id" :value="role.id">{{ role.label }}</option></select>
              </label>
              <label class="field">工具所在手臂
                <select v-model="form.arm" :disabled="form.source === 'existing'">
                  <option value="right">右臂</option><option value="left">左臂</option>
                </select>
              </label>
            </div>

            <template v-if="form.source === 'capture'">
              <label class="field field-wide">相机序列号
                <select v-model="form.camera_serial" @change="previewCamera">
                  <option value="">（请选择）</option>
                  <option v-for="camera in cameras.devices || []" :key="camera.serial" :value="camera.serial">{{ camera.serial }} · {{ camera.name }}</option>
                </select>
              </label>
              <label class="field field-wide">3D采集计划
                <select v-model="form.plan_id">
                  <option value="">（请选择）</option>
                  <option v-for="plan in plans" :key="plan.id" :value="plan.id" :disabled="plan.draft">{{ plan.name }} · {{ plan.sample_count }}个采样点{{ plan.draft ? '（草稿）' : '' }}</option>
                </select>
              </label>
              <label class="hold-option"><input type="checkbox" v-model="form.hold_hand_zero" />采集期间保持灵巧手零位</label>
              <p class="muted">标记点须相对手腕固定。点在可动手指上时，请启用零位保持；刚性工具无需启用。</p>
              <div v-if="!extrinsic" class="alert warn">当前相机位置还没有生效的2D外参，请先完成2D手眼标定。</div>
              <div v-if="!hasReadyPlan" class="missing-plan">
                <span>{{ plans.length ? '当前手臂的3D计划尚未完成校验。' : '当前手臂还没有3D采集计划。' }}</span>
                <RouterLink class="btn" :to="{ name: 'plans' }">前往采集计划</RouterLink>
              </div>
              <div v-else class="setup-actions">
                <button class="btn lg" :disabled="!!busy || state?.service?.ok === false || !extrinsic || !form.camera_serial || !form.plan_id" @click="prepareCapture">下一步</button>
              </div>
            </template>
            <template v-else>
              <label class="field field-wide">已拍摄任务目录
                <select v-model="form.task_path">
                  <option value="">（请选择）</option>
                  <option v-for="task in tasks" :key="task.path" :value="task.path" :disabled="!!task.error">{{ task.name }} · {{ task.arm || '未知臂' }} · {{ task.episode_count }}组</option>
                </select>
                <small class="mono path-hint">{{ form.task_path || '当前没有可用的历史3D采集目录' }}</small>
              </label>
              <div v-if="!extrinsic" class="alert warn">当前相机位置还没有生效的2D外参；已有数据也需要它才能完成求解。</div>
              <div class="setup-actions">
                <button class="btn lg" :disabled="!!busy || !extrinsic || !form.task_path" @click="loadExisting">进入手动选点</button>
              </div>
            </template>
          </article>

          <article v-else-if="step === 'arm'" class="card">
            <h2 class="card-title">接管{{ job.arm === 'left' ? '左' : '右' }}臂并放到计划原点</h2>
            <ol class="instructions">
              <li>确认没有其他程序控制手臂，点击“接管”。</li>
              <li>使用“协力拖动”将手臂移动到计划原点附近，全程扶住手臂。</li>
              <li>点击“接住保持”，确认标记点和手部处于相机有效深度范围。</li>
              <li>在急停位置监护，然后开始自动采集。</li>
            </ol>
            <div class="actions">
              <span class="tag" :class="armEngaged ? 'ok' : ''">{{ armEngaged ? '已接管' : '未接管' }}</span>
              <button class="btn" :disabled="!!busy || armEngaged" @click="engage">接管</button>
              <button class="btn ghost" :disabled="!!busy || !armEngaged" @click="guide">协力拖动</button>
              <button class="btn ghost" :disabled="!!busy || !armEngaged" @click="catchHold">接住保持</button>
              <button class="btn ghost" :disabled="!!busy || !armEngaged" @click="disarm">解除接管</button>
            </div>
            <label class="field">运行名称（可选）<input v-model.trim="form.run_name" placeholder="留空自动生成" /></label>
            <div class="actions">
              <button class="btn lg danger" :disabled="!!busy || !armEngaged" @click="runCapture">开始自动采集（手臂将自动运动）</button>
            </div>
          </article>

          <article v-else-if="step === 'run'" class="card">
            <h2 class="card-title">3D自动采集中 · {{ job.run_id }}</h2>
            <div class="run-status"><span class="tag" :class="isRunning ? 'warn' : replay?.state === 'completed' ? 'ok' : 'bad'">{{ replay?.state || '—' }}</span><span>{{ replay?.message }}</span></div>
            <div class="progress-track"><div :style="{ width: `${Math.min(100, ((progress.route_index || 0) + 1) / Math.max(1, progress.route_count || 1) * 100)}%` }"></div></div>
            <p>{{ progress.node_name || '等待轨迹状态' }} · {{ progress.route_index ?? 0 }}/{{ progress.route_count ?? '—' }}</p>
            <div class="actions">
              <button class="btn warn" :disabled="!!busy || !isRunning || replay?.state === 'paused'" @click="pause">当前节点后暂停</button>
              <button class="btn ghost" :disabled="!!busy || replay?.state !== 'paused'" @click="resume">继续</button>
              <button class="btn danger" :disabled="!!busy || !isRunning" @click="stop">立即停止</button>
              <button class="btn" :disabled="!!busy || !runFinished" @click="enterAnnotation">{{ replay?.state === 'completed' ? '采集完成，进入手动选点' : '使用已采集数据进入选点' }}</button>
            </div>
          </article>

          <article v-else-if="step === 'annotate'" class="annotation-card">
            <div class="annotation-head">
              <div><h2>手动选点</h2><p>逐个姿态选择手部标记点并保存。进度实时落盘，可中途退出后继续。</p></div>
              <div><strong>{{ annotatedCount }}/{{ episodes.length }}</strong><span>姿态已保存选点 · 有效模型点 {{ annotation.usable_point_count || 0 }}/{{ annotation.min_points || 3 }}</span></div>
            </div>
            <iframe :key="iframeKey" :src="`${state?.ui_url || '/three-d-ui/'}?embedded=annotation`" title="3D点云手动选点操作台"></iframe>
            <div class="actions annotation-actions">
              <button class="btn ghost" :disabled="!!busy" @click="iframeKey += 1; refresh({ keepStep: true })">刷新选点进度</button>
              <button class="btn lg" :disabled="!!busy || !annotationReady" @click="finishAnnotation">选点完成，进入求解</button>
            </div>
          </article>

          <article v-else-if="step === 'solve'" class="card">
            <h2 class="card-title">求解并检查手安装结果</h2>
            <p>使用当前生效的2D相机外参，将点云选点转换到腕部坐标系，求解手安装位姿与TCP。</p>
            <table class="plain"><tbody>
              <tr><th>数据目录</th><td class="mono path-cell">{{ job.run_dir }}</td></tr>
              <tr><th>采集姿态</th><td>{{ job.sample_count ?? episodes.length }}</td></tr>
              <tr><th>已选点姿态</th><td>{{ job.annotated_count ?? annotatedCount }}</td></tr>
              <tr><th>2D外参</th><td>{{ extrinsic?.run_id || job.extrinsic_artifact_id }}</td></tr>
            </tbody></table>
            <div class="actions"><button class="btn ghost" @click="step = 'annotate'">返回修改选点</button><button class="btn lg" :disabled="!!busy" @click="solve">开始求解</button></div>
          </article>

          <article v-else class="card">
            <h2 class="card-title">结果检查与归档生效</h2>
            <div v-if="!result" class="alert warn">没有找到解算结果，请返回手动选点后重新求解。</div>
            <table v-else class="plain"><tbody>
              <tr><th>样本 / 标记点</th><td>{{ result.num_samples ?? result.sample_indices?.length ?? '—' }} / {{ result.point_count ?? result.tcp_points_wrist_m?.length ?? '—' }}</td></tr>
              <tr><th>残差</th><td>{{ result.residual_mm?.rms ?? result.residual_mm ?? '—' }} mm</td></tr>
              <tr><th>结果状态</th><td><span class="tag" :class="!mount.stale ? 'ok' : 'bad'">{{ mount.stale ? '选点已变化，需要重算' : '可归档' }}</span></td></tr>
            </tbody></table>
            <label class="field">归档运行名<input v-model.trim="archiveRunId" /></label>
            <label class="field">实体手序列号（可选）<input v-model.trim="handSerial" placeholder="用于区分同型号实体手" /></label>
            <div class="actions"><button class="btn ghost" @click="step = 'annotate'">返回修改选点</button><button class="btn lg" :disabled="!!busy || !result || mount.stale || job.finalized" @click="finalize">{{ job.finalized ? '已归档并生效' : '确认归档并生效' }}</button></div>
          </article>
        </main>

        <aside class="side-column">
          <div v-if="step !== 'annotate'" class="camera-panel">
            <CameraPreview :url="wsUrl" :board-size="boardSize" />
            <div class="camera-caption">
              <span>{{ currentRoleLabel }}</span>
              <span class="mono">{{ job.camera_serial || form.camera_serial || '未选择相机' }}</span>
            </div>
          </div>
          <div class="summary card">
            <h2 class="card-title">当前任务</h2>
            <table class="plain"><tbody>
              <tr><th>机器人</th><td>{{ config?.robot?.unit_code || '—' }}</td></tr>
              <tr><th>当前手</th><td>{{ activeHand?.name || active.hand_id || '未选择' }}</td></tr>
              <tr><th>工具手臂</th><td>{{ (job.arm || form.arm) === 'left' ? '左臂' : '右臂' }}</td></tr>
              <tr><th>借助相机</th><td>{{ currentRoleLabel }}</td></tr>
              <tr><th>2D外参</th><td><span class="tag" :class="extrinsic ? 'ok' : 'bad'">{{ extrinsic?.run_id || '未生效' }}</span></td></tr>
              <tr><th>数据来源</th><td>{{ (job.source || form.source) === 'existing' ? '已有数据' : '重新采集' }}</td></tr>
              <tr><th>采集计划</th><td>{{ job.plan_name || plans.find((item) => item.id === form.plan_id)?.name || '—' }}</td></tr>
              <tr><th>采集进度</th><td>{{ replay?.captures?.length ?? 0 }}/{{ sampleTotal ?? '—' }}</td></tr>
              <tr><th>选点进度</th><td>{{ annotatedCount }}/{{ episodes.length }}</td></tr>
            </tbody></table>
            <a v-if="state?.ui_url" class="btn ghost full" :href="state.ui_url" target="_blank" rel="noopener">新窗口打开高级操作台</a>
          </div>
        </aside>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hand-inner { max-width: 1280px; width: 100%; padding-top: 32px; }
.hold-option { display: flex; align-items: center; gap: 8px; margin-top: 18px; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.page-message { margin-bottom: 16px; }
.wizard-grid { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 20px; align-items: start; }
.wizard-grid > main { min-width: 0; }
.setup-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding-bottom: 16px; margin-bottom: 18px; border-bottom: 1px solid #e2e2e2; }
.setup-heading .card-title { margin: 0; }
.source-tabs { display: inline-flex; gap: 3px; padding: 3px; border-radius: 6px; background: #e9e9e9; }
.source-tabs button { display: inline-flex; align-items: center; justify-content: center; height: 32px; padding: 0 16px; border-radius: 4px; color: #666; }
.source-tabs button.active { background: #fff; color: #1a1a1a; box-shadow: 0 1px 3px rgb(0 0 0 / 10%); }
.field { display: flex; flex-direction: column; gap: 7px; margin-top: 16px; }
.field select, .field input { width: 100%; }
.identity-grid { display: grid; grid-template-columns: minmax(220px, 300px) 220px; gap: 14px; align-items: end; }
.field-wide { max-width: 680px; }
.path-hint { margin-top: 3px; color: #888; overflow-wrap: anywhere; }
.path-cell { overflow-wrap: anywhere; }
.missing-plan { display: flex; align-items: center; justify-content: space-between; gap: 16px; max-width: 680px; padding: 12px 14px; margin-top: 14px; border-radius: 5px; background: #fff3e0; color: #7a4b00; font-size: 13px; }
.missing-plan .btn { flex: none; }
.setup-actions { margin-top: 20px; }
.instructions { line-height: 2; color: #555; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
.run-status { display: flex; align-items: center; gap: 12px; margin: 14px 0; }
.progress-track { height: 8px; overflow: hidden; border-radius: 999px; background: #e8e8e8; }
.progress-track div { height: 100%; background: #2e7d4c; transition: width .25s ease; }
.annotation-card { overflow: hidden; border: 1px solid #dedede; border-radius: 6px; background: white; }
.annotation-head { display: flex; justify-content: space-between; gap: 20px; padding: 16px 18px; }
.annotation-head h2, .annotation-head p { margin: 0; }
.annotation-head p { margin-top: 5px; color: #777; }
.annotation-head > div:last-child { text-align: right; }
.annotation-head strong, .annotation-head span { display: block; }
.annotation-head strong { font-size: 22px; }
.annotation-card iframe { display: block; width: 100%; height: min(720px, calc(100dvh - 300px)); min-height: 520px; border: 0; border-top: 1px solid #ddd; border-bottom: 1px solid #ddd; }
.annotation-actions { justify-content: flex-end; padding: 0 18px 18px; }
.side-column { position: sticky; top: 76px; min-width: 0; }
.camera-panel { overflow: hidden; border: 1px solid #e1e1e1; border-radius: 6px; background: #fff; }
.camera-panel :deep(.preview) { border-radius: 0; }
.camera-caption { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; color: #666; font-size: 12px; }
.camera-caption .mono { overflow: hidden; text-overflow: ellipsis; }
.summary { margin-top: 14px; }
.side-column > .summary:first-child { margin-top: 0; }
.summary .full { display: flex; width: 100%; margin-top: 18px; }
@media (max-width: 1050px) {
  .wizard-grid { grid-template-columns: 1fr; }
  .side-column { position: static; }
}
@media (max-width: 700px) {
  .setup-heading { align-items: flex-start; flex-direction: column; }
  .identity-grid { grid-template-columns: 1fr; }
  .field-wide { max-width: none; }
  .annotation-card iframe { min-height: 600px; }
}
</style>
