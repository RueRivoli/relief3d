<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js'
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js'
import { api } from '../api'

const props = defineProps({ job: { type: Object, required: true } })

const container = ref(null)
const scene = ref(null)
const loading = ref(true)
const loadError = ref('')
const view = ref('dense') // 'splat' | 'dense' | 'sparse'
const splatLoading = ref(false)
const pointSize = ref(1)
const showCameras = ref(true)
const showBox = ref(false)
const measuring = ref(false)
const measurements = ref([]) // { id, a: [x,y,z], b: [x,y,z], raw }
const pendingPoint = ref(null)
const selectedCamera = ref(null)
const scaleKey = `scale:${props.job.id}`
const scale = ref(readScale()) // meters per scene unit, null when uncalibrated

function readScale() {
  try {
    const v = parseFloat(localStorage.getItem(scaleKey))
    return Number.isFinite(v) && v > 0 ? v : null
  } catch {
    return null
  }
}

function writeScale(v) {
  try {
    if (v == null) localStorage.removeItem(scaleKey)
    else localStorage.setItem(scaleKey, String(v))
  } catch { /* storage unavailable */ }
}

// Measuring always picks on a point cloud, hidden behind the splats in splat view.
const cloudKind = computed(() => (view.value === 'splat' ? (scene.value.files.dense ? 'dense' : 'sparse') : view.value))

const unit = computed(() => (scale.value ? 'm' : 'units'))
function fmt(raw) {
  const v = raw * (scale.value ?? 1)
  return `${v < 10 ? v.toFixed(2) : v.toFixed(1)} ${unit.value}`
}
const dimensions = computed(() => {
  const s = scene.value?.bounds.size
  return s ? { width: fmt(s[0]), depth: fmt(s[2]), height: fmt(s[1]) } : null
})

// --- three.js state (kept outside Vue reactivity) ---
let renderer, labelRenderer, camera, controls, threeScene, resizeObserver
let cloud = null
const clouds = {}
let spark = null
let splatMesh = null
let splatReady = null
let cameraGroup, boxHelper, measureGroup, pendingMarker
const cameraTargets = []
const raycaster = new THREE.Raycaster()
let sceneScale = 1 // characteristic length used to size helpers
let flight = null

function initThree() {
  const el = container.value
  renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setClearColor(0x0f1115)
  el.appendChild(renderer.domElement)

  labelRenderer = new CSS2DRenderer()
  labelRenderer.domElement.className = 'labels'
  el.appendChild(labelRenderer.domElement)

  threeScene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(60, 1, 0.01, 1000)
  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.08
  controls.addEventListener('start', () => (flight = null))

  const grid = new THREE.GridHelper(20, 20, 0x3a4150, 0x232833)
  grid.material.transparent = true
  grid.material.opacity = 0.6
  threeScene.add(grid)

  cameraGroup = new THREE.Group()
  measureGroup = new THREE.Group()
  threeScene.add(cameraGroup, measureGroup)

  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(el)
  resize()

  let down = null
  renderer.domElement.addEventListener('pointerdown', (e) => (down = { x: e.clientX, y: e.clientY }))
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 5) onClick(e)
    down = null
  })

  renderer.setAnimationLoop(animate)
}

function resize() {
  const { clientWidth: w, clientHeight: h } = container.value
  if (!w || !h) return
  renderer.setSize(w, h)
  labelRenderer.setSize(w, h)
  camera.aspect = w / h
  camera.updateProjectionMatrix()
}

function animate() {
  if (flight) {
    const t = Math.min((performance.now() - flight.start) / flight.duration, 1)
    const k = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2 // easeInOutQuad
    camera.position.lerpVectors(flight.fromPos, flight.toPos, k)
    controls.target.lerpVectors(flight.fromTarget, flight.toTarget, k)
    camera.up.lerpVectors(flight.fromUp, flight.toUp, k).normalize()
    camera.fov = THREE.MathUtils.lerp(flight.fromFov, flight.toFov, k)
    camera.updateProjectionMatrix()
    if (t === 1) flight = null
  }
  controls.update()
  renderer.render(threeScene, camera)
  labelRenderer.render(threeScene, camera)
}

