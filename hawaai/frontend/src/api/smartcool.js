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

/** Match backend `normalize_room_id` (trim + lowercase) — WS payloads use canonical room_id. */
function normalizeRoomKey(id) {
  return id != null ? String(id).trim().toLowerCase() : ''
}

/** Non-empty trimmed room id, or rejects — all dashboard APIs must be room-scoped. */
function roomParam(roomId, label = 'room_id') {
  const s = roomId != null ? String(roomId).trim() : ''
  if (!s) return Promise.reject(new Error(`${label} is required`))
  return Promise.resolve(s)
}

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
export const getStatus = (roomId) =>
  roomParam(roomId).then((rid) => request(`/status?room_id=${encodeURIComponent(rid)}`))

/** Cached outdoor weather — no room coupling (Settings preview). */
export const getWeather = () => request('/weather')

// ── Sessions ─────────────────────────────────────────────────────────────────
export const getSessions = (params = {}) => {
  const rid = params.room_id != null ? String(params.room_id).trim() : ''
  if (!rid) return Promise.reject(new Error('room_id is required'))
  const q = new URLSearchParams({ ...params, room_id: rid }).toString()
  return request(`/sessions?${q}`)
}

export const getSessionStats = (roomId) =>
  roomParam(roomId).then((rid) =>
    request(`/sessions/stats?room_id=${encodeURIComponent(rid)}`),
  )

// ── Snapshots ────────────────────────────────────────────────────────────────
export const getSnapshots = (minutes = 120, roomId) =>
  roomParam(roomId).then((rid) =>
    request(`/snapshots?minutes=${minutes}&room_id=${encodeURIComponent(rid)}`),
  )

// ── Daily stats ───────────────────────────────────────────────────────────────
export const getDailyStats = (days = 7, roomId) =>
  roomParam(roomId).then((rid) =>
    request(`/daily?days=${days}&room_id=${encodeURIComponent(rid)}`),
  )

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
  const rid = await roomParam(roomId)
  return request(`/ai/status?room_id=${encodeURIComponent(rid)}`)
}

// ── Multi-room ───────────────────────────────────────────────────────────────
export const getRooms = () => request('/rooms')

export const getRoom = (roomId) =>
  roomParam(roomId).then((rid) =>
    request(`/rooms/${encodeURIComponent(rid)}`),
  )

export const createRoom = (data) =>
  request('/rooms', { method: 'POST', body: JSON.stringify(data) })

export const updateRoom = (roomId, data) =>
  request(`/rooms/${encodeURIComponent(roomId)}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const deleteRoom = (roomId, { purge = false } = {}) => {
  const q = purge ? '?purge=true' : ''
  return request(`/rooms/${encodeURIComponent(roomId)}${q}`, { method: 'DELETE' })
}

export const disableRoom = roomId =>
  roomParam(roomId).then(rid =>
    request(`/rooms/${encodeURIComponent(rid)}/disable`, { method: 'POST' }),
  )

export const enableRoom = roomId =>
  roomParam(roomId).then(rid =>
    request(`/rooms/${encodeURIComponent(rid)}/enable`, { method: 'POST' }),
  )
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
export const getInsights = (roomId) =>
  roomParam(roomId).then((rid) =>
    request(`/insights?room_id=${encodeURIComponent(rid)}`),
  )

// ── Export ───────────────────────────────────────────────────────────────────
export async function downloadExport(format = 'csv', roomId) {
  const rid = await roomParam(roomId)
  const res = await fetch(`${BASE}/export/${format}?room_id=${encodeURIComponent(rid)}`)
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
  const rid = await roomParam(roomId)
  const q = new URLSearchParams({ room_id: rid, limit: String(limit) }).toString()
  return request(`/ai/decisions?${q}`)
}

// ── WebSocket live updates (exactly one logical subscription per caller) ─────
/**
 * One socket per call; subscribe with room_id on connect. Reconnects unless close() was used.
 * Messages for a different room_id are dropped (defense-in-depth — server is also room-scoped).
 */
export function connectLive(roomId, onMessage, onError) {
  const rid = roomId != null ? String(roomId).trim() : ''
  if (!rid) {
    if (onError) onError(new Error('room_id is required'))
    return { close: () => {}, ws: null }
  }

  let ws = null
  let closedManually = false

  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const wsPath = INGRESS_PATH + '/ws'

  function connect() {
    if (closedManually) return
    ws = new WebSocket(`${proto}://${location.host}${wsPath}`)

    ws.onopen = () => {
      try {
        ws.send(JSON.stringify({ type: 'subscribe', room_id: rid }))
      } catch (e) {
        if (onError) onError(e)
      }
    }

    ws.onmessage = (event) => {
      let data
      try {
        data = JSON.parse(event.data)
      } catch {
        return
      }
      if (
        data.room_id != null &&
        normalizeRoomKey(data.room_id) !== normalizeRoomKey(rid)
      ) {
        console.warn('[HawaAI] Ignoring WS payload for wrong room:', data.room_id, 'expected', rid)
        return
      }
      try {
        onMessage(data)
      } catch (e) {
        if (onError) onError(e)
      }
    }

    ws.onerror = onError || (() => {})

    ws.onclose = () => {
      if (!closedManually) {
        setTimeout(connect, 2000 + Math.random() * 1000)
      }
    }
  }

  connect()

  return {
    close: () => {
      closedManually = true
      try {
        ws?.close()
      } catch {
        /* ignore */
      }
    },
    get ws() {
      return ws
    },
  }
}
