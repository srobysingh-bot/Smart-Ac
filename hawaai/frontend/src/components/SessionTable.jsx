import { useEffect, useMemo, useState } from 'react'
import { getSessions } from '../api/smartcool.js'
import { ChevronDown, ChevronUp } from 'lucide-react'

function fmt(v, decimals = 1, suffix = '') {
  return v != null ? `${Number(v).toFixed(decimals)}${suffix}` : '--'
}

function formatDateTime(iso) {
  if (!iso) return '--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--'
  return d.toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatGroupDate(iso) {
  if (!iso) return 'Unknown date'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Unknown date'
  return d.toLocaleDateString([], { year: 'numeric', month: 'long', day: 'numeric' })
}

function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return null
  const mins = (new Date(endIso) - new Date(startIso)) / 60000
  if (mins < 0) return null
  if (mins < 60) return `${Math.round(mins)}m`
  return `${Math.floor(mins / 60)}h ${Math.round(mins % 60)}m`
}

const REASON_COLORS = {
  cooled: 'text-green-400',
  vacant: 'text-yellow-400',
  manual: 'text-gray-400',
  manual_off: 'text-gray-400',
  schedule: 'text-blue-400',
}

function sessionQuality(s) {
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)

  if (s.valid === true) return 'good'
  if (s.valid === false) return (dt != null && dt >= 0.3) ? 'weak' : 'invalid'

  const dur = s.duration_minutes ??
    (s.start_time && s.end_time
      ? (new Date(s.end_time) - new Date(s.start_time)) / 60000
      : null)
  if (dur != null && dur >= 3 && dt != null && dt >= 0.3) return 'good'
  if (dt != null && dt >= 0.3) return 'weak'
  return 'invalid'
}

const QUALITY_BADGE = {
  good: { label: 'Good', dot: 'bg-green-400', text: 'text-green-400' },
  weak: { label: 'Weak', dot: 'bg-yellow-400', text: 'text-yellow-400' },
  invalid: { label: 'Invalid', dot: 'bg-red-500', text: 'text-red-400' },
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

function StorageBadge({ session }) {
  const stored = session.is_record_valid !== 0 && session.provisional !== 1
  return (
    <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${
      stored ? 'bg-green-900/40 text-green-300' : 'bg-red-900/40 text-red-300'
    }`}>
      {stored ? 'Stored' : 'Invalid'}
    </span>
  )
}

function costDisplay(session) {
  if (session.energy_consumed_kwh == null || session.cost_estimate == null) return '--'
  return `₹${Number(session.cost_estimate).toFixed(2)}`
}

function groupByDate(rows) {
  const groups = []
  for (const row of rows) {
    const key = formatGroupDate(row.start_time)
    let group = groups.find(g => g.key === key)
    if (!group) {
      group = { key, rows: [] }
      groups.push(group)
    }
    group.rows.push(row)
  }
  return groups
}

export default function SessionTable({ limit = 10, roomId }) {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [showInvalid, setShowInvalid] = useState(false)

  useEffect(() => {
    if (!roomId) {
      setSessions([])
      setLoading(false)
      return
    }
    setLoading(true)
    getSessions({ limit, room_id: roomId })
      .then(r => setSessions(r.sessions || []))
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [limit, roomId])

  const enriched = useMemo(
    () => sessions.map(s => ({ ...s, _quality: sessionQuality(s) })),
    [sessions],
  )
  const visible = enriched.filter(s => s._quality !== 'invalid')
  const invalidRows = enriched.filter(s => s._quality === 'invalid')
  const toRender = showInvalid ? [...visible, ...invalidRows] : visible
  const grouped = groupByDate(toRender)

  if (!roomId) {
    return <p className="text-sm text-gray-500 py-4 text-center">Select a room to list sessions.</p>
  }

  if (loading) {
    return <p className="text-sm text-gray-500 py-4 text-center">Loading...</p>
  }
  if (!sessions.length) {
    return <p className="text-sm text-gray-600 py-4 text-center">No valid sessions recorded yet</p>
  }

  return (
    <div className="overflow-x-auto">
      {toRender.length === 0 ? (
        <p className="text-sm text-gray-600 py-4 text-center">No valid sessions recorded yet</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
              <th className="pb-2 pr-3">Start</th>
              <th className="pb-2 pr-3">End</th>
              <th className="pb-2 pr-3">Duration</th>
              <th className="pb-2 pr-3">Delta Temp</th>
              <th className="pb-2 pr-3">kWh</th>
              <th className="pb-2 pr-3">Cost</th>
              <th className="pb-2 pr-3">Stored</th>
              <th className="pb-2 pr-3">Reason</th>
              <th className="pb-2">Quality</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/50">
            {grouped.map(group => (
              <FragmentRows key={group.key} group={group} />
            ))}
          </tbody>
        </table>
      )}

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

function FragmentRows({ group }) {
  return (
    <>
      <tr className="bg-gray-900/40">
        <td colSpan={9} className="py-2 pr-3 text-xs font-semibold text-blue-300">
          {group.key}
        </td>
      </tr>
      {group.rows.map(s => {
        const delta = s.delta_temp ??
          (s.indoor_temp_start != null && s.indoor_temp_end != null
            ? s.indoor_temp_start - s.indoor_temp_end
            : null)
        const isInvalid = s._quality === 'invalid'
        const duration = formatDuration(s.start_time, s.end_time) ??
          (s.time_to_cool_minutes != null ? `${Math.round(s.time_to_cool_minutes)}m` : null)
        return (
          <tr
            key={s.session_id}
            className={`hover:bg-gray-800/30 transition-colors ${isInvalid ? 'opacity-40' : ''}`}
          >
            <td className="py-2 pr-3 text-gray-400 whitespace-nowrap">{formatDateTime(s.start_time)}</td>
            <td className="py-2 pr-3 text-gray-400 whitespace-nowrap">{formatDateTime(s.end_time)}</td>
            <td className="py-2 pr-3 text-gray-300">{duration ?? '--'}</td>
            <td className="py-2 pr-3">
              {delta != null ? <span className="text-blue-400">-{Number(delta).toFixed(1)}°C</span> : '--'}
            </td>
            <td className="py-2 pr-3">{fmt(s.energy_consumed_kwh, 3)}</td>
            <td className="py-2 pr-3 text-yellow-400">{costDisplay(s)}</td>
            <td className="py-2 pr-3"><StorageBadge session={s} /></td>
            <td className={`py-2 pr-3 text-xs font-medium ${REASON_COLORS[s.reason_stopped] || 'text-gray-500'}`}>
              {s.reason_stopped || '--'}
            </td>
            <td className="py-2"><QualityBadge quality={s._quality} /></td>
          </tr>
        )
      })}
    </>
  )
}
