<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'
const props = defineProps({ job: Object, episodes: Array })
const emit = defineEmits(['updated'])
const episode = ref(''), point = ref(''), error = ref(''), busy = ref(false)
const width = ref(1), height = ref(1)
const points = computed(() => props.job.object_mode === 'tcp'
  ? [{ id: 'tcp', label: 'TCP实体点' }]
  : [{ id: 'origin', label: '原点 / TCP' }, { id: 'x', label: 'X正方向点' }, { id: 'xy', label: 'XY平面正Y侧点' }])
const samples = computed(() => props.job.tool_samples || [])
const current = computed(() => samples.value.filter(s => s.episode === episode.value))
watch(() => props.episodes, () => {
  if (!props.episodes.some(e => e.name === episode.value)) episode.value = props.episodes[0]?.name || ''
}, { immediate: true })
watch(points, () => { point.value = points.value[0].id }, { immediate: true })
async function pick(event) {
  if (busy.value) return
  const img = event.currentTarget, rect = img.getBoundingClientRect()
  if (!img.naturalWidth) return
  const u = Math.min(img.naturalWidth - 1, Math.max(0, Math.floor((event.clientX - rect.left) * img.naturalWidth / rect.width)))
  const v = Math.min(img.naturalHeight - 1, Math.max(0, Math.floor((event.clientY - rect.top) * img.naturalHeight / rect.height)))
  busy.value = true; error.value = ''
  try {
    const response = await api.pickToolPoint({ episode: episode.value, point_id: point.value, u, v })
    emit('updated', response.job)
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function remove(s) {
  busy.value = true; error.value = ''
  try { emit('updated', (await api.removeToolPoint(s)).job) }
  catch (e) { error.value = e.message } finally { busy.value = false }
}
</script>

<template>
  <div class="tool-picker">
    <p>在不同采集姿态中点击同一个实体特征点；每个点至少覆盖3个不同姿态。点击后读取深度并自动保存，再点同一槽位可覆盖。</p>
    <p v-if="job.object_mode === 'tool'">工具原点同时作为TCP；从原点到X方向点定义正X轴，第三点确定XY平面的正Y侧。三个点不能共线。</p>
    <p v-else>单点TCP只输出腕坐标系中的位置，不定义工具朝向。</p>
    <div class="controls">
      <label class="field">采集姿态<select v-model="episode" :disabled="busy"><option v-for="e in episodes" :key="e.name" :value="e.name">{{ e.name }}</option></select></label>
      <label class="field">当前特征点<select v-model="point" :disabled="busy"><option v-for="p in points" :key="p.id" :value="p.id">{{ p.label }}</option></select></label>
    </div>
    <div v-if="error" class="alert warn">{{ error }}</div>
    <div v-if="episode" class="image-wrap">
      <img :key="episode" :src="`/three-d/api/offline/episodes/${encodeURIComponent(episode)}/preview`"
        alt="点击工具特征点" @click="pick" @error="error = '图像加载失败，请检查数据目录后重新选择姿态'" @load="width = $event.target.naturalWidth; height = $event.target.naturalHeight; error = ''" />
      <span v-for="s in current" :key="s.point_id" class="marker" :style="{ left: `${s.pixel[0] / width * 100}%`, top: `${s.pixel[1] / height * 100}%` }">{{ s.point_id }}</span>
    </div>
    <p v-if="busy">正在处理深度，请稍候…</p>
    <div class="controls"><span v-for="p in points" :key="p.id">{{ p.label }}：{{ samples.filter(s => s.point_id === p.id).length }} 个姿态</span></div>
    <div class="controls"><button v-for="s in current" :key="s.point_id" class="btn ghost" :disabled="busy" @click="remove(s)">删除当前姿态的 {{ s.point_id }}</button></div>
  </div>
</template>

<style scoped>
.tool-picker { padding: 20px; }
.tool-picker p { color: #666; line-height: 1.7; }
.controls { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; margin: 16px 0; }
.image-wrap { position: relative; max-width: 960px; }
.image-wrap img { display: block; width: 100%; cursor: crosshair; border-radius: 6px; }
.marker { position: absolute; transform: translate(-50%, -50%); pointer-events: none; border: 2px solid white; border-radius: 4px; padding: 2px; color: white; background: #ac3300; font-size: 12px; }
</style>
