import { useCallback, useEffect, useState } from 'react'
import { getSessions } from '../api/smartcool.js'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'

const PAGE_SIZE = 20

function formatDuration(start, end) {
  if (!start || !end) return '—'
  const diff = (new Date(end) - new Date(start)) / 60000
  if (diff < 60) return `${diff.toFixed(0)}m`
  return `${Math.floor(diff / 60)}h ${Math.round(diff % 60)}m`
}

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function ReasonBadge({ reason }) {
  const MAP = {
    cooled:   'bg-green-900/50 text-green-300',
    vacant:   'bg-yellow-900/50 text-yellow-300',
    manual:   'bg-gray-700 text-gray-300',
    schedule: 'bg-blue-900/50 text-blue-300',
  }
  const cls = MAP[reason] || 'bg-gray-800 text-gray-400'
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {reason || 'unknown'}
    </span>
  )
}

export default function SessionHistory() {
  const [sessions, setSessions] = useState([])
  const [total,    setTotal]    = useState(0)
  const [page,     setPage]     = useState(0)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo,   setDateTo]   = useState('')
  const [loading,  setLoading]  = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE }
    if (dateFrom) params.date_from = dateFrom
    if (dateTo)   params.date_to   = dateTo + 'T23:59:59'
    getSessions(params)
      .then(r => { setSessions(r.sessions); setTotal(r.total) })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [page, dateFrom, dateTo])

  useEffect(() => { load() }, [load])

  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Session History</h1>
        <span className="text-sm text-gray-500">{total} total sessions</span>
      </div>

      {/* Filters */}
      <div className="card flex flex-wrap items-end gap-4">
        <div>
          <label className="text-xs text-gray-500 block mb-1">From</label>
          <input
            type="date"
            value={dateFrom}
            onChange={e => { setDateFrom(e.target.value); setPage(0) }}
            className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 block mb-1">To</label>
          <input
            type="date"
            value={dateTo}
            onChange={e => { setDateTo(e.target.value); setPage(0) }}
            className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
          />
        </div>
        <button
          onClick={() => { setDateFrom(''); setDateTo(''); setPage(0) }}
          className="px-3 py-2 text-sm text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg hover:border-gray-600 transition-colors"
        >
          Clear
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
              <th className="pb-3 pr-4">Date</th>
              <th className="pb-3 pr-4">Start</th>
              <th className="pb-3 pr-4">End</th>
              <th className="pb-3 pr-4">Duration</th>
              <th className="pb-3 pr-4">Δ Temp</th>
              <th className="pb-3 pr-4">Cool Time</th>
              <th className="pb-3 pr-4">kWh</th>
              <th className="pb-3 pr-4">Cost</th>
              <th className="pb-3">Reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {loading ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-gray-500">Loading…</td>
              </tr>
            ) : sessions.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-gray-500">No sessions found</td>
              </tr>
            ) : sessions.map(s => {
              const deltaTemp = s.indoor_temp_start != null && s.indoor_temp_end != null
                ? (s.indoor_temp_start - s.indoor_temp_end).toFixed(1)
                : null
              return (
                <tr key={s.session_id} className="hover:bg-gray-800/40 transition-colors">
                  <td className="py-2.5 pr-4 text-gray-400">{formatDate(s.start_time)}</td>
                  <td className="py-2.5 pr-4">{formatTime(s.start_time)}</td>
                  <td className="py-2.5 pr-4 text-gray-400">{formatTime(s.end_time)}</td>
                  <td className="py-2.5 pr-4">{formatDuration(s.start_time, s.end_time)}</td>
                  <td className="py-2.5 pr-4">
                    {deltaTemp != null
                      ? <span className="text-blue-400">−{deltaTemp}°C</span>
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4">
                    {s.time_to_cool_minutes != null
                      ? `${s.time_to_cool_minutes.toFixed(0)} min`
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4">
                    {s.energy_consumed_kwh != null
                      ? `${s.energy_consumed_kwh.toFixed(3)}`
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4 text-yellow-400">
                    {s.cost_estimate != null ? `₹${s.cost_estimate.toFixed(2)}` : '—'}
                  </td>
                  <td className="py-2.5">
                    <ReasonBadge reason={s.reason_stopped} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-4 pt-2">
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            className="p-1.5 rounded-lg hover:bg-gray-800 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={18} />
          </button>
          <span className="text-sm text-gray-400">
            Page {page + 1} of {totalPages}
          </span>
          <button
            onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            className="p-1.5 rounded-lg hover:bg-gray-800 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronRight size={18} />
          </button>
        </div>
      )}
    </div>
  )
}
