import { useCallback, useEffect, useMemo, useState } from 'react'
import { getSessions } from '../api/smartcool.js'
import { useRoom } from '../context/RoomContext.jsx'
import { ChevronLeft, ChevronRight, ChevronDown, ChevronUp, Filter } from 'lucide-react'

const PAGE_SIZE = 20

function formatDuration(start, end) {
  if (!start || !end) return '--'
  const diff = (new Date(end) - new Date(start)) / 60000
  if (!Number.isFinite(diff) || diff < 0) return '--'
  if (diff < 60) return `${diff.toFixed(0)}m`
  return `${Math.floor(diff / 60)}h ${Math.round(diff % 60)}m`
}

function finalizedDurationMinutes(s) {
  if (!s.start_time || !s.end_time) return null
  const mins = (new Date(s.end_time) - new Date(s.start_time)) / 60000
  return Number.isFinite(mins) && mins >= 0 ? mins : null
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

function sessionQuality(s) {
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)

  if (s.valid === true) return 'good'
  if (s.valid === false) return (dt != null && dt >= 0.3) ? 'weak' : 'invalid'

  const dur = finalizedDurationMinutes(s)
  if (dur != null && dur >= 3 && dt != null && dt >= 0.3) return 'good'
  if (dt != null && dt >= 0.3) return 'weak'
  return 'invalid'
}

function isFastCooling(s) {
  if (s.cooling_rate != null) return s.cooling_rate > 0.5
  if (s.cooling_type === 'fast') return true
  const dt = s.delta_temp ??
    (s.indoor_temp_start != null && s.indoor_temp_end != null
      ? s.indoor_temp_start - s.indoor_temp_end
      : null)
  const dur = finalizedDurationMinutes(s)
  return dt != null && dur != null && dur > 0 && (dt / dur) > 0.5
}

