<script setup>
defineProps({
  steps: { type: Array, required: true }, // [{ id, label }]
  current: { type: String, required: true },
})
</script>

<template>
  <ol class="steps">
    <li
      v-for="(s, i) in steps"
      :key="s.id"
      :class="{
        done: steps.findIndex((x) => x.id === current) > i,
        active: s.id === current,
      }"
    >
      <span class="num">{{ i + 1 }}</span>
      <span class="label">{{ s.label }}</span>
    </li>
  </ol>
</template>

<style scoped>
.steps {
  display: flex;
  gap: 6px;
  margin: 0 0 28px;
  padding: 0;
  list-style: none;
}

.steps li {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 4px;
  background: #f5f5f5;
  color: #888;
  font-size: 13px;
}

.steps li.active {
  background: #1a1a1a;
  color: #fff;
}

.steps li.done {
  background: #e6f4ec;
  color: #2e7d4c;
}

.num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.08);
  font-size: 12px;
}

.steps li.active .num {
  background: rgba(255, 255, 255, 0.2);
}
</style>
