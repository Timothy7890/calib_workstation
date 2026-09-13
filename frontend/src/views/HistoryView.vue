<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useConfig } from '../composables/useConfig'
import { fmt, localTime as t } from '../utils/format'

const { config } = useConfig()
const router = useRouter()
const artifacts = ref([])
// 内参只是 SDK 值的存档（随外参一并归档、一并生效），默认不在列表里显示
const showIntrinsic = ref(false)
const shownArtifacts = computed(() => artifacts.value.filter((m) => showIntrinsic.value || m.type !== 'intrinsic'))
const intrinsicCount = computed(() => artifacts.value.filter((m) => m.type === 'intrinsic').length)
const runs = ref([])
const error = ref('')
const busy = ref(false)

async function load() {
  // 三块数据互不依赖：18004 不可达时产物列表与云端设置照常显示
  const [a, r, c] = await Promise.allSettled([api.artifacts(), api.runs(), api.cloud()])
  if (a.status === 'fulfilled') artifacts.value = a.value.items
  if (r.status === 'fulfilled') runs.value = r.value.runs
  if (c.status === 'fulfilled') applyCloud(c.value)
  const failed = [a, r, c].filter((x) => x.status === 'rejected').map((x) => x.reason.message)
  error.value = failed.join('；')
}

// ---- 云端推送 ----
const cloud = ref(null) // {settings, counts, last}
const cloudOpen = ref(false)
const cloudForm = ref({ url: '', token: '', auto_push: true })
const cloudBusy = ref('')
const cloudMsg = ref('')
const cloudErr = ref('')
const pendingCount = computed(() => (cloud.value ? cloud.value.counts.pending + cloud.value.counts.stale : 0))

function applyCloud(c) {
  cloud.value = c
  cloudForm.value = { url: c.settings.url || '', token: '', auto_push: c.settings.auto_push }
  if (!c.settings.configured) cloudOpen.value = true
}

async function saveCloud() {
  cloudBusy.value = 'save'
  cloudErr.value = ''
  cloudMsg.value = ''
  try {
    const payload = { url: cloudForm.value.url, auto_push: cloudForm.value.auto_push }
    if (cloudForm.value.token) payload.token = cloudForm.value.token
    applyCloud(await api.setCloud(payload))
    cloudMsg.value = '已保存'
  } catch (e) {
    cloudErr.value = e.message
  } finally {
    cloudBusy.value = ''
  }
}

async function testCloud() {
  cloudBusy.value = 'test'
  cloudErr.value = ''
  cloudMsg.value = ''
  try {
    const r = await api.testCloud()
    cloudMsg.value = `连接正常（云端版本 ${r.health.version}，token 有效）`
  } catch (e) {
    cloudErr.value = e.message
  } finally {
    cloudBusy.value = ''
  }
}

async function syncCloud(force = false) {
  if (force && !confirm('把当前机器人的全部归档产物重新推送一遍？（云端按运行名覆盖，不会产生重复）')) return
  cloudBusy.value = 'sync'
  cloudErr.value = ''
  cloudMsg.value = ''
  try {
    const r = await api.syncCloud(force)
    cloudMsg.value = `已推送 ${r.pushed.length} 项${r.failed.length ? `，失败 ${r.failed.length} 项` : ''}${r.skipped ? `，${r.skipped} 项无需同步` : ''}`
    if (r.failed.length) cloudErr.value = r.failed.map((f) => `${f.run_id}: ${f.error}`).join('；')
    await load()
  } catch (e) {
    cloudErr.value = e.message
  } finally {
    cloudBusy.value = ''
  }
}

async function pushOne(m) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.pushArtifact(m.type, m.camera_role, m.run_id)
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

const SYNC = { synced: ['已同步', 'ok'], stale: ['状态待同步', 'warn'], pending: ['未推送', ''] }
function remoteUrl(m) {
  const base = cloud.value?.settings?.url
  return base ? `${base}/robots/${encodeURIComponent(m.unit_code)}/${m.type === 'camera_transform' ? 'camera-transform' : m.type}` : ''
}