function ReasonBadge({ reason }) {
  const map = {
    cooled: 'bg-green-900/50 text-green-300',
    vacant: 'bg-yellow-900/50 text-yellow-300',
    manual: 'bg-gray-700 text-gray-300',
    manual_off: 'bg-gray-700 text-gray-300',
    auto_comfort: 'bg-cyan-900/60 text-cyan-200',
    schedule: 'bg-blue-900/50 text-blue-300',
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${map[reason] || 'bg-gray-800 text-gray-400'}`}>
      {reason || 'unknown'}
    </span>
  )
}

const QUALITY_CFG = {
  good: { label: 'Good', dot: 'bg-green-400', text: 'text-green-400' },
  weak: { label: 'Weak', dot: 'bg-yellow-400', text: 'text-yellow-400' },
  invalid: { label: 'Invalid', dot: 'bg-red-500', text: 'text-red-400' },
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

function costDisplay(session, tariffPerKwh = 8.0) {
  const kwh = Number(session.energy_consumed_kwh ?? session.kwh ?? 0)
  if (!Number.isFinite(kwh) || kwh <= 0) return '\u20b90.00'

  const tariff = Number(
    tariffPerKwh
      ?? session.power_tariff_per_kwh
      ?? session.energy_tariff_per_kwh
      ?? 8.0,
  )
  const safeTariff = Number.isFinite(tariff) && tariff >= 0 ? tariff : 8.0
  const cost = kwh * safeTariff
  return `\u20b9${cost.toFixed(2)}`
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

const FILTER_OPTS = [
  { id: 'all', label: 'All' },
  { id: 'valid', label: 'Valid only' },
  { id: 'fast', label: 'Fast cooling' },
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

export default function SessionHistory() {
  const { rooms, activeRoomId, setActiveRoom } = useRoom()
  const [sessions, setSessions] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [filter, setFilter] = useState('all')
  const [showInvalid, setShowInvalid] = useState(false)
  const [loading, setLoading] = useState(false)
  const [tariffPerKwh, setTariffPerKwh] = useState(8.0)

  useEffect(() => {
    setPage(0)
  }, [activeRoomId])

  const load = useCallback(() => {
    if (!activeRoomId) {
      setSessions([])
      setTotal(0)
      setTariffPerKwh(8.0)
      setLoading(false)
      return
    }
    setLoading(true)
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE, room_id: activeRoomId }
    if (dateFrom) params.date_from = dateFrom
    if (dateTo) params.date_to = `${dateTo}T23:59:59`
    getSessions(params)
      .then(r => {
        setSessions(r.sessions || [])
        setTotal(r.total || 0)
        setTariffPerKwh(Number(r.tariff_per_kwh ?? 8.0))
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [page, dateFrom, dateTo, activeRoomId])

  useEffect(() => { load() }, [load])

  const enriched = useMemo(() => sessions.map(s => ({
    ...s,
    _quality: sessionQuality(s),
    _fast: isFastCooling(s),
  })), [sessions])

  let displayed = enriched
  if (filter === 'valid') displayed = enriched.filter(s => s._quality === 'good')
  if (filter === 'fast') displayed = enriched.filter(s => s._fast)

  const validRows = filter === 'all' ? displayed.filter(s => s._quality !== 'invalid') : displayed
  const invalidRows = filter === 'all' ? displayed.filter(s => s._quality === 'invalid') : []
  const toRender = filter === 'all' && !showInvalid ? validRows : displayed
  const groupedRows = groupByDate(toRender)
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="container-app px-4 sm:px-6 py-4 sm:py-6 pb-24 md:pb-12 space-y-4 min-w-0">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-xl font-bold">Session History</h1>
        <span className="text-sm text-gray-500 shrink-0">{activeRoomId ? `${total} sessions (this room)` : 'Select a room'}</span>
      </div>

      <div className="card flex flex-col sm:flex-row flex-wrap items-stretch gap-3">
        <div className="flex-1 min-w-0">
          <label className="text-xs text-gray-500 block mb-1">Room</label>
          <select
            className="w-full sm:max-w-xs min-h-[44px] bg-gray-800 border border-blue-500/40 rounded-lg px-3 py-2 text-sm text-gray-100"
            value={activeRoomId || ''}
            onChange={e => {
              const id = e.target.value
              setActiveRoom(id || null)
              setPage(0)
            }}
          >
            <option value="">Select room...</option>
            {rooms.map(r => (
              <option key={r.id} value={r.id}>{r.name || r.id}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="card flex flex-col md:flex-row md:flex-wrap md:items-end gap-4">
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
          className="min-h-[44px] sm:min-h-[40px] px-4 py-2 text-sm text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg hover:border-gray-600 transition-colors tap-highlight-none w-full sm:w-auto"
        >
          Clear
        </button>
        <div className="md:ml-auto w-full md:w-auto">
          <FilterBar active={filter} onChange={f => { setFilter(f); setShowInvalid(false); setPage(0) }} />
        </div>
      </div>

      {!activeRoomId ? (
        <div className="card">
          <p className="text-sm text-gray-500 p-6 text-center">Choose a room to view session history for that room only.</p>
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                <th className="pb-3 pr-4">Start</th>
                <th className="pb-3 pr-4">End</th>
                <th className="pb-3 pr-4">Duration</th>
                <th className="pb-3 pr-4">Delta Temp</th>
                <th className="pb-3 pr-4">Cool Time</th>
                <th className="pb-3 pr-4">kWh</th>
                <th className="pb-3 pr-4">Cost</th>
                <th className="pb-3 pr-4">Stored</th>
                <th className="pb-3 pr-4">Quality</th>
                <th className="pb-3">Reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60">
              {loading ? (
                <tr>
                  <td colSpan={10} className="py-8 text-center text-gray-500">Loading...</td>
                </tr>
              ) : toRender.length === 0 ? (
                <tr>
                  <td colSpan={10} className="py-8 text-center text-gray-500">
                    {filter !== 'all' ? 'No sessions match this filter' : 'No valid sessions recorded yet'}
                  </td>
                </tr>
              ) : groupedRows.map(group => (
                <TableGroup key={group.key} group={group} tariffPerKwh={tariffPerKwh} />
              ))}
            </tbody>
          </table>

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
      )}

      {activeRoomId && totalPages > 1 && (
        <div className="flex items-center justify-center gap-4 pt-2">
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            className="p-1.5 rounded-lg hover:bg-gray-800 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={18} />
          </button>
          <span className="text-sm text-gray-400">Page {page + 1} of {totalPages}</span>
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

function TableGroup({ group, tariffPerKwh }) {
  return (
    <>
      <tr className="bg-gray-900/40">
        <td colSpan={10} className="py-2.5 pr-4 text-xs font-semibold text-blue-300">
          {group.key}
        </td>
      </tr>
      {group.rows.map(s => {
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
            <td className="py-2.5 pr-4 whitespace-nowrap">{formatDateTime(s.start_time)}</td>
            <td className="py-2.5 pr-4 text-gray-400 whitespace-nowrap">{formatDateTime(s.end_time)}</td>
            <td className="py-2.5 pr-4">{formatDuration(s.start_time, s.end_time)}</td>
            <td className="py-2.5 pr-4">
              {deltaTemp != null ? <span className="text-blue-400">-{Number(deltaTemp).toFixed(1)}°C</span> : '--'}
            </td>
            <td className="py-2.5 pr-4">
              {s.time_to_cool_minutes != null ? `${Number(s.time_to_cool_minutes).toFixed(0)} min` : '--'}
            </td>
            <td className="py-2.5 pr-4">
              {s.energy_consumed_kwh != null ? `${Number(s.energy_consumed_kwh).toFixed(3)}` : '--'}
            </td>
            <td className="py-2.5 pr-4 text-yellow-400">{costDisplay(s, tariffPerKwh)}</td>
            <td className="py-2.5 pr-4"><StorageBadge session={s} /></td>
            <td className="py-2.5 pr-4">
              <QualityBadge quality={s._quality} />
              {s._fast && <span className="ml-2 text-xs text-green-400 font-medium">Fast</span>}
            </td>
            <td className="py-2.5"><ReasonBadge reason={s.reason_stopped} /></td>
          </tr>
        )
      })}
    </>
  )
}
