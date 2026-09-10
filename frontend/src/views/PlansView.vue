<script setup>
// 采集计划编辑是高级功能：直接内嵌 18004 的编辑页面，不重做。
import { computed } from 'vue'
import { useConfig } from '../composables/useConfig'

const { config } = useConfig()
const url = computed(() => config.value?.services_public?.replay || '')
</script>

<template>
  <section class="page plans">
    <div class="bar">
      <span>采集计划（高级）</span>
      <span class="muted">在此录制/编辑轨迹节点；计划的目标（头/腰）与手臂要与标定向导中的选择一致。</span>
      <a v-if="url" class="btn ghost" :href="url" target="_blank" rel="noopener">在新窗口打开</a>
    </div>
    <iframe v-if="url" :src="url" title="采集计划编辑"></iframe>
    <div v-else class="page-inner muted">正在读取回放服务地址…</div>
  </section>
</template>

<style scoped>
.plans {
  display: flex;
  flex-direction: column;
}

.bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 32px;
  border-bottom: 1px solid #e8e8e8;
  font-size: 14px;
}

.bar .btn {
  margin-left: auto;
  height: 30px;
}

iframe {
  flex: 1;
  width: 100%;
  border: 0;
  min-height: calc(100vh - 56px - 51px);
}
</style>
