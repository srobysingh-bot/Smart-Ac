import test from 'node:test'
import assert from 'node:assert/strict'

import {
  authoritativeSessionDisplay,
  filterSessionsForRoom,
  telemetryPresentation,
} from '../src/utils/sessionTelemetry.js'

test('power healthy and energy meter offline remain separate', () => {
  assert.deepEqual(telemetryPresentation({
    power_telemetry_status: 'healthy',
    kwh_telemetry_status: 'offline',
  }), { powerStatus: 'healthy', kwhStatus: 'offline' })
})

test('timer consumes backend elapsed rather than deriving from start time', () => {
  const view = authoritativeSessionDisplay({
    active_session_continuity_confirmed: true,
    active_session_elapsed_seconds: 42,
    active_session_started_at: '2020-01-01T00:00:00Z',
  })
  assert.equal(view.elapsedSeconds, 42)
})

test('false long runtime is hidden when continuity is not confirmed', () => {
  const view = authoritativeSessionDisplay({
    active_session_continuity_confirmed: false,
    active_session_elapsed_seconds: 78 * 3600,
    active_session_started_at: '2020-01-01T00:00:00Z',
  })
  assert.equal(view.elapsedSeconds, null)
  assert.equal(view.startedAt, null)
})

test('power recovery pending renders reconnecting state', () => {
  const view = authoritativeSessionDisplay({
    active_session_continuity_confirmed: false,
    active_session_state: 'reconnecting',
    active_session_recovery_state: 'recovery_pending',
  })
  assert.equal(view.reconnecting, true)
})

test('session history remains scoped to canonical room', () => {
  const rows = filterSessionsForRoom([
    { room_id: ' Study Room ', id: 'study' },
    { room_id: 'Dining Room', id: 'dining' },
  ], 'study room')
  assert.deepEqual(rows.map(row => row.id), ['study'])
})
