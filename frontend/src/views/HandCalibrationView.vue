<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const state = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref('')
const cameraRole = ref('head')
const handSerial = ref('')
const runId = ref(defaultRunId())
const publishResult = ref(null)

function defaultRunId() {
  const d = new Date()
  const p = (v) => String(v).padStart(2, '0')
  return `mount-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

const active = computed(() => state.value?.active || {})
const mount = computed(() => state.value?.mount || {})
const result = computed(() => mount.value?.result || null)
const extrinsic = computed(() => state.value?.camera_artifacts?.extrinsic || null)
const activeHand = computed(() =>
  (state.value?.hands || []).find((item) => item.id === active.value.hand_id),
)

async function refresh() {
  error.value = ''
  try {
    state.value = await api.handCalibration()
    cameraRole.value = state.value.camera_role || cameraRole.value
  } catch (e) {
    error.value = e.message
  }
}

async function solve() {
  busy.value = 'solve'
  error.value = ''
  notice.value = ''
  try {
    await api.solveHandCalibration(cameraRole.value)
    notice.value = '解算完成。请先在点云操作台核对叠加效果，再归档并发布。'
    await refresh()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

async function finalize() {
  if (!confirm('确认当前 3D 解算结果正确，并归档为当前手安装与 TCP 标定？')) return
  busy.value = 'finalize'
  error.value = ''
  notice.value = ''
  publishResult.value = null
  try {
    publishResult.value = await api.finalizeHandCalibration({
      run_id: runId.value,
      camera_role: cameraRole.value,
      hand_serial: handSerial.value,
    })
    notice.value = '已拆分归档 hand_mount 与 tcp_profile，并登记、绑定到 18000。'
    runId.value = defaultRunId()
    await refresh()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = ''
  }
}

onMounted(refresh)
</script>

<template>
  <section class="page hand-calibration">
    <div class="toolbar">
      <div>
        <strong>3D 手安装 / TCP 标定</strong>
        <span class="muted">当前组合：{{ active.arm || '未选择臂' }} · {{ activeHand?.name || active.hand_id || '未选择手' }} · {{ cameraRole }}</span>
      </div>
      <a v-if="state?.ui_url" class="btn ghost" :href="state.ui_url" target="_blank" rel="noopener">新窗口打开操作台</a>
      <button class="btn ghost" :disabled="!!busy" @click="refresh">刷新</button>
    </div>

    <div v-if="error" class="alert page-message">{{ error }}</div>
    <div v-if="notice" class="alert info page-message">{{ notice }}</div>
    <div v-if="publishResult?.cloud_error" class="alert warn page-message">
      本地归档及 18000 绑定成功，但云端同步失败：{{ publishResult.cloud_error }}。可在“标定记录”中重试。
    </div>
    <div v-if="state && !state.service.ok" class="alert warn page-message">
      内置3D引擎未就绪：{{ state.service.error }}
    </div>

    <div class="workspace">
      <iframe v-if="state?.service.ok && state?.ui_url" :src="state.ui_url" title="18005 内置3D点云与手安装标定操作台"></iframe>
      <div v-else class="empty">3D 操作台未就绪</div>

      <aside>
        <article class="card">
          <h2 class="card-title">发布前检查</h2>
          <table class="plain">
            <tbody>
              <tr><th>18000 激活臂</th><td>{{ active.arm || '未选择' }}</td></tr>
              <tr><th>实体手</th><td>{{ activeHand?.name || active.hand_id || '未选择' }}</td></tr>
              <tr><th>相机位置</th><td>
                <select v-model="cameraRole">
                  <option value="head">head</option>
                  <option value="waist">waist</option>
                </select>
              </td></tr>
              <tr><th>2D 外参</th><td>
                <span class="tag" :class="extrinsic ? 'ok' : 'bad'">{{ extrinsic ? extrinsic.run_id : '未生效' }}</span>
              </td></tr>
              <tr><th>3D 结果</th><td>
                <span class="tag" :class="result && !mount.stale ? 'ok' : 'bad'">
                  {{ !result ? '未解算' : mount.stale ? '已过期' : '可归档' }}
                </span>
              </td></tr>
              <tr v-if="result"><th>样本 / 点</th><td>{{ result.num_samples ?? result.sample_indices?.length ?? '—' }} / {{ result.point_count ?? result.tcp_points_wrist_m?.length ?? '—' }}</td></tr>
              <tr v-if="result"><th>残差</th><td>{{ result.residual_mm?.rms ?? result.residual_mm ?? '—' }} mm</td></tr>
            </tbody>
          </table>
          <button class="btn action" :disabled="!!busy || !state?.service.ok || !extrinsic" @click="solve">
            {{ busy === 'solve' ? '正在解算…' : '用当前 2D 外参解算' }}
          </button>
        </article>

        <article class="card">
          <h2 class="card-title">归档并发布</h2>
          <label class="field">运行名<input v-model.trim="runId" /></label>
          <label class="field">实体手序列号（可选）<input v-model.trim="handSerial" placeholder="用于区分同型号实体手" /></label>
          <p class="muted">发布后生成独立的 hand_mount 与 tcp_profile，关联当前相机产物，并原子绑定至 18000 当前组合。</p>
          <button class="btn action" :disabled="!!busy || !result || mount.stale || !extrinsic || !runId" @click="finalize">
            {{ busy === 'finalize' ? '正在归档发布…' : '确认归档并发布' }}
          </button>
        </article>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.hand-calibration {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 56px);
  height: calc(100dvh - 56px);
  min-height: 0;
  overflow: hidden;
}
.toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid #e8e8e8; }
.toolbar > div { display: flex; flex-direction: column; gap: 3px; margin-right: auto; }
.page-message { margin: 10px 24px 0; }
.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 16px;
  padding: 16px 24px 24px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
iframe, .empty {
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 1px solid #e4e4e4;
  border-radius: 6px;
  background: #fafafa;
}
.empty { display: grid; place-items: center; color: #888; }
aside { display: flex; flex-direction: column; gap: 16px; min-height: 0; overflow-y: auto; }
.card { background: #f7f7f7; }
.card .field + .field { margin-top: 12px; }
.action { width: 100%; margin-top: 16px; }
select { min-width: 110px; padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px; background: white; }
@media (max-width: 980px) {
  .hand-calibration { height: auto; min-height: calc(100dvh - 56px); overflow: visible; }
  .workspace { grid-template-columns: 1fr; overflow: visible; }
  iframe, .empty { height: auto; min-height: 600px; }
  aside { overflow: visible; }
}
</style>
