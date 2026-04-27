import { useEffect, useState } from 'react'
import { getSessions } from '../api/smartcool.js'
import { ChevronDown, ChevronUp } from 'lucide-react'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(v, decimals = 1, suffix = '') {
  return v != null ? `${Number(v).toFixed(decimals)}${suffix}` : '—'
}

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return null
  const mins = (new Date(endIso) - new Date(startIso)) / 60000
  if (mins < 0) return null
  if (mins < 60) return `${Math.round(mins)}m`
  return `${Math.floor(mins / 60)}h ${Math.round(mins % 60)}m`
}

const REASON_COLORS = {
  cooled:   'text-green-400',
  vacant:   'text-yellow-400',
  manual:   'text-gray-400',
  manual_off: 'text-gray-400',
  schedule: 'text-blue-400',
}

/**
 * Classify session quality:
 *  valid === true               → good
 *  valid === false, dt ≥ 0.3   → weak (some cooling but failed another criterion)
 *  valid === false, dt < 0.3   → invalid (no meaningful cooling)
 *
 * Falls back to computing delta_temp when server hasn't enriched the row.
 */
function sessionQuality(s) {
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)

  if (s.valid === true)  return 'good'
  if (s.valid === false) return (dt != null && dt >= 0.3) ? 'weak' : 'invalid'

  // No `valid` field (old sessions without _enrich_session) — derive from raw data
  const dur = s.duration_minutes ??
    (s.start_time && s.end_time
      ? (new Date(s.end_time) - new Date(s.start_time)) / 60000
      : null)
  if (dur != null && dur >= 3 && dt != null && dt >= 0.3) return 'good'
  if (dt != null && dt >= 0.3) return 'weak'
  return 'invalid'
}

const QUALITY_BADGE = {
  good:    { label: 'Good',    dot: 'bg-green-400',  text: 'text-green-400'  },
  weak:    { label: 'Weak',    dot: 'bg-yellow-400', text: 'text-yellow-400' },
  invalid: { label: 'Invalid', dot: 'bg-red-500',    text: 'text-red-400'    },
}

function QualityBadge({ quality }) {
  const q = QUALITY_BADGE[quality] || QUALITY_BADGE.invalid
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`w-1.5 h-1.5 rounded-full ${q.dot}`} />
      <span className={`text-xs font-medium ${q.text}`}>{q.label}</span>
    </span>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function SessionTable({ limit = 10 }) {
  const [sessions,    setSessions]    = useState([])
  const [loading,     setLoading]     = useState(true)
  const [showInvalid, setShowInvalid] = useState(false)

  useEffect(() => {
    getSessions({ limit })
      .then(r => setSessions(r.sessions || []))
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [limit])

  if (loading) {
    return <p className="text-sm text-gray-500 py-4 text-center">Loading…</p>
  }
  if (!sessions.length) {
    return <p className="text-sm text-gray-600 py-4 text-center">No sessions recorded yet</p>
  }

  // Partition by quality
  const enriched    = sessions.map(s => ({ ...s, _quality: sessionQuality(s) }))
  const visible     = enriched.filter(s => s._quality !== 'invalid')
  const invalidRows = enriched.filter(s => s._quality === 'invalid')

  const toRender = showInvalid ? [...visible, ...invalidRows] : visible

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
            <th className="pb-2 pr-3">Start</th>
            <th className="pb-2 pr-3">End</th>
            <th className="pb-2 pr-3">Duration</th>
            <th className="pb-2 pr-3">Δ Temp</th>
            <th className="pb-2 pr-3">kWh</th>
            <th className="pb-2 pr-3">Cost</th>
            <th className="pb-2 pr-3">Reason</th>
            <th className="pb-2">Quality</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800/50">
          {toRender.map(s => {
            const delta = s.delta_temp ??
              (s.indoor_temp_start != null && s.indoor_temp_end != null
                ? s.indoor_temp_start - s.indoor_temp_end
                : null)
            const isInvalid = s._quality === 'invalid'
            const duration  = formatDuration(s.start_time, s.end_time) ??
              (s.time_to_cool_minutes != null ? `${Math.round(s.time_to_cool_minutes)}m` : null)
            return (
              <tr
                key={s.session_id}
                className={`hover:bg-gray-800/30 transition-colors ${isInvalid ? 'opacity-40' : ''}`}
              >
                <td className="py-2 pr-3 text-gray-400">{formatTime(s.start_time)}</td>
                <td className="py-2 pr-3 text-gray-400">{formatTime(s.end_time)}</td>
                <td className="py-2 pr-3 text-gray-300">{duration ?? '—'}</td>
                <td className="py-2 pr-3">
                  {delta != null
                    ? <span className="text-blue-400">−{delta.toFixed(1)}°C</span>
                    : '—'}
                </td>
                <td className="py-2 pr-3">{fmt(s.energy_consumed_kwh, 3)}</td>
                <td className="py-2 pr-3 text-yellow-400">
                  {s.cost_estimate != null ? `₹${s.cost_estimate.toFixed(2)}` : '—'}
                </td>
                <td className={`py-2 pr-3 text-xs font-medium ${REASON_COLORS[s.reason_stopped] || 'text-gray-500'}`}>
                  {s.reason_stopped || '—'}
                </td>
                <td className="py-2">
                  <QualityBadge quality={s._quality} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {/* Toggle invalid sessions */}
      {invalidRows.length > 0 && (
        <button
          onClick={() => setShowInvalid(v => !v)}
          className="flex items-center gap-1.5 mt-3 text-xs text-gray-500 hover:text-gray-300 transition-colors"
        >
          {showInvalid ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          {showInvalid
            ? `Hide ${invalidRows.length} low-quality session${invalidRows.length !== 1 ? 's' : ''}`
            : `Show ${invalidRows.length} low-quality session${invalidRows.length !== 1 ? 's' : ''}`}
        </button>
      )}
    </div>
  )
}
