<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { inputSummary } from '../labels'

const props = defineProps({ capabilities: { type: Object, required: true } })
const emit = defineEmits(['created'])

const ACCEPT = /\.(jpe?g|png|webp|heic|bmp|tiff?)$/i
const VIDEO = /\.(mp4|mov|m4v|webm|mkv|avi)$/i
const files = ref([]) // { file, url, video }
const name = ref('')
const dense = ref(true)
const splat = ref(false)
const dragging = ref(false)
const uploading = ref(false)
const uploadProgress = ref(0)
const error = ref('')
const input = ref(null)

const numVideos = computed(() => files.value.filter((f) => f.video).length)
const numPhotos = computed(() => files.value.length - numVideos.value)
// A video alone is enough: frames are extracted on the server.
const canSubmit = computed(() => (numPhotos.value >= 3 || numVideos.value > 0) && !uploading.value)

function addFiles(list) {
  error.value = ''
  const known = new Set(files.value.map((f) => f.file.name + f.file.size))
  for (const file of list) {
    const video = VIDEO.test(file.name)
    if (video && !props.capabilities.video) {
      error.value = 'Video input is not available: install PyAV on the server (pip install av).'
      continue
    }
    if (!(video || ACCEPT.test(file.name)) || known.has(file.name + file.size)) continue
    // Browsers can't preview HEIC; show a placeholder tile instead.
    const url = /\.heic$/i.test(file.name) ? null : URL.createObjectURL(file)
    files.value.push({ file, url, video })
  }
  if (numPhotos.value > props.capabilities.max_images) {
    error.value = `At most ${props.capabilities.max_images} photos per scene.`
  }
}

function onDrop(e) {
  dragging.value = false
  addFiles(e.dataTransfer.files)
}

function removeFile(i) {
  const [f] = files.value.splice(i, 1)
  if (f.url) URL.revokeObjectURL(f.url)
}

function clearAll() {
  files.value.forEach((f) => f.url && URL.revokeObjectURL(f.url))
  files.value = []
}

async function submit() {
  uploading.value = true
  error.value = ''
  try {
    const job = await api.createJob(
      files.value.map((f) => f.file),
      { name: name.value, dense: dense.value && props.capabilities.dense, splat: splat.value && props.capabilities.splat },
      (p) => (uploadProgress.value = p),
    )
    clearAll()
    name.value = ''
    emit('created', job)
  } catch (e) {
    error.value = e.message
  } finally {
    uploading.value = false
    uploadProgress.value = 0
  }
}

onUnmounted(clearAll)
</script>