async function removeArtifact(m) {
  if (busy.value) return
  const note = m.status === 'active' ? '\n它当前是生效项，删除后该相机位置将回到"未标定"。' : ''
  if (!confirm(`删除 ${roleLabel(m.camera_role, m)} · ${m.run_id} 的归档产物（外参 + 内参）？${note}\n原始采集数据保留，之后仍可在下方"采集运行"里重新归档。`)) return
  busy.value = true
  error.value = ''
  try {
    const r = await api.deleteArtifact(m.type, m.camera_role, m.run_id)
    if (r.cloud_error) error.value = `本地已删除，但云端删除失败：${r.cloud_error}`
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

async function activate(m) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.activate(m.type, m.camera_role, m.run_id)
    await load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

/** 把历史运行装入当前任务，跳到向导的求解 / 结果步骤 */
async function openRun(r) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.loadRun(r.run_id, r.arm)
    router.push({ name: 'calibrate' })
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

onMounted(load)

const TYPE_LABEL = {
  extrinsic: '外参',
  intrinsic: 'SDK内参存档',
  camera_transform: '内部相机转换',
  hand_mount: '灵巧手安装',
  tcp_profile: 'TCP 配置',
}
const TARGET_LABEL = { hand_eye_2D_head: '2D 头部', hand_eye_2D_waist: '2D 腰部' }
const OUTCOME = { completed: '完成', stopped: '已停止', fault: '故障' }

/** 只显示 runs/<arm>/<run_id> 这一段，完整路径靠复制 */
function shortPath(p) {
  const parts = String(p || '').split('/').filter(Boolean)
  return parts.slice(-3).join('/')
}

const copied = ref('')
let copiedTimer = null
async function copyPath(p) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(p)
    } else {
      // 非 https / 旧浏览器：退回 execCommand
      const ta = document.createElement('textarea')
      ta.value = p
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copied.value = p
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => (copied.value = ''), 1500)
  } catch (e) {
    error.value = '复制失败：' + e.message
  }
}
const STATUS = { active: ['生效', 'ok'], draft: ['已归档', ''], superseded: ['已被替代', 'warn'] }
function roleLabel(id, m) {
  return config.value?.cameras?.[id]?.label || m?.camera_label || id
}
</script>

