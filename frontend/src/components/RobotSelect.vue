<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useConfig } from '../composables/useConfig'
import { localTime } from '../utils/format'

const props = defineProps({
  // gate：首次进入、还没有编号时占满页面；dialog：右上角「切换」弹出
  mode: { type: String, default: 'gate' },
})
const emit = defineEmits(['done', 'cancel'])

const { config, reload } = useConfig()
const info = ref(null)
const code = ref('')
const busy = ref(false)
const error = ref('')
const input = ref(null)

onMounted(async () => {
  try {
    info.value = await api.robot()
    code.value = info.value.unit_code || ''
  } catch (e) {
    error.value = e.message
  }
  input.value?.focus()
})

function pick(c) {
  code.value = c
}

async function submit() {
  const value = code.value.trim()
  if (!value) {
    error.value = '请输入机器人编号'
    return
  }
  busy.value = true
  error.value = ''
  try {
    await api.setRobot(value)
    await reload()
    emit('done', value)
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div :class="mode === 'gate' ? 'gate' : 'dialog-mask'" @click.self="mode === 'dialog' && emit('cancel')">
    <section class="card panel">
      <div class="muted" v-if="config">{{ config.robot.vendor }} · {{ config.robot.model.toUpperCase() }}</div>
      <h1 class="page-title">{{ mode === 'gate' ? '这台机器人的编号是？' : '切换机器人' }}</h1>
      <p class="page-desc">
        标定产物按编号归档到独立目录，编号写错会存到别的机器人名下。编号印在机器人铭牌上，如
        <span class="mono">H2-1336</span>。
      </p>

      <form class="row" @submit.prevent="submit">
        <label class="field grow">
          <span>机器人编号</span>
          <input
            ref="input"
            v-model="code"
            type="text"
            class="mono"
            placeholder="H2-1336"
            autocomplete="off"
            spellcheck="false"
            :disabled="busy"
          />
        </label>
        <button class="btn" type="submit" :disabled="busy">{{ busy ? '保存中…' : '确定' }}</button>
        <button v-if="mode === 'dialog'" class="btn" type="button" :disabled="busy" @click="emit('cancel')">取消</button>
      </form>

      <div v-if="info?.known?.length" class="known">
        <span class="muted">本机已有记录：</span>
        <button v-for="c in info.known" :key="c" type="button" class="chip mono" :class="{ on: c === code }" @click="pick(c)">
          {{ c }}
        </button>
      </div>

      <div v-if="error" class="alert" style="margin-top: 16px">{{ error }}</div>
      <p v-if="info?.unit_code && mode === 'dialog'" class="muted" style="margin-top: 16px">
        当前：<span class="mono">{{ info.unit_code }}</span>（{{ localTime(info.set_at) }} 设置）
      </p>
    </section>
  </div>
</template>

<style scoped>
.gate {
  flex: 1;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 72px 24px;
}

.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 96px;
  background: rgba(0, 0, 0, 0.28);
}

.panel {
  width: 560px;
  max-width: 100%;
}

.row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}

.grow {
  flex: 1;
}

.row input {
  font-size: 18px;
  letter-spacing: 0.04em;
}

.known {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 20px;
  font-size: 13px;
}

.chip {
  padding: 4px 12px;
  border: 1px solid #ddd;
  border-radius: 999px;
  background: #fff;
  font-size: 13px;
  cursor: pointer;
}

.chip:hover {
  border-color: #999;
}

.chip.on {
  border-color: #1a1a1a;
  background: #1a1a1a;
  color: #fff;
}
</style>
