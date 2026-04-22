import { useEffect, useState } from 'react'
import { getSessions } from '../api/smartcool.js'

function fmt(v, decimals = 1, suffix = '') {
  return v != null ? `${Number(v).toFixed(decimals)}${suffix}` : '—'
}

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const REASON_COLORS = {
  cooled:   'text-green-400',
  vacant:   'text-yellow-400',
  manual:   'text-gray-400',
  schedule: 'text-blue-400',
}

export default function SessionTable({ limit = 10 }) {
  const [sessions, setSessions] = useState([])
  const [loading,  setLoading]  = useState(true)

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

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
            <th className="pb-2 pr-3">Start</th>
            <th className="pb-2 pr-3">Δ Temp</th>
            <th className="pb-2 pr-3">Time to Cool</th>
            <th className="pb-2 pr-3">kWh</th>
            <th className="pb-2 pr-3">Cost</th>
            <th className="pb-2">Reason</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800/50">
          {sessions.map(s => {
            const delta = s.indoor_temp_start != null && s.indoor_temp_end != null
              ? (s.indoor_temp_start - s.indoor_temp_end)
              : null
            return (
              <tr key={s.session_id} className="hover:bg-gray-800/30 transition-colors">
                <td className="py-2 pr-3 text-gray-400">{formatTime(s.start_time)}</td>
                <td className="py-2 pr-3">
                  {delta != null
                    ? <span className="text-blue-400">−{delta.toFixed(1)}°C</span>
                    : '—'}
                </td>
                <td className="py-2 pr-3">{fmt(s.time_to_cool_minutes, 0, ' min')}</td>
                <td className="py-2 pr-3">{fmt(s.energy_consumed_kwh, 3)}</td>
                <td className="py-2 pr-3 text-yellow-400">{s.cost_estimate != null ? `₹${s.cost_estimate.toFixed(2)}` : '—'}</td>
                <td className={`py-2 text-xs font-medium ${REASON_COLORS[s.reason_stopped] || 'text-gray-500'}`}>
                  {s.reason_stopped || '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
