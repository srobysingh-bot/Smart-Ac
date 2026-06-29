export function normalizeRoomId(value) {
  return String(value || '').trim().toLowerCase()
}

export function telemetryPresentation(status = {}) {
  return {
    powerStatus: String(status.power_telemetry_status || status.telemetry_status || 'not_configured').toLowerCase(),
    kwhStatus: String(status.kwh_telemetry_status || 'not_configured').toLowerCase(),
  }
}

export function authoritativeSessionDisplay(status = {}) {
  const runtime = status.runtime || status
  const continuity = Boolean(runtime.active_session_continuity_confirmed)
  const rawElapsed = Number(runtime.active_session_elapsed_seconds)
  const elapsedSeconds = continuity && Number.isFinite(rawElapsed) && rawElapsed >= 0
    ? Math.floor(rawElapsed)
    : null
  const reconnecting = !continuity && (
    runtime.active_session_state === 'reconnecting'
    || runtime.active_session_recovery_state === 'recovery_pending'
  )
  return {
    elapsedSeconds,
    reconnecting,
    startedAt: continuity ? (runtime.active_session_started_at || null) : null,
  }
}

export function filterSessionsForRoom(rows, roomId) {
  const canonical = normalizeRoomId(roomId)
  return (Array.isArray(rows) ? rows : []).filter(
    row => normalizeRoomId(row?.room_id) === canonical,
  )
}
