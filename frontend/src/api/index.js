// 18005 接口层。所有错误统一抛 Error(message)，message 已是可直接展示的中文。

async function request(method, path, body) {
  const init = { method, headers: { Accept: 'application/json' } }
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(path, init)
  } catch (e) {
    throw new Error(`无法连接工作站服务（${e.message}）`)
  }
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { error: text }
  }
  if (!res.ok) {
    const detail = data && (data.detail ?? data)
    const msg =
      (detail && (detail.message || detail.error)) ||
      (typeof detail === 'string' ? detail : '') ||
      `${method} ${path} 失败（HTTP ${res.status}）`
    throw new Error(msg)
  }
  return data
}

export const api = {
  config: () => request('GET', '/api/config'),
  health: () => request('GET', '/api/health'),
  robot: () => request('GET', '/api/robot'),
  setRobot: (unitCode) => request('PUT', '/api/robot', { unit_code: unitCode }),
  rememberCamera: (role, serial, name) => request('PUT', '/api/robot/cameras', { role, serial, name }),
  forgetCamera: (role) => request('DELETE', `/api/robot/cameras/${role}`),

  cameras: () => request('GET', '/api/cameras'),
  selectCamera: (serial, cameraRole) => request('POST', '/api/cameras/select', { serial, camera_role: cameraRole }),
  detect: () => request('POST', '/api/cameras/detect'),

  plans: (cameraRole, arm) =>
    request('GET', `/api/plans?camera_role_id=${encodeURIComponent(cameraRole)}${arm ? `&arm=${arm}` : ''}`),

  calibration: () => request('GET', '/api/calibration'),
  prepare: (payload) => request('POST', '/api/calibration/prepare', payload),
  engage: () => request('POST', '/api/calibration/engage'),
  guide: () => request('POST', '/api/calibration/guide'),
  catchHold: () => request('POST', '/api/calibration/catch'),
  disarm: () => request('POST', '/api/calibration/disarm'),
  run: (runName) => request('POST', '/api/calibration/run', { run_name: runName || '', confirm: true }),
  pause: () => request('POST', '/api/calibration/pause'),
  resume: () => request('POST', '/api/calibration/resume'),
  stop: () => request('POST', '/api/calibration/stop'),
  markCaptured: () => request('POST', '/api/calibration/mark-captured'),
  solve: (payload) => request('POST', '/api/calibration/solve', payload),
  solveStatus: () => request('GET', '/api/calibration/solve/status'),
  finalize: (payload) => request('POST', '/api/calibration/finalize', payload),
  resetJob: () => request('POST', '/api/calibration/reset'),
  setJobRole: (cameraRole, cameraLabel) =>
    request('POST', '/api/calibration/role', { camera_role: cameraRole, camera_label: cameraLabel || '' }),
  loadRun: (runId, arm) => request('POST', '/api/calibration/load-run', { run_id: runId, arm }),

  handCalibration: () => request('GET', '/api/hand-calibration'),
  solveHandCalibration: (cameraRole) =>
    request('POST', '/api/hand-calibration/solve', { camera_role: cameraRole }),
  finalizeHandCalibration: (payload) => request('POST', '/api/hand-calibration/finalize', payload),

  artifacts: (type, role) => {
    const q = new URLSearchParams()
    if (type) q.set('type', type)
    if (role) q.set('camera_role', role)
    const s = q.toString()
    return request('GET', `/api/artifacts${s ? `?${s}` : ''}`)
  },
  activeArtifacts: () => request('GET', '/api/artifacts/active'),
  activate: (type, role, runId) =>
    request('POST', `/api/artifacts/${type}/${role}/${encodeURIComponent(runId)}/activate`),
  deleteArtifact: (type, role, runId) =>
    request('DELETE', `/api/artifacts/${type}/${role}/${encodeURIComponent(runId)}`),
  fileUrl: (type, role, runId, name) =>
    `/api/artifacts/${type}/${role}/${encodeURIComponent(runId)}/files/${encodeURIComponent(name)}`,
  runs: () => request('GET', '/api/runs'),

  cloud: () => request('GET', '/api/cloud'),
  setCloud: (payload) => request('PUT', '/api/cloud', payload),
  testCloud: () => request('POST', '/api/cloud/test'),
  syncCloud: (force = false) => request('POST', `/api/cloud/sync${force ? '?force=true' : ''}`),
  pushArtifact: (type, role, runId) =>
    request('POST', `/api/artifacts/${type}/${role}/${encodeURIComponent(runId)}/push`),
}
