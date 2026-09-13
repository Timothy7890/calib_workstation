<script setup>
// 18005 /ws/stream 实时图像。统一工作站发送二进制 JPEG，同时兼容旧服务的 JSON/Base64。
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
let objectUrl = ''

function showBinaryFrame(data) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: 'image/jpeg' })
  const nextUrl = URL.createObjectURL(blob)
  const previousUrl = objectUrl
  objectUrl = nextUrl
  src.value = nextUrl
  if (previousUrl) URL.revokeObjectURL(previousUrl)
}

function connect() {
  close()
  if (!props.url) return
  try {
    ws = new WebSocket(props.url)
    ws.binaryType = 'arraybuffer'
  } catch {
    scheduleRetry()
    return
  }
  ws.onopen = () => {
    connected.value = true
    if (props.boardSize) ws.send(JSON.stringify({ board_size: props.boardSize, show_corners: true }))
  }
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') {
      showBinaryFrame(ev.data)
      return
    }
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
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
  src.value = ''
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
  </div>
</template>

<style scoped>
.preview {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
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

</style>
