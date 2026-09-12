<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import logo from './assets/bwton-logo.png'
import { api } from './api'
import { useConfig } from './composables/useConfig'
import RobotSelect from './components/RobotSelect.vue'

const { config, error: configError } = useConfig()
const health = ref(null)
const switching = ref(false)
let timer = null

async function refreshHealth() {
  try {
    health.value = await api.health()
  } catch (e) {
    health.value = { ok: false, services: {}, error: e.message }
  }
}

onMounted(() => {
  refreshHealth()
  timer = setInterval(refreshHealth, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="layout">
    <header class="topbar">
      <RouterLink to="/" class="brand">
        <img :src="logo" alt="八维通 BWTON" class="brand-logo" />
        <span class="brand-title">相机标定工作站</span>
      </RouterLink>
      <nav class="nav">
        <RouterLink :to="{ name: 'home' }" class="nav-link" exact-active-class="is-active">总览</RouterLink>
        <RouterLink :to="{ name: 'calibrate' }" class="nav-link" active-class="is-active">开始标定</RouterLink>
        <RouterLink :to="{ name: 'hand-calibration' }" class="nav-link" active-class="is-active">3D 手/TCP</RouterLink>
        <RouterLink :to="{ name: 'history' }" class="nav-link" active-class="is-active">标定记录</RouterLink>
        <RouterLink :to="{ name: 'plans' }" class="nav-link" active-class="is-active">采集计划</RouterLink>
        <RouterLink :to="{ name: 'guide' }" class="nav-link" active-class="is-active">使用说明</RouterLink>
      </nav>
      <div class="status">
        <button v-if="config?.robot.unit_code" type="button" class="unit" title="切换机器人" @click="switching = true">
          {{ config.robot.unit_code }} <span class="unit-switch">切换</span>
        </button>
        <span v-if="health" class="dot" :class="health.ok ? 'ok' : 'bad'" :title="health.error || ''"></span>
        <span v-if="health" class="svc">
          <span :class="health.services.hand_eye_2d?.ok ? 'ok' : 'bad'">相机</span>
          <span :class="health.services.hand_eye_3d?.ok ? 'ok' : 'idle'">3D</span>
          <span :class="health.services.replay?.ok ? 'ok' : 'bad'">回放</span>
          <span :class="health.services.capability?.ok ? 'ok' : 'bad'">能力中心</span>
        </span>
      </div>
    </header>
    <main class="content">
      <div v-if="configError" class="alert" style="margin: 32px">{{ configError }}</div>
      <RobotSelect v-else-if="config && !config.robot.unit_code" mode="gate" />
      <RouterView v-else-if="config" :key="config.robot.unit_code" />
    </main>
    <RobotSelect v-if="switching" mode="dialog" @done="switching = false" @cancel="switching = false" />
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 32px;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
}

.brand {
  display: flex;
  align-items: center;
  gap: 14px;
  height: 100%;
  margin-right: 40px;
}

.brand-logo {
  height: 32px;
  display: block;
  filter: invert(1);
}

.brand-title {
  font-size: 15px;
  font-weight: 500;
  color: #1a1a1a;
}

.nav {
  display: flex;
  align-items: stretch;
  height: 100%;
  gap: 8px;
}

.nav-link {
  position: relative;
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 12px;
  font-size: 14px;
  color: #333;
}

.nav-link:hover {
  color: #000;
}

.nav-link.is-active {
  color: #000;
  font-weight: 500;
}

.nav-link.is-active::after {
  content: '';
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 0;
  height: 2px;
  background: #1a1a1a;
}

.status {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: #666;
}

.unit {
  font-weight: 500;
  color: #1a1a1a;
  font-size: 13px;
  padding: 4px 10px;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
}

.unit:hover {
  border-color: #999;
}

.unit-switch {
  margin-left: 6px;
  font-weight: 400;
  color: #888;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #bbb;
}

.dot.ok {
  background: #2e9d5b;
}

.dot.bad {
  background: #d64545;
}

.svc {
  display: flex;
  gap: 8px;
}

.svc span {
  padding: 2px 8px;
  border-radius: 999px;
  background: #f0f0f0;
  font-size: 12px;
}

.svc span.ok {
  color: #2e7d4c;
  background: #e6f4ec;
}

.svc span.bad {
  color: #b23b3b;
  background: #fbe9e9;
}

.svc span.idle {
  color: #777;
}

.content {
  flex: 1;
  display: flex;
  min-height: 0;
}
</style>