function flyTo(position, target, { up = new THREE.Vector3(0, 1, 0), fov = 60, duration = 700 } = {}) {
  flight = {
    start: performance.now(),
    duration,
    fromPos: camera.position.clone(),
    toPos: position.clone(),
    fromTarget: controls.target.clone(),
    toTarget: target.clone(),
    fromUp: camera.up.clone(),
    toUp: up.clone(),
    fromFov: camera.fov,
    toFov: fov,
  }
}

// --- scene content ---
async function loadCloud(kind) {
  if (!clouds[kind]) {
    const geometry = await new PLYLoader().loadAsync(api.outputUrl(props.job.id, scene.value.files[kind]))
    const material = new THREE.PointsMaterial({ vertexColors: true, sizeAttenuation: true })
    clouds[kind] = new THREE.Points(geometry, material)
  }
  if (cloud) threeScene.remove(cloud)
  cloud = clouds[kind]
  threeScene.add(cloud)
  updatePointSize()
}

function loadSplat() {
  splatReady ??= createSplat()
  return splatReady
}

async function createSplat() {
  splatLoading.value = true
  try {
    const { SparkRenderer, SplatMesh } = await import('@sparkjsdev/spark') // large, load on demand
    spark = new SparkRenderer({ renderer })
    threeScene.add(spark)
    splatMesh = new SplatMesh({ url: api.outputUrl(props.job.id, scene.value.files.splat) })
    // Splats are trained in COLMAP coordinates; move them into the viewer frame.
    splatMesh.matrixAutoUpdate = false
    splatMesh.matrix.set(...scene.value.splat_transform.flat())
    threeScene.add(splatMesh)
    await splatMesh.initialized
  } finally {
    splatLoading.value = false
  }
}

async function showView(kind) {
  if (kind === 'splat') await loadSplat()
  await loadCloud(cloudKind.value)
  cloud.visible = kind !== 'splat'
  if (splatMesh) splatMesh.visible = kind === 'splat'
}

function updatePointSize() {
  if (!cloud) return
  const base = cloudKind.value === 'sparse' ? 0.004 : 0.0018
  cloud.material.size = base * sceneScale * pointSize.value
}

function buildCameras() {
  const depth = 0.04 * sceneScale
  const positions = []
  for (const cam of scene.value.cameras) {
    const R = new THREE.Matrix3().fromArray(cam.rotation.flat()).transpose() // row-major → column-major
    const origin = new THREE.Vector3(...cam.position)
    const hw = (cam.width / 2 / cam.focal) * depth
    const hh = (cam.height / 2 / cam.focal) * depth
    // OpenCV camera frame: x right, y down, z forward.
    const corners = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([x, y]) =>
      new THREE.Vector3(x, y, depth).applyMatrix3(R).add(origin),
    )
    for (let i = 0; i < 4; i++) {
      positions.push(...origin.toArray(), ...corners[i].toArray())
      positions.push(...corners[i].toArray(), ...corners[(i + 1) % 4].toArray())
    }
    // "Up" tick on the top edge so orientation is readable.
    const topMid = corners[0].clone().add(corners[1]).multiplyScalar(0.5)
    const tick = new THREE.Vector3(0, -hh * 0.6, 0).applyMatrix3(R).add(topMid)
    positions.push(...topMid.toArray(), ...tick.toArray())

    const target = new THREE.Mesh(
      new THREE.SphereGeometry(depth * 0.12, 12, 8),
      new THREE.MeshBasicMaterial({ color: 0xffb020 }),
    )
    target.position.copy(origin)
    target.userData.camera = cam
    cameraTargets.push(target)
    cameraGroup.add(target)
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  cameraGroup.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0xffb020 })))
}

function buildBox() {
  const { min, max } = scene.value.bounds
  const box = new THREE.Box3(new THREE.Vector3(...min), new THREE.Vector3(...max))
  boxHelper = new THREE.Box3Helper(box, 0x4f8cff)
  boxHelper.visible = showBox.value
  threeScene.add(boxHelper)
}

