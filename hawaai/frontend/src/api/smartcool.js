/**
 * HawaAI API client.
 *
 * When served via HA ingress the page URL is:
 *   https://ha-host/api/hassio_ingress/TOKEN/
 * The backend injects:
 *   window.__INGRESS_PATH__ = "/api/hassio_ingress/TOKEN"
 * so we can construct correct absolute URLs that go through the ingress proxy.
 * When accessed directly (dev / port-forward), __INGRESS_PATH__ is "".
 */

const INGRESS_PATH = (typeof window !== 'undefined' && window.__INGRESS_PATH__) || ''
const BASE = INGRESS_PATH + '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  const ct = res.headers.get('Content-Type') || ''
  if (ct.includes('application/json')) return res.json()
  return res.text()
}

// ── Status ───────────────────────────────────────────────────────────────────
export const getStatus = (roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/status?room_id=${encodeURIComponent(roomId)}`)
}

/** Cached outdoor weather — no room coupling (Settings preview). */
export const getWeather = () => request('/weather')

// ── Sessions ─────────────────────────────────────────────────────────────────
export const getSessions = (params = {}) => {
  if (!params.room_id) return Promise.reject(new Error('room_id is required'))
  const q = new URLSearchParams(params).toString()
  return request(`/sessions?${q}`)
}

export const getSessionStats = (roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/sessions/stats?room_id=${encodeURIComponent(roomId)}`)
}

// ── Snapshots ────────────────────────────────────────────────────────────────
export const getSnapshots = (minutes = 120, roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/snapshots?minutes=${minutes}&room_id=${encodeURIComponent(roomId)}`)
}

// ── Daily stats ───────────────────────────────────────────────────────────────
export const getDailyStats = (days = 7, roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/daily?days=${days}&room_id=${encodeURIComponent(roomId)}`)
}

// ── Config ───────────────────────────────────────────────────────────────────
export const getConfig   = () => request('/config')
export const patchConfig = (data) =>
  request('/config', { method: 'POST', body: JSON.stringify(data) })
export const reloadConfig = () => request('/config/reload', { method: 'POST' })

export const setAiEnabled = (ai_enabled) =>
  request('/ai', { method: 'POST', body: JSON.stringify({ ai_enabled }) })

/** Merge Ollama URL/model (and/or ai_enabled) to disk; same as Settings Save for these keys. */
export const updateAiConfig = (data) =>
  request('/ai', { method: 'POST', body: JSON.stringify(data) })

/** Last AI call lifecycle for a room (GET /api/ai/status). */
export async function getAiStatus(roomId) {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/ai/status?room_id=${encodeURIComponent(roomId)}`)
}

// ── Multi-room ───────────────────────────────────────────────────────────────
export const getRooms = () => request('/rooms')

export const getRoom = (roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/rooms/${encodeURIComponent(roomId)}`)
}

export const createRoom = (data) =>
  request('/rooms', { method: 'POST', body: JSON.stringify(data) })

export const updateRoom = (roomId, data) =>
  request(`/rooms/${encodeURIComponent(roomId)}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const deleteRoom = (roomId) =>
  request(`/rooms/${encodeURIComponent(roomId)}`, { method: 'DELETE' })
export const getBrands = () => request('/brands')

// ── HA Entities ──────────────────────────────────────────────────────────────
export const getEntities = (domain) => {
  const q = domain ? `?domain=${domain}` : ''
  return request(`/entities${q}`)
}

// ── Climate entity ────────────────────────────────────────────────────────────
export const getClimateState = (entityId) =>
  request(`/climate/${encodeURIComponent(entityId)}`)

export const setClimateTemperature = (entityId, temperature) =>
  request(`/climate/${encodeURIComponent(entityId)}/set_temperature`, {
    method: 'POST',
    body: JSON.stringify({ temperature }),
  })

export const setHvacMode = (entityId, hvac_mode) =>
  request(`/climate/${encodeURIComponent(entityId)}/set_hvac_mode`, {
    method: 'POST',
    body: JSON.stringify({ hvac_mode }),
  })

export const setFanMode = (entityId, fan_mode) =>
  request(`/climate/${encodeURIComponent(entityId)}/set_fan_mode`, {
    method: 'POST',
    body: JSON.stringify({ fan_mode }),
  })

export const setSwingMode = (entityId, swing_mode) =>
  request(`/climate/${encodeURIComponent(entityId)}/set_swing_mode`, {
    method: 'POST',
    body: JSON.stringify({ swing_mode }),
  })

// ── HA Device Registry ───────────────────────────────────────────────────────
export const getDevices = () => request('/devices')
export const getDeviceEntities = (deviceId) => request(`/devices/${encodeURIComponent(deviceId)}/entities`)

// ── Insights ─────────────────────────────────────────────────────────────────
export const getInsights = (roomId) => {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  return request(`/insights?room_id=${encodeURIComponent(roomId)}`)
}

// ── Export ───────────────────────────────────────────────────────────────────
export async function downloadExport(format = 'csv', roomId) {
  if (!roomId) throw new Error('roomId is required')
  const res = await fetch(`${BASE}/export/${format}?room_id=${encodeURIComponent(roomId)}`)
  if (!res.ok) throw new Error('Export failed')
  const blob = await res.blob()
  const cd   = res.headers.get('Content-Disposition') || ''
  const match = cd.match(/filename="(.+)"/)
  const filename = match ? match[1] : `hawaai_data.${format}`
  const url = URL.createObjectURL(blob)
  const a   = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

/** Recent persisted AI outputs (ML / audit). */
export async function getAiDecisions(roomId, limit = 50) {
  if (!roomId) return Promise.reject(new Error('roomId is required'))
  const q = new URLSearchParams({ room_id: roomId, limit: String(limit) }).toString()
  return request(`/ai/decisions?${q}`)
}

// ── WebSocket live updates (per-room subscribe) ─────────────────────────────
export function connectLive(roomId, onMessage, onError) {
  if (!roomId) {
    if (onError) onError(new Error('roomId is required'))
    return { ws: null, close: () => {} }
  }
  let intentionalClose = false
  const proto  = location.protocol === 'https:' ? 'wss' : 'ws'
  const wsPath = INGRESS_PATH + '/ws'
  const ws     = new WebSocket(`${proto}://${location.host}${wsPath}`)

  ws.onopen = () => {
    try {
      ws.send(JSON.stringify({ type: 'subscribe', room_id: roomId }))
    } catch (e) {
      if (onError) onError(e)
    }
  }
  ws.onmessage = (evt) => {
    try { onMessage(JSON.parse(evt.data)) } catch {}
  }
  ws.onerror = onError || (() => {})
  ws.onclose = () => {
    if (intentionalClose) return
    setTimeout(() => connectLive(roomId, onMessage, onError), 5000)
  }
  return {
    ws,
    close: () => {
      intentionalClose = true
      try {
        ws.close()
      } catch {
        /* ignore */
      }
    },
  }
}
