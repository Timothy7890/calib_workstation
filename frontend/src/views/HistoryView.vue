<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useConfig } from '../composables/useConfig'
import { fmt, localTime as t } from '../utils/format'

const { config } = useConfig()
const router = useRouter()
const artifacts = ref([])
const runs = ref([])
const error = ref('')
const busy = ref(false)

async function load() {
  try {
    const [a, r] = await Promise.all([api.artifacts(), api.runs()])
    artifacts.value = a.items
    runs.value = r.runs
  } catch (e) {
    error.value = e.message
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

const TYPE_LABEL = { extrinsic: '外参', intrinsic: '内参', camera_transform: '内部相机转换' }
const STATUS = { active: ['生效', 'ok'], draft: ['已归档', ''], superseded: ['已被替代', 'warn'] }
function roleLabel(id) {
  return config.value?.cameras?.[id]?.label || id
}
</script>

<template>
  <section class="page">
    <div class="page-inner">
      <h1 class="page-title">标定记录</h1>
      <p class="page-desc">已归档的标定产物与所有 2D 采集运行。产物目录中的 manifest.json 是推送云端平台的依据。</p>
      <div v-if="error" class="alert" style="margin-bottom: 16px">{{ error }}</div>

      <h2 class="card-title">标定产物</h2>
      <table class="plain" style="margin-bottom: 36px">
        <thead>
          <tr>
            <th>类型</th>
            <th>相机</th>
            <th>运行</th>
            <th>手臂</th>
            <th>质量</th>
            <th>时间</th>
            <th>状态</th>
            <th>文件</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!artifacts.length"><td colspan="9" class="muted">还没有归档的产物</td></tr>
          <tr v-for="m in artifacts" :key="m.path">
            <td>{{ TYPE_LABEL[m.type] || m.type }}</td>
            <td>{{ roleLabel(m.camera_role) }}<div class="muted mono">{{ m.camera_serial }}</div></td>
            <td class="mono">{{ m.run_id }}</td>
            <td>{{ m.arm === 'left' ? '左' : '右' }}</td>
            <td>
              <template v-if="m.type === 'extrinsic'">
                内点 {{ m.quality?.num_inliers }} / {{ m.quality?.num_samples }}<br />
                <span class="muted">平移 {{ fmt(m.quality?.residual_translation_mm?.mean) }} mm · 旋转 {{ fmt(m.quality?.residual_rotation_deg?.mean, 3) }}°</span>
              </template>
              <template v-else>{{ m.quality?.width }}×{{ m.quality?.height }}</template>
            </td>
            <td>{{ t(m.created_at) }}</td>
            <td><span class="tag" :class="STATUS[m.status]?.[1]">{{ STATUS[m.status]?.[0] || m.status }}</span></td>
            <td>
              <a v-for="f in m.files" :key="f.name" class="file" :href="api.fileUrl(m.type, m.camera_role, m.run_id, f.name)" download>{{ f.name }}</a>
            </td>
            <td>
              <button v-if="m.status !== 'active'" class="btn ghost" :disabled="busy" @click="activate(m)">设为生效</button>
            </td>
          </tr>
        </tbody>
      </table>

      <h2 class="card-title">采集运行</h2>
      <table class="plain">
        <thead>
          <tr>
            <th>运行</th>
            <th>计划</th>
            <th>手臂</th>
            <th>结果</th>
            <th>采集</th>
            <th>开始</th>
            <th>状态</th>
            <th>目录</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!runs.length"><td colspan="9" class="muted">还没有 2D 采集运行</td></tr>
          <tr v-for="r in runs" :key="r.path">
            <td class="mono">{{ r.run_id }}</td>
            <td>{{ r.plan_name }}<div class="muted">{{ r.target }}</div></td>
            <td>{{ r.arm === 'left' ? '左' : '右' }}</td>
            <td><span class="tag" :class="r.outcome === 'completed' ? 'ok' : 'warn'">{{ r.outcome || '—' }}</span></td>
            <td>{{ r.capture_count }}</td>
            <td>{{ t(r.started_at) }}</td>
            <td>
              <span class="tag" :class="r.finalized ? 'ok' : r.solved ? '' : 'warn'">
                {{ r.finalized ? '已归档' : r.solved ? '已求解未归档' : '未求解' }}
              </span>
            </td>
            <td class="mono muted">{{ r.path }}</td>
            <td>
              <button v-if="!r.finalized && r.capture_count > 0" class="btn ghost" :disabled="busy" @click="openRun(r)">
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
.file {
  display: block;
  color: #1a1a1a;
  text-decoration: underline;
  font-size: 12.5px;
  line-height: 1.7;
}
</style>
