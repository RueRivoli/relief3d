<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import UploadPanel from './components/UploadPanel.vue'
import JobProgress from './components/JobProgress.vue'
import SceneViewer from './components/SceneViewer.vue'

const jobs = ref([])
const selectedId = ref(location.hash.slice(1) || null)
const capabilities = ref({ dense: false, max_images: 200 })
const loadError = ref('')
let timer = null

const selected = computed(() => jobs.value.find((j) => j.id === selectedId.value) ?? null)

async function refresh() {
  try {
    jobs.value = await api.listJobs()
    loadError.value = ''
  } catch (e) {
    loadError.value = `Backend unreachable: ${e.message}`
  }
  const active = jobs.value.some((j) => j.status === 'queued' || j.status === 'running')
  clearTimeout(timer)
  timer = setTimeout(refresh, active ? 1000 : 5000)
}

function select(id) {
  selectedId.value = id
  history.replaceState(null, '', id ? `#${id}` : location.pathname)
}

async function onCreated(job) {
  await refresh()
  select(job.id)
}

async function remove(job) {
  if (!confirm(`Delete "${job.name}"?`)) return
  await api.deleteJob(job.id)
  if (selectedId.value === job.id) select(null)
  refresh()
}

function formatDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
}

onMounted(async () => {
  capabilities.value = await api.capabilities().catch(() => capabilities.value)
  refresh()
})
onUnmounted(() => clearTimeout(timer))
</script>

<template>
  <div class="layout">
    <aside>
      <header>
        <div class="logo">◆ Images to 3D</div>
        <button class="primary" @click="select(null)">+ New scene</button>
      </header>
      <p v-if="loadError" class="error small">{{ loadError }}</p>
      <ul class="jobs">
        <li
          v-for="job in jobs"
          :key="job.id"
          :class="{ selected: job.id === selectedId }"
          @click="select(job.id)"
        >
          <span class="dot" :class="job.status" />
          <div class="info">
            <div class="name">{{ job.name }}</div>
            <div class="muted small">{{ job.num_images }} photos · {{ formatDate(job.created) }}</div>
          </div>
          <button class="del danger" title="Delete" :disabled="job.status === 'running'" @click.stop="remove(job)">✕</button>
        </li>
        <li v-if="!jobs.length && !loadError" class="empty muted">No scenes yet</li>
      </ul>
    </aside>

    <main>
      <UploadPanel v-if="!selected" :capabilities="capabilities" @created="onCreated" />
      <SceneViewer v-else-if="selected.status === 'done'" :key="selected.id" :job="selected" />
      <JobProgress v-else :job="selected" />
    </main>
  </div>
</template>

<style scoped>
.layout { display: flex; height: 100%; }
aside {
  width: 280px;
  flex-shrink: 0;
  background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}
header { padding: 16px; display: flex; flex-direction: column; gap: 12px; border-bottom: 1px solid var(--border); }
.logo { font-weight: 700; font-size: 16px; letter-spacing: 0.2px; }
.jobs { list-style: none; margin: 0; padding: 8px; overflow-y: auto; flex: 1; }
.jobs li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border-radius: 8px;
  cursor: pointer;
}
.jobs li:hover { background: var(--panel-2); }
.jobs li.selected { background: var(--panel-2); outline: 1px solid var(--border); }
.jobs li.empty { cursor: default; justify-content: center; }
.info { flex: 1; min-width: 0; }
.name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.small { font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--muted); }
.dot.done { background: var(--success); }
.dot.error { background: var(--danger); }
.dot.running { background: var(--accent); animation: pulse 1s infinite alternate; }
@keyframes pulse { to { opacity: 0.3; } }
.del { padding: 2px 7px; font-size: 11px; opacity: 0; }
.jobs li:hover .del { opacity: 1; }
main { flex: 1; min-width: 0; min-height: 0; position: relative; overflow: hidden; }
.error.small { padding: 0 16px; }

@media (max-width: 720px) {
  .layout { flex-direction: column; }
  aside { width: 100%; max-height: 35%; border-right: none; border-bottom: 1px solid var(--border); }
  main { min-height: 0; }
}
</style>