function overview() {
  const { min, max } = scene.value.bounds
  const center = new THREE.Vector3(...min).add(new THREE.Vector3(...max)).multiplyScalar(0.5)
  const size = new THREE.Vector3(...max).sub(new THREE.Vector3(...min))
  // Look from the side the photos were taken from, slightly above.
  const camCenter = scene.value.cameras
    .reduce((acc, c) => acc.add(new THREE.Vector3(...c.position)), new THREE.Vector3())
    .divideScalar(scene.value.cameras.length)
  const dir = camCenter.clone().sub(center).setY(0)
  if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1)
  dir.normalize().multiplyScalar(size.length() * 0.85)
  dir.y = size.length() * 0.45
  return { position: center.clone().add(dir), target: center }
}

function resetView(animated = true) {
  const { position, target } = overview()
  if (animated) return flyTo(position, target)
  camera.position.copy(position)
  controls.target.copy(target)
}

function topView() {
  const { target } = overview()
  const h = Math.max(...scene.value.bounds.size) * 1.3
  flyTo(target.clone().setY(h), target, { up: new THREE.Vector3(0, 0, -1) })
}

function viewFromCamera(cam) {
  const R = new THREE.Matrix3().fromArray(cam.rotation.flat()).transpose()
  const forward = new THREE.Vector3(0, 0, 1).applyMatrix3(R)
  const up = new THREE.Vector3(0, -1, 0).applyMatrix3(R)
  const pos = new THREE.Vector3(...cam.position)
  const fov = THREE.MathUtils.radToDeg(2 * Math.atan(cam.height / 2 / cam.focal))
  flyTo(pos, pos.clone().add(forward.multiplyScalar(sceneScale * 0.05)), { up, fov })
}

// --- picking & measuring ---
function pointer(e) {
  const rect = renderer.domElement.getBoundingClientRect()
  return new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1)
}

function onClick(e) {
  raycaster.setFromCamera(pointer(e), camera)
  if (measuring.value) {
    raycaster.params.Points.threshold = sceneScale * 0.004
    const hit = raycaster.intersectObject(cloud)[0]
    if (hit) addMeasurePoint(hit.point)
    return
  }
  if (showCameras.value) {
    const hit = raycaster.intersectObjects(cameraTargets)[0]
    selectedCamera.value = hit ? hit.object.userData.camera : null
  }
}

function marker(p) {
  const m = new THREE.Mesh(
    new THREE.SphereGeometry(sceneScale * 0.004, 12, 8),
    new THREE.MeshBasicMaterial({ color: 0xff4f9a, depthTest: false }),
  )
  m.renderOrder = 10
  m.position.copy(p)
  return m
}

function addMeasurePoint(p) {
  if (!pendingPoint.value) {
    pendingPoint.value = p.toArray()
    pendingMarker = marker(p)
    measureGroup.add(pendingMarker)
    return
  }
  const a = new THREE.Vector3(...pendingPoint.value)
  measureGroup.remove(pendingMarker)
  pendingPoint.value = null
  measurements.value.push({ id: crypto.randomUUID(), a: a.toArray(), b: p.toArray(), raw: a.distanceTo(p) })
}

function rebuildMeasurements() {
  measureGroup.children.slice().forEach((o) => {
    if (o !== pendingMarker) {
      measureGroup.remove(o)
      o.traverse((c) => c.element?.remove())
    }
  })
  for (const m of measurements.value) {
    const a = new THREE.Vector3(...m.a)
    const b = new THREE.Vector3(...m.b)
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([a, b]),
      new THREE.LineBasicMaterial({ color: 0xff4f9a, depthTest: false }),
    )
    line.renderOrder = 10
    const div = document.createElement('div')
    div.className = 'measure-label'
    div.textContent = fmt(m.raw)
    const label = new CSS2DObject(div)
    label.position.copy(a.clone().add(b).multiplyScalar(0.5))
    line.add(label)
    measureGroup.add(line, marker(a), marker(b))
  }
}

function calibrate(m) {
  const input = prompt('Real length of this measurement, in meters:', scale.value ? (m.raw * scale.value).toFixed(2) : '')
  const meters = parseFloat(input?.replace(',', '.'))
  if (!Number.isFinite(meters) || meters <= 0) return
  scale.value = meters / m.raw
  writeScale(scale.value)
}