<template>
  <div class="upload">
    <div class="intro">
      <h1>Reconstruct a scene in 3D</h1>
      <p class="muted">
        Upload photos or a video of a room or a landscape taken from different positions. The app recovers where
        each photo was taken and builds a colored 3D point cloud you can explore and measure.
      </p>
    </div>

    <div
      class="dropzone"
      :class="{ dragging }"
      @click="input.click()"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input ref="input" type="file" multiple accept="image/*,.heic,video/*" hidden @change="addFiles($event.target.files); $event.target.value = ''" />
      <div class="icon">⬆</div>
      <div><strong>Drop photos or a video here</strong> or click to browse</div>
      <div class="muted small">JPG, PNG, HEIC, WEBP · at least 3, ideally 15–60 photos — or MP4, MOV videos</div>
    </div>

    <div v-if="files.length" class="thumbs">
      <div v-for="(f, i) in files" :key="f.file.name + f.file.size" class="thumb">
        <video v-if="f.video && f.url" :src="f.url" muted preload="metadata" />
        <img v-else-if="f.url" :src="f.url" :alt="f.file.name" loading="lazy" />
        <div v-else class="placeholder">{{ f.file.name.split('.').pop().toUpperCase() }}</div>
        <span v-if="f.video" class="badge">▶ Video</span>
        <button class="remove" title="Remove" @click="removeFile(i)">✕</button>
      </div>
    </div>

    <div class="form">
      <input v-model="name" type="text" placeholder="Scene name (optional)" maxlength="80" />
      <label class="check" :class="{ disabled: !capabilities.dense }" :title="capabilities.dense ? '' : 'Install backend/requirements-dense.txt to enable'">
        <input v-model="dense" type="checkbox" :disabled="!capabilities.dense" />
        Dense reconstruction <span class="muted">(AI depth, slower but much fuller)</span>
      </label>
      <label class="check" :class="{ disabled: !capabilities.splat }" :title="capabilities.splat ? '' : 'Run scripts/install-brush.sh to enable'">
        <input v-model="splat" type="checkbox" :disabled="!capabilities.splat" />
        Gaussian splatting <span class="muted">(photorealistic rendering, ~15–30 min on a laptop GPU)</span>
      </label>
      <div class="actions">
        <span class="muted">{{ inputSummary({ num_images: numPhotos, num_videos: numVideos }) }}</span>
        <button v-if="files.length" :disabled="uploading" @click="clearAll">Clear</button>
        <button class="primary" :disabled="!canSubmit" @click="submit">
          {{ uploading ? `Uploading ${Math.round(uploadProgress * 100)}%` : 'Build 3D scene' }}
        </button>
      </div>
      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <details class="tips">
      <summary>Tips for good results</summary>
      <ul>
        <li>Move around the scene between shots — don't just rotate on the spot. Each photo should overlap ~60% with the previous one.</li>
        <li>For a room: walk along the walls, pointing the camera toward the opposite side, and include the corners.</li>
        <li>Keep the scene static, well lit and sharp (avoid motion blur). Textured surfaces work best; plain white walls and mirrors are hard.</li>
        <li>Use the same camera and zoom for all photos.</li>
        <li>Video: walk slowly and steadily, 20 s to 2 min, no fast pans. About 100 sharp frames are picked automatically.</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.upload { max-width: 900px; margin: 0 auto; padding: 40px 24px; display: flex; flex-direction: column; gap: 20px; height: 100%; overflow-y: auto; }
h1 { margin: 0 0 6px; font-size: 24px; }
.intro p { margin: 0; line-height: 1.5; }
.dropzone {
  border: 2px dashed var(--border);
  border-radius: var(--radius);
  padding: 40px 20px;
  text-align: center;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  transition: border-color 0.15s, background 0.15s;
}
.dropzone:hover, .dropzone.dragging { border-color: var(--accent); background: rgba(79, 140, 255, 0.06); }
.icon { font-size: 28px; color: var(--accent); }
.small { font-size: 12px; }
.thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.thumb { position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden; background: var(--panel-2); }
.thumb img, .thumb video { width: 100%; height: 100%; object-fit: cover; display: block; }
.badge { position: absolute; left: 4px; bottom: 4px; padding: 1px 6px; border-radius: 4px; font-size: 11px; background: rgba(0, 0, 0, 0.6); color: #fff; }
.placeholder { display: grid; place-items: center; height: 100%; color: var(--muted); font-size: 12px; }
.remove { position: absolute; top: 4px; right: 4px; padding: 1px 6px; font-size: 11px; background: rgba(0, 0, 0, 0.6); opacity: 0; }
.thumb:hover .remove { opacity: 1; }
.form { display: flex; flex-direction: column; gap: 12px; }
.check { display: flex; gap: 8px; align-items: center; cursor: pointer; }
.check.disabled { opacity: 0.5; cursor: not-allowed; }
.actions { display: flex; gap: 10px; align-items: center; justify-content: flex-end; }
.actions .muted { margin-right: auto; }
.error { margin: 0; }
.tips { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 16px; }
.tips summary { cursor: pointer; font-weight: 600; }
.tips ul { margin: 10px 0 0; padding-left: 20px; line-height: 1.6; color: var(--muted); }
</style>
