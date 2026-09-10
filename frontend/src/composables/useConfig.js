import { ref } from 'vue'
import { api } from '../api'

// 全局只拉一次 /api/config（机器人编号、下游服务对浏览器可用的地址等）
const config = ref(null)
const error = ref('')
let pending = null

function load() {
  if (pending) return pending
  pending = api
    .config()
    .then((c) => {
      config.value = c
      error.value = ''
    })
    .catch((e) => {
      error.value = e.message
    })
    .finally(() => {
      pending = null
    })
  return pending
}

export function useConfig() {
  if (!config.value && !pending) load()
  return { config, error, reload: load }
}

// 8131 的图像流地址：ws://<浏览器访问的主机>:8131/ws/stream
export function streamUrl(cfg) {
  const base = cfg?.services_public?.hand_eye_2d
  if (!base) return ''
  return base.replace(/^http/, 'ws') + '/ws/stream'
}
