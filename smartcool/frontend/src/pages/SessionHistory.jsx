import { useCallback, useEffect, useState } from 'react'
import { getSessions } from '../api/smartcool.js'
import { ChevronLeft, ChevronRight, ChevronDown, ChevronUp, Filter } from 'lucide-react'

const PAGE_SIZE = 20

// ── Helpers ───────────────────────────────────────────────────────────────────

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

/**
 * Classify session quality using backend-computed fields when available.
 *  valid === true              → good
 *  valid === false, dt ≥ 0.3  → weak
 *  valid === false, dt < 0.3  → invalid
 */
function sessionQuality(s) {
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)

  if (s.valid === true)  return 'good'
  if (s.valid === false) return (dt != null && dt >= 0.3) ? 'weak' : 'invalid'

  // Legacy sessions without enrichment
  const dur = s.duration_minutes ??
    (s.start_time && s.end_time
      ? (new Date(s.end_time) - new Date(s.start_time)) / 60000
      : null)
  if (dur != null && dur >= 3 && dt != null && dt >= 0.3) return 'good'
  if (dt != null && dt >= 0.3) return 'weak'
  return 'invalid'
}

/** Fast cooling: cooling_rate > 0.5 °C/min, or derive from delta/duration. */
function isFastCooling(s) {
  if (s.cooling_rate != null) return s.cooling_rate > 0.5
  if (s.cooling_type === 'fast') return true
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)
  const dur = s.duration_minutes ??
    (s.start_time && s.end_time
      ? (new Date(s.end_time) - new Date(s.start_time)) / 60000
      : null)
  return dt != null && dur != null && dur > 0 && (dt / dur) > 0.5
}

// ── Sub-components ────────────────────────────────────────────────────────────

