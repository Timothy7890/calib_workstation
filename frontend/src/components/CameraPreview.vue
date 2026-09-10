<script setup>
// 8131 /ws/stream 实时图像（左目，带角点叠加）。
import { onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  url: { type: String, default: '' },
  boardSize: { type: String, default: '' },
})

const src = ref('')
const detected = ref(false)
const connected = ref(false)
const count = ref(0)
let ws = null
let retry = null

function connect() {
  close()
  if (!props.url) return
  try {
    ws = new WebSocket(props.url)
  } catch {
    scheduleRetry()
    return
  }
  ws.onopen = () => {
    connected.value = true
    if (props.boardSize) ws.send(JSON.stringify({ board_size: props.boardSize, show_corners: true }))
  }
  ws.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data)
      if (d.left) src.value = 'data:image/jpeg;base64,' + d.left
      detected.value = !!d.left_detected
      count.value = d.count ?? count.value
    } catch {
      /* ignore */
    }
  }
  ws.onclose = () => {
    connected.value = false
    scheduleRetry()
  }
  ws.onerror = () => {
    connected.value = false
  }
}

function scheduleRetry() {
  clearTimeout(retry)
  retry = setTimeout(connect, 1500)
}

function close() {
  clearTimeout(retry)
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
}

watch(() => props.url, connect)
onMounted(connect)
onUnmounted(close)

defineExpose({ detected, connected })
</script>

<template>
  <div class="preview">
    <img v-if="src" :src="src" alt="相机画面" />
    <div v-else class="placeholder">
      <span v-if="!url">未配置图像流</span>
      <span v-else-if="!connected">正在连接相机画面…</span>
      <span v-else>等待相机帧（相机未连接或未选择）</span>
    </div>
    <div class="overlay">
      <span class="tag" :class="detected ? 'ok' : 'warn'">{{ detected ? '已检出棋盘格' : '未检出棋盘格' }}</span>
      <span class="tag">{{ connected ? '画面在线' : '画面离线' }}</span>
    </div>
  </div>
</template>

<style scoped>
.preview {
  position: relative;
  width: 100%;
  aspect-ratio: 4 / 3;
  background: #111;
  border-radius: 6px;
  overflow: hidden;
}

.preview img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #999;
  font-size: 13px;
}

.overlay {
  position: absolute;
  left: 10px;
  bottom: 10px;
  display: flex;
  gap: 8px;
}
</style>
