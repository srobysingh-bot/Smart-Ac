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
export const getStatus = () => request('/status')

// ── Sessions ─────────────────────────────────────────────────────────────────
export const getSessions = (params = {}) => {
  const q = new URLSearchParams(params).toString()
  return request(`/sessions${q ? '?' + q : ''}`)
}
export const getSessionStats = () => request('/sessions/stats')

// ── Snapshots ────────────────────────────────────────────────────────────────
export const getSnapshots = (minutes = 120) =>
  request(`/snapshots?minutes=${minutes}`)

// ── Daily stats ───────────────────────────────────────────────────────────────
export const getDailyStats = (days = 7) => request(`/daily?days=${days}`)

// ── Config ───────────────────────────────────────────────────────────────────
export const getConfig   = () => request('/config')
export const patchConfig = (data) =>
  request('/config', { method: 'POST', body: JSON.stringify(data) })
export const reloadConfig = () => request('/config/reload', { method: 'POST' })

// ── Brands ───────────────────────────────────────────────────────────────────
export const getBrands = () => request('/brands')

// ── HA Entities ──────────────────────────────────────────────────────────────
export const getEntities = (domain) => {
  const q = domain ? `?domain=${domain}` : ''
  return request(`/entities${q}`)
}

// ── Export ───────────────────────────────────────────────────────────────────
export async function downloadExport(format = 'csv') {
  const res = await fetch(`${BASE}/export/${format}`)
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

// ── WebSocket live updates ────────────────────────────────────────────────────
export function connectLive(onMessage, onError) {
  const proto  = location.protocol === 'https:' ? 'wss' : 'ws'
  // Include ingress path prefix so the request routes through HA ingress
  // e.g.  wss://ha-host/api/hassio_ingress/TOKEN/ws  (via ingress)
  //   or  ws://localhost:8099/ws                       (direct access)
  const wsPath = INGRESS_PATH + '/ws'
  const ws     = new WebSocket(`${proto}://${location.host}${wsPath}`)

  ws.onmessage = (evt) => {
    try { onMessage(JSON.parse(evt.data)) } catch {}
  }
  ws.onerror = onError || (() => {})
  ws.onclose = () => {
    // Auto-reconnect after 5 s
    setTimeout(() => connectLive(onMessage, onError), 5000)
  }
  return ws
}