function ReasonBadge({ reason }) {
  const MAP = {
    cooled:    'bg-green-900/50  text-green-300',
    vacant:    'bg-yellow-900/50 text-yellow-300',
    manual:    'bg-gray-700      text-gray-300',
    manual_off:'bg-gray-700      text-gray-300',
    schedule:  'bg-blue-900/50   text-blue-300',
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${MAP[reason] || 'bg-gray-800 text-gray-400'}`}>
      {reason || 'unknown'}
    </span>
  )
}

const QUALITY_CFG = {
  good:    { label: 'Good',    dot: 'bg-green-400',  text: 'text-green-400'  },
  weak:    { label: 'Weak',    dot: 'bg-yellow-400', text: 'text-yellow-400' },
  invalid: { label: 'Invalid', dot: 'bg-red-500',    text: 'text-red-400'    },
}

function QualityBadge({ quality }) {
  const q = QUALITY_CFG[quality] || QUALITY_CFG.invalid
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`w-1.5 h-1.5 rounded-full ${q.dot}`} />
      <span className={`text-xs font-medium ${q.text}`}>{q.label}</span>
    </span>
  )
}

// ── Filter bar ────────────────────────────────────────────────────────────────

const FILTER_OPTS = [
  { id: 'all',   label: 'All' },
  { id: 'valid', label: 'Valid only' },
  { id: 'fast',  label: 'Fast cooling' },
]

function FilterBar({ active, onChange }) {
  return (
    <div className="flex items-center gap-2">
      <Filter size={13} className="text-gray-500" />
      <div className="flex bg-gray-900 border border-gray-800 rounded-lg overflow-hidden text-xs">
        {FILTER_OPTS.map(opt => (
          <button
            key={opt.id}
            onClick={() => onChange(opt.id)}
            className={`px-3 py-1.5 transition-colors ${
              active === opt.id ? 'bg-blue-600 text-white' : 'text-gray-400 hover:bg-gray-800'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function SessionHistory() {
  const [sessions,    setSessions]    = useState([])
  const [total,       setTotal]       = useState(0)
  const [page,        setPage]        = useState(0)
  const [dateFrom,    setDateFrom]    = useState('')
  const [dateTo,      setDateTo]      = useState('')
  const [filter,      setFilter]      = useState('all')   // 'all' | 'valid' | 'fast'
  const [showInvalid, setShowInvalid] = useState(false)
  const [loading,     setLoading]     = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE }
    if (dateFrom) params.date_from = dateFrom
    if (dateTo)   params.date_to   = dateTo + 'T23:59:59'
    getSessions(params)
      .then(r => { setSessions(r.sessions || []); setTotal(r.total || 0) })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [page, dateFrom, dateTo])

  useEffect(() => { load() }, [load])

  // Client-side enrichment & filtering (valid / fast come from _enrich_session server-side)
  const enriched = sessions.map(s => ({
    ...s,
    _quality: sessionQuality(s),
    _fast:    isFastCooling(s),
  }))

  let displayed = enriched
  if (filter === 'valid') displayed = enriched.filter(s => s._quality === 'good')
  if (filter === 'fast')  displayed = enriched.filter(s => s._fast)

  // When not using a filter: split good/weak vs invalid for collapse
  const validRows   = filter === 'all' ? displayed.filter(s => s._quality !== 'invalid') : displayed
  const invalidRows = filter === 'all' ? displayed.filter(s => s._quality === 'invalid') : []
  const toRender    = filter === 'all' && !showInvalid ? validRows : displayed

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
        <div className="ml-auto">
          <FilterBar active={filter} onChange={f => { setFilter(f); setShowInvalid(false); setPage(0) }} />
        </div>
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
              <th className="pb-3 pr-4">Quality</th>
              <th className="pb-3">Reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {loading ? (
              <tr>
                <td colSpan={10} className="py-8 text-center text-gray-500">Loading…</td>
              </tr>
            ) : toRender.length === 0 ? (
              <tr>
                <td colSpan={10} className="py-8 text-center text-gray-500">
                  {filter !== 'all' ? 'No sessions match this filter' : 'No sessions found'}
                </td>
              </tr>
            ) : toRender.map(s => {
              const deltaTemp = s.delta_temp ??
                (s.indoor_temp_start != null && s.indoor_temp_end != null
                  ? (s.indoor_temp_start - s.indoor_temp_end).toFixed(1)
                  : null)
              const isInvalid = s._quality === 'invalid'
              return (
                <tr
                  key={s.session_id}
                  className={`hover:bg-gray-800/40 transition-colors ${isInvalid ? 'opacity-40' : ''}`}
                >
                  <td className="py-2.5 pr-4 text-gray-400">{formatDate(s.start_time)}</td>
                  <td className="py-2.5 pr-4">{formatTime(s.start_time)}</td>
                  <td className="py-2.5 pr-4 text-gray-400">{formatTime(s.end_time)}</td>
                  <td className="py-2.5 pr-4">{formatDuration(s.start_time, s.end_time)}</td>
                  <td className="py-2.5 pr-4">
                    {deltaTemp != null
                      ? <span className="text-blue-400">−{Number(deltaTemp).toFixed(1)}°C</span>
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4">
                    {s.time_to_cool_minutes != null ? `${s.time_to_cool_minutes.toFixed(0)} min` : '—'}
                  </td>
                  <td className="py-2.5 pr-4">
                    {s.energy_consumed_kwh != null ? `${s.energy_consumed_kwh.toFixed(3)}` : '—'}
                  </td>
                  <td className="py-2.5 pr-4 text-yellow-400">
                    {s.cost_estimate != null ? `₹${s.cost_estimate.toFixed(2)}` : '—'}
                  </td>
                  <td className="py-2.5 pr-4">
                    <QualityBadge quality={s._quality} />
                    {s._fast && (
                      <span className="ml-2 text-xs text-green-400 font-medium">⚡ Fast</span>
                    )}
                  </td>
                  <td className="py-2.5">
                    <ReasonBadge reason={s.reason_stopped} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {/* Collapse invalid rows toggle */}
        {filter === 'all' && invalidRows.length > 0 && (
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