<template>
  <section class="page">
    <div class="page-inner">
      <h1 class="page-title">标定记录</h1>
      <p class="page-desc">已归档的标定产物与所有 2D 采集运行。产物目录中的 manifest.json 是推送云端平台的依据。</p>
      <div v-if="error" class="alert" style="margin-bottom: 16px">{{ error }}</div>

      <section v-if="cloud" class="card cloud">
        <div class="row-head">
          <h2 class="card-title" style="margin: 0">云端同步</h2>
          <div class="cloud-summary">
            <template v-if="cloud.settings.configured">
              <span class="mono muted">{{ cloud.settings.url }}</span>
              <span class="tag" :class="pendingCount ? 'warn' : 'ok'">{{ pendingCount ? `${pendingCount} 项待同步` : '全部已同步' }}</span>
              <button class="btn ghost sm" :disabled="!!cloudBusy || !pendingCount" @click="syncCloud(false)">{{ cloudBusy === 'sync' ? '推送中…' : '立即同步' }}</button>
            </template>
            <span v-else class="tag warn">未配置</span>
            <button class="btn ghost sm" @click="cloudOpen = !cloudOpen">{{ cloudOpen ? '收起' : '设置' }}</button>
          </div>
        </div>
        <div v-if="cloudOpen" class="cloud-form">
          <label class="field"><span>云端地址</span><input v-model="cloudForm.url" placeholder="https://bwt.example.com" spellcheck="false" /></label>
          <label class="field"><span>推送 token</span><input v-model="cloudForm.token" type="password" :placeholder="cloud.settings.token_set ? `已保存（${cloud.settings.token_masked}），留空不改` : '云端 .env 里的 CALIB_API_TOKEN'" spellcheck="false" autocomplete="off" /></label>
          <label class="check"><input v-model="cloudForm.auto_push" type="checkbox" /> 归档 / 切换生效后自动推送</label>
          <div class="cloud-actions">
            <button class="btn sm" :disabled="!!cloudBusy" @click="saveCloud">保存</button>
            <button class="btn ghost sm" :disabled="!!cloudBusy || !cloud.settings.configured" @click="testCloud">{{ cloudBusy === 'test' ? '测试中…' : '测试连接' }}</button>
            <button class="btn ghost sm" :disabled="!!cloudBusy || !cloud.settings.configured" @click="syncCloud(true)">全部重推</button>
            <span class="muted small">保存在 <span class="mono">{{ cloud.settings.path }}</span></span>
          </div>
        </div>
        <div v-if="cloudMsg" class="muted" style="margin-top: 8px">{{ cloudMsg }}</div>
        <div v-if="cloudErr" class="alert" style="margin-top: 8px">{{ cloudErr }}</div>
        <div v-else-if="cloud.last?.at && cloud.last.failed?.length" class="alert warn" style="margin-top: 8px">
          上次自动推送有 {{ cloud.last.failed.length }} 项失败：{{ cloud.last.error }}
        </div>
      </section>

      <div class="row-head">
        <h2 class="card-title">标定产物</h2>
        <label v-if="intrinsicCount" class="muted small"><input v-model="showIntrinsic" type="checkbox" /> 显示内参存档（SDK 内参，{{ intrinsicCount }} 份）</label>
      </div>
      <table class="plain runs" style="margin-bottom: 36px">
        <thead>
          <tr>
            <th>类型</th>
            <th>相机</th>
            <th>运行</th>
            <th>手臂</th>
            <th>质量</th>
            <th>时间</th>
            <th>状态</th>
            <th>云端</th>
            <th>文件</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!shownArtifacts.length"><td colspan="10" class="muted">还没有归档的产物</td></tr>
          <tr v-for="m in shownArtifacts" :key="m.path">
            <td class="nowrap">{{ TYPE_LABEL[m.type] || m.type }}</td>
            <td>{{ m.subject?.tool_id || roleLabel(m.camera_role, m) }}<div class="muted mono">{{ m.camera_serial }}</div></td>
            <td class="mono nowrap">{{ m.run_id }}</td>
            <td>{{ ['left', 'left_arm'].includes(m.arm) ? '左' : '右' }}</td>
            <td class="nowrap">
              <template v-if="m.type === 'extrinsic'">
                内点 {{ m.quality?.num_inliers }} / {{ m.quality?.num_samples }}<br />
                <span class="muted">平移 {{ fmt(m.quality?.residual_translation_mm?.mean) }} mm · 旋转 {{ fmt(m.quality?.residual_rotation_deg?.mean, 3) }}°</span>
              </template>
              <template v-else-if="m.subject?.kind === 'tool'">RMS {{ fmt(m.quality?.rms) }} mm</template>
              <template v-else>{{ m.quality?.width }}×{{ m.quality?.height }}</template>
            </td>
            <td class="nowrap mono">{{ t(m.created_at) }}</td>
            <td><span class="tag" :class="STATUS[m.status]?.[1]">{{ STATUS[m.status]?.[0] || m.status }}</span></td>
            <td class="nowrap">
              <span v-if="m.local_only" class="tag">本地归档</span>
              <a v-else-if="m.sync_state === 'synced' && remoteUrl(m)" class="tag ok link" :href="remoteUrl(m)" target="_blank" rel="noopener" :title="'云端查看 · ' + t(m.cloud?.pushed_at)">已同步 ↗</a>
              <span v-else class="tag" :class="SYNC[m.sync_state]?.[1]">{{ SYNC[m.sync_state]?.[0] || m.sync_state }}</span>
            </td>
            <td class="nowrap files">
              <a v-for="f in m.files" :key="f.name" class="file" :href="api.fileUrl(m.type, m.camera_role, m.run_id, f.name)" download>{{ f.name }}</a>
            </td>
            <td class="nowrap actions-cell">
              <button v-if="!m.local_only && m.status !== 'active'" class="btn ghost sm" :disabled="busy" @click="activate(m)">设为生效</button>
              <button v-if="!m.local_only && m.sync_state !== 'synced' && cloud?.settings?.configured" class="btn ghost sm" :disabled="busy" @click="pushOne(m)">推送</button>
              <button class="btn ghost sm danger-text" :disabled="busy" @click="removeArtifact(m)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>

      <h2 class="card-title">采集运行</h2>
      <table class="plain runs">
        <thead>
          <tr>
            <th>运行</th>
            <th>计划</th>
            <th>手臂</th>
            <th>结果</th>
            <th class="num">采集</th>
            <th>开始</th>
            <th>状态</th>
            <th>目录</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!runs.length"><td colspan="9" class="muted">还没有 2D 采集运行</td></tr>
          <tr v-for="r in runs" :key="r.path">
            <td class="mono nowrap">{{ r.run_id }}</td>
            <td class="plan"><span class="ellipsis" :title="r.plan_name">{{ r.plan_name }}</span><div class="muted">{{ TARGET_LABEL[r.target] || r.target }}</div></td>
            <td class="nowrap">{{ r.arm === 'left' ? '左' : '右' }}</td>
            <td class="nowrap"><span class="tag" :class="r.outcome === 'completed' ? 'ok' : 'warn'">{{ OUTCOME[r.outcome] || r.outcome || '—' }}</span></td>
            <td class="num">{{ r.capture_count }}</td>
            <td class="nowrap mono">{{ t(r.started_at) }}</td>
            <td class="nowrap">
              <span class="tag" :class="r.finalized ? 'ok' : r.solved ? '' : 'warn'">
                {{ r.finalized ? '已归档' : r.solved ? '已求解未归档' : '未求解' }}
              </span>
            </td>
            <td class="nowrap">
              <button class="path" :title="'点击复制完整路径\n' + r.path" @click="copyPath(r.path)">
                <span class="mono">{{ shortPath(r.path) }}</span>
                <span class="copy-hint">{{ copied === r.path ? '已复制' : '复制' }}</span>
              </button>
            </td>
            <td class="nowrap">
              <button v-if="!r.finalized && r.capture_count > 0" class="btn ghost sm" :disabled="busy" @click="openRun(r)">
                {{ r.solved ? '查看结果 / 归档' : '去求解' }}
              </button>
              <span v-else-if="!r.finalized" class="muted">无样本</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.cloud {
  margin-bottom: 28px;
}
.cloud-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.cloud-form {
  margin-top: 14px;
  display: grid;
  gap: 10px;
  max-width: 720px;
}
.cloud-form .field > span {
  display: block;
  font-size: 12.5px;
  color: #666;
  margin-bottom: 4px;
}
.cloud-form .field > input {
  width: 100%;
}
.check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}
.cloud-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.tag.link {
  text-decoration: none;
  cursor: pointer;
}
.runs {
  table-layout: auto;
}
.runs .nowrap {
  white-space: nowrap;
}
.runs .num {
  text-align: right;
  white-space: nowrap;
}
.runs .plan {
  max-width: 220px;
}
.runs .ellipsis {
  display: block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.runs .btn.sm {
  height: 30px;
  padding: 0 12px;
  font-size: 13px;
  white-space: nowrap;
}
.path {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  border: 1px dashed #d5d5d5;
  border-radius: 4px;
  background: #fafafa;
  color: #333;
  font-size: 12.5px;
  cursor: pointer;
}
.path:hover {
  border-color: #1a1a1a;
  background: #f0f0f0;
}
.path .copy-hint {
  font-size: 11.5px;
  color: #888;
}
.row-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
}
.row-head .small {
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.actions-cell .btn + .btn {
  margin-left: 6px;
}
.danger-text {
  color: #b42318;
  border-color: #e6b3ad;
}
.danger-text:hover {
  background: #fdf1ef;
}
.file {
  display: inline-block;
  color: #1a1a1a;
  text-decoration: underline;
  font-size: 12.5px;
  line-height: 1.7;
}
.files .file + .file {
  margin-left: 14px;
}
</style>
