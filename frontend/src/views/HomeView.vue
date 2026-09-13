<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useConfig } from '../composables/useConfig'
import { fmt, localTime } from '../utils/format'

const { config, reload } = useConfig()
const active = ref(null)
const health = ref(null)
const error = ref('')

const roles = computed(() => Object.entries(config.value?.cameras || {}).map(([id, c]) => ({ id, ...c })))

onMounted(async () => {
  try {
    ;[active.value, health.value] = await Promise.all([api.activeArtifacts(), api.health()])
  } catch (e) {
    error.value = e.message
  }
})

async function forget(role) {
  if (!confirm('清除该位置记住的相机序列号？下次标定需重新选择。')) return
  try {
    await api.forgetCamera(role)
    await reload()
  } catch (e) {
    error.value = e.message
  }
}

function when(m) {
  return localTime(m?.activated_at)
}
</script>

<template>
  <section class="page">
    <div class="page-inner">
      <header class="hero" v-if="config">
        <div>
          <div class="muted">{{ config.robot.vendor }} · {{ config.robot.model.toUpperCase() }}</div>
          <h1 class="page-title">{{ config.robot.unit_code }}</h1>
          <p class="page-desc" style="margin-bottom: 0">
            本机器人的相机标定状态。标定产物按「机器人编号 / 类型 / 相机位置 / 运行」归档于
            <span class="mono">{{ config.unit_root }}/calibrations</span>。编号有误请点右上角切换。
          </p>
        </div>
        <RouterLink class="btn lg" :to="{ name: 'calibrate' }">开始 2D 手眼标定</RouterLink>
      </header>

      <div v-if="error" class="alert" style="margin-bottom: 16px">{{ error }}</div>
      <div v-if="health && !health.ok" class="alert warn" style="margin-bottom: 16px">
        服务未就绪：
        <span v-for="(s, k) in health.services" :key="k" v-show="!s.ok">{{ k }} — {{ s.error }}；</span>
      </div>

      <div class="cards">
        <article v-for="r in roles" :key="r.id" class="card">
          <h2 class="card-title">{{ r.label }}</h2>
          <div class="muted" style="margin-bottom: 12px">
            <template v-if="r.serial">
              相机：<span class="mono">{{ r.serial }}</span> {{ r.serial_name }}
              <span style="margin-left: 6px">{{ localTime(r.serial_set_at) }} 选定</span>
              <a href="#" style="margin-left: 8px" @click.prevent="forget(r.id)">清除</a>
            </template>
            <template v-else>相机：尚未选择（首次标定时预览并选定，之后自动记住）</template>
          </div>
          <table class="plain">
            <tbody>
              <tr>
                <th>外参</th>
                <td v-if="active?.extrinsic?.[r.id]">
                  <span class="tag ok">生效</span> {{ when(active.extrinsic[r.id]) }}
                  <div class="muted" style="margin-top: 4px">
                    {{ active.extrinsic[r.id].run_id }} · 内点 {{ active.extrinsic[r.id].quality?.num_inliers }} /
                    {{ active.extrinsic[r.id].quality?.num_samples }} · 平移残差均值
                    {{ fmt(active.extrinsic[r.id].quality?.residual_translation_mm?.mean) }} mm
                  </div>
                </td>
                <td v-else><span class="tag bad">未标定</span></td>
              </tr>
              <tr>
                <th>SDK内参存档</th>
                <td v-if="active?.intrinsic?.[r.id]">
                  <span class="tag ok">已记录</span> {{ when(active.intrinsic[r.id]) }}
                  <div class="muted" style="margin-top: 4px">
                    {{ active.intrinsic[r.id].quality?.width }}×{{ active.intrinsic[r.id].quality?.height }} ·
                    <span class="mono">{{ active.intrinsic[r.id].camera_serial }}</span>
                  </div>
                </td>
                <td v-else><span class="tag">读取自SDK</span></td>
              </tr>
              <tr>
                <th>内部相机转换</th>
                <td><span class="tag">使用SDK参数</span></td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 32px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

@media (max-width: 900px) {
  .cards {
    grid-template-columns: 1fr;
  }

  .hero {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
