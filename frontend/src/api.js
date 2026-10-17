async function request(path, options = {}) {
  const res = await fetch(`/api${path}`, options)
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch { /* non-JSON error body */ }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  capabilities: () => request('/capabilities'),
  listJobs: () => request('/jobs'),
  getJob: (id) => request(`/jobs/${id}`),
  deleteJob: (id) => request(`/jobs/${id}`, { method: 'DELETE' }),
  scene: (id) => request(`/jobs/${id}/output/scene.json`),
  outputUrl: (id, file) => `/api/jobs/${id}/output/${file}`,
  imageUrl: (id, name) => `/api/jobs/${id}/images/${encodeURIComponent(name)}`,

  // XHR instead of fetch to report upload progress.
  createJob(files, { name, dense }, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData()
      files.forEach((f) => form.append('files', f, f.name))
      form.append('name', name)
      form.append('dense', dense ? 'true' : 'false')
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/jobs')
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total)
      xhr.onload = () => {
        let body = null
        try { body = JSON.parse(xhr.responseText) } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body)
        else reject(new Error(body?.detail ?? `Upload failed (${xhr.status})`))
      }
      xhr.onerror = () => reject(new Error('Network error during upload'))
      xhr.send(form)
    })
  },
}
