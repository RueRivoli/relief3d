<script setup>
import { computed } from 'vue'

const props = defineProps({
  job: { type: Object, required: true },
})

const STEPS = [
  { key: 'prepare', label: 'Preparing images' },
  { key: 'features', label: 'Detecting features' },
  { key: 'matching', label: 'Matching photos' },
  { key: 'mapping', label: 'Recovering cameras & structure' },
  { key: 'dense', label: 'Dense depth reconstruction' },
  { key: 'export', label: 'Exporting scene' },
]

const steps = computed(() => STEPS.filter((s) => s.key !== 'dense' || props.job.dense))
const currentIndex = computed(() => steps.value.findIndex((s) => s.key === props.job.stage))

function state(i) {
  if (props.job.status === 'error') {
    if (i < currentIndex.value) return 'done'
    return i === currentIndex.value ? 'failed' : 'todo'
  }
  if (props.job.status === 'queued') return 'todo'
  if (i < currentIndex.value) return 'done'
  return i === currentIndex.value ? 'active' : 'todo'
}
</script>

<template>
  <div class="progress">
    <h2>{{ job.name }}</h2>
    <p class="muted">{{ job.num_images }} photos</p>

    <div v-if="job.status === 'error'" class="error-box">
      <strong>Reconstruction failed</strong>
      <p>{{ job.error }}</p>
    </div>
    <p v-else-if="job.status === 'queued'" class="muted">Waiting for another reconstruction to finish…</p>

    <ol class="steps">
      <li v-for="(s, i) in steps" :key="s.key" :class="state(i)">
        <span class="marker">
          <template v-if="state(i) === 'done'">✓</template>
          <template v-else-if="state(i) === 'failed'">✕</template>
          <template v-else>{{ i + 1 }}</template>
        </span>
        <div class="body">
          <div>{{ s.label }}</div>
          <template v-if="state(i) === 'active'">
            <div class="muted small">{{ job.message }}</div>
            <div class="bar"><div :style="{ width: `${Math.max(job.progress * 100, 3)}%` }" :class="{ indeterminate: job.progress === 0 }" /></div>
          </template>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.progress { max-width: 560px; margin: 0 auto; padding: 48px 24px; }
h2 { margin: 0; }
.steps { list-style: none; padding: 0; margin: 28px 0 0; display: flex; flex-direction: column; gap: 18px; }
.steps li { display: flex; gap: 14px; align-items: flex-start; color: var(--muted); }
.steps li.active, .steps li.done { color: var(--text); }
.marker {
  width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
  display: grid; place-items: center; font-size: 12px;
  border: 1px solid var(--border); background: var(--panel);
}
.done .marker { background: var(--success); border-color: var(--success); color: #0b1a12; }
.active .marker { border-color: var(--accent); color: var(--accent); }
.failed .marker { background: var(--danger); border-color: var(--danger); color: #fff; }
.body { flex: 1; padding-top: 3px; }
.small { font-size: 12px; margin-top: 4px; }
.bar { height: 6px; background: var(--panel-2); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.bar > div { height: 100%; background: var(--accent); transition: width 0.4s; }
.bar > div.indeterminate { width: 30% !important; animation: slide 1.2s infinite ease-in-out; }
@keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
.error-box { margin-top: 20px; padding: 14px 16px; border: 1px solid var(--danger); border-radius: var(--radius); background: rgba(255, 93, 93, 0.08); }
.error-box p { margin: 6px 0 0; color: var(--muted); }
</style>