function resetScale() {
  scale.value = null
  writeScale(null)
}

function toggleMeasuring() {
  measuring.value = !measuring.value
  if (!measuring.value && pendingMarker) {
    measureGroup.remove(pendingMarker)
    pendingPoint.value = null
  }
}

function removeMeasurement(id) {
  measurements.value = measurements.value.filter((m) => m.id !== id)
}

watch(measurements, rebuildMeasurements, { deep: true })
watch(scale, rebuildMeasurements)
watch(pointSize, updatePointSize)
watch(view, (k) => scene.value && !loading.value && showView(k).catch((e) => (loadError.value = e.message)))
watch(showCameras, (v) => {
  cameraGroup.visible = v
  if (!v) selectedCamera.value = null
})
watch(showBox, (v) => boxHelper && (boxHelper.visible = v))

onMounted(async () => {
  initThree()
  try {
    scene.value = await api.scene(props.job.id)
    sceneScale = Math.hypot(...scene.value.bounds.size) || 1
    view.value = scene.value.files.splat ? 'splat' : scene.value.files.dense ? 'dense' : 'sparse'
    await showView(view.value)
    buildCameras()
    buildBox()
    resetView(false)
  } catch (e) {
    loadError.value = e.message
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  renderer.setAnimationLoop(null)
  resizeObserver.disconnect()
  controls.dispose()
  threeScene.traverse((o) => {
    o.geometry?.dispose()
    o.material?.dispose()
  })
  Object.values(clouds).forEach((c) => {
    c.geometry.dispose()
    c.material.dispose()
  })
  splatMesh?.dispose()
  renderer.dispose()
})
</script>

<template>
  <div class="viewer">
    <div ref="container" class="canvas" :class="{ measuring }" />

    <div v-if="loading" class="overlay">Loading 3D scene…</div>
    <div v-else-if="splatLoading" class="overlay">Loading Gaussian splats…</div>
    <div v-else-if="loadError" class="overlay error">{{ loadError }}</div>

    <template v-if="scene">
      <div class="panel top-left">
        <div class="title">{{ job.name }}</div>
        <div class="muted small">
          {{ scene.stats.images_registered }}/{{ scene.stats.images_uploaded }} photos placed ·
          <template v-if="view === 'splat'">{{ scene.stats.splats.toLocaleString() }} splats</template>
          <template v-else>{{ (scene.stats.dense_points || scene.stats.sparse_points).toLocaleString() }} points</template>
        </div>
        <div v-if="dimensions" class="dims">
          <div><span class="muted">Width</span>{{ dimensions.width }}</div>
          <div><span class="muted">Depth</span>{{ dimensions.depth }}</div>
          <div><span class="muted">Height</span>{{ dimensions.height }}</div>
        </div>
        <div v-if="!scale" class="hint muted small">
          Photos alone don't give real-world scale. Measure something whose size you know (a door ≈ 2.0 m), then click
          <em>Set length</em> to calibrate.
        </div>
        <div v-else class="small calibrated">
          ✓ Calibrated to meters <button class="link" @click="resetScale">reset</button>
        </div>
        <div v-if="scene.stats.images_registered < scene.stats.images_uploaded" class="warn small">
          {{ scene.stats.images_uploaded - scene.stats.images_registered }} photo(s) couldn't be placed (not enough overlap).
        </div>
      </div>

      <div class="panel toolbar">
        <div v-if="scene.files.dense || scene.files.splat" class="seg">
          <button v-if="scene.files.splat" :class="{ active: view === 'splat' }" @click="view = 'splat'">Splats</button>
          <button v-if="scene.files.dense" :class="{ active: view === 'dense' }" @click="view = 'dense'">Dense</button>
          <button :class="{ active: view === 'sparse' }" @click="view = 'sparse'">Sparse</button>
        </div>
        <label v-if="view !== 'splat'" class="size">
          <span class="muted small">Point size</span>
          <input v-model.number="pointSize" type="range" min="0.3" max="4" step="0.1" />
        </label>
        <button :class="{ active: showCameras }" @click="showCameras = !showCameras">Cameras</button>
        <button :class="{ active: showBox }" @click="showBox = !showBox">Bounds</button>
        <button :class="{ active: measuring }" @click="toggleMeasuring">📏 Measure</button>
        <button @click="resetView()">Reset view</button>
        <button @click="topView">Top view</button>
        <a :href="api.outputUrl(job.id, scene.files[view])" :download="`${job.name}-${view}.ply`">
          <button>Download .ply</button>
        </a>
      </div>

      <div v-if="measuring" class="panel status small">
        {{ pendingPoint ? 'Click the second point' : 'Click a first point on the cloud' }}
      </div>

      <div v-if="measurements.length" class="panel measures">
        <div class="title small">Measurements</div>
        <div v-for="(m, i) in measurements" :key="m.id" class="measure">
          <span class="muted">#{{ i + 1 }}</span>
          <strong>{{ fmt(m.raw) }}</strong>
          <button class="link" @click="calibrate(m)">Set length</button>
          <button class="link danger" @click="removeMeasurement(m.id)">✕</button>
        </div>
      </div>

      <div v-if="selectedCamera" class="panel photo">
        <img :src="api.imageUrl(job.id, selectedCamera.name)" :alt="selectedCamera.name" />
        <div class="photo-actions">
          <button class="primary" @click="viewFromCamera(selectedCamera)">View from here</button>
          <button @click="selectedCamera = null">Close</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.viewer { position: absolute; inset: 0; }
.canvas { position: absolute; inset: 0; }
.canvas.measuring { cursor: crosshair; }
.canvas :deep(.labels) { position: absolute; top: 0; left: 0; pointer-events: none; }
.canvas :deep(.measure-label) {
  background: #ff4f9a; color: #fff; font-size: 12px; font-weight: 600;
  padding: 2px 7px; border-radius: 6px; white-space: nowrap;
}
.overlay { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }
.panel {
  position: absolute;
  background: rgba(23, 26, 33, 0.88);
  backdrop-filter: blur(8px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px;
}
.top-left { top: 16px; left: 16px; width: 280px; display: flex; flex-direction: column; gap: 8px; }
.title { font-weight: 600; }
.small { font-size: 12px; }
.dims { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 4px; }
.dims > div { background: var(--panel-2); border-radius: 8px; padding: 6px 8px; display: flex; flex-direction: column; font-weight: 600; font-size: 13px; }
.dims .muted { font-weight: 400; font-size: 11px; }
.hint { line-height: 1.45; }
.calibrated { color: var(--success); }
.warn { color: #ffb020; }
.toolbar {
  bottom: 16px; left: 50%; transform: translateX(-50%);
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: center;
  padding: 8px; max-width: calc(100% - 32px);
}
.seg { display: flex; }
.seg button { border-radius: 0; margin-left: -1px; }
.seg button:first-child { border-radius: 8px 0 0 8px; margin-left: 0; }
.seg button:last-child { border-radius: 0 8px 8px 0; }
.size { display: flex; align-items: center; gap: 6px; }
.size input { width: 90px; accent-color: var(--accent); }
.status { top: 16px; left: 50%; transform: translateX(-50%); padding: 8px 14px; }
.measures { top: 16px; right: 16px; width: 240px; display: flex; flex-direction: column; gap: 6px; }
.measure { display: flex; align-items: center; gap: 8px; }
.measure strong { margin-right: auto; }
button.link { background: none; border: none; padding: 0 2px; color: var(--accent); font-size: 12px; }
button.link.danger { color: var(--muted); }
button.link.danger:hover { color: var(--danger); }
.photo { right: 16px; bottom: 80px; width: 320px; padding: 8px; }
.photo img { width: 100%; border-radius: 6px; display: block; }
.photo-actions { display: flex; gap: 8px; margin-top: 8px; justify-content: flex-end; }

@media (max-width: 720px) {
  .top-left { width: auto; right: 16px; }
  .measures { top: auto; bottom: 120px; }
  .photo { width: auto; left: 16px; }
}
</style>
