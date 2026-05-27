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

function finalizedDurationMinutes(s) {
  if (!s.start_time || !s.end_time) return null
  const mins = (new Date(s.end_time) - new Date(s.start_time)) / 60000
  return Number.isFinite(mins) && mins >= 0 ? mins : null
}

const REASON_COLORS = {
  cooled: 'border-emerald-400/20 bg-emerald-400/[0.07] text-emerald-200',
  thermostat_reached: 'border-emerald-400/20 bg-emerald-400/[0.07] text-emerald-200',
  vacant: 'border-amber-400/20 bg-amber-400/[0.07] text-amber-200',
  manual: 'border-gray-500/20 bg-white/[0.04] text-gray-300',
  manual_off: 'border-gray-500/20 bg-white/[0.04] text-gray-300',
  power_off: 'border-gray-500/20 bg-white/[0.04] text-gray-300',
  schedule: 'border-sky-400/20 bg-sky-400/[0.07] text-sky-200',
}

function reasonLabel(reason) {
  if (!reason) return '--'
  return String(reason)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
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

const QUALITY_BADGE = {
  good: {
    label: 'Good',
    dot: 'bg-emerald-300 shadow-[0_0_8px_rgba(110,231,183,0.55)]',
    text: 'text-emerald-200',
    bg: 'border-emerald-400/20 bg-emerald-400/[0.07]',
  },
  weak: {
    label: 'Review',
    dot: 'bg-amber-300 shadow-[0_0_8px_rgba(252,211,77,0.55)]',
    text: 'text-amber-200',
    bg: 'border-amber-400/20 bg-amber-400/[0.07]',
  },
  invalid: {
    label: 'Low',
    dot: 'bg-red-400 shadow-[0_0_8px_rgba(248,113,113,0.55)]',
    text: 'text-red-200',
    bg: 'border-red-400/20 bg-red-400/[0.07]',
  },
}

function QualityBadge({ quality }) {
  const q = QUALITY_BADGE[quality] || QUALITY_BADGE.invalid
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 ${q.bg}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${q.dot}`} />
      <span className={`text-[11px] font-semibold ${q.text}`}>{q.label}</span>
    </span>
  )
}

function StorageBadge({ session }) {
  const stored = session.is_record_valid !== 0 && session.provisional !== 1
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
      stored ? 'border-emerald-400/20 bg-emerald-400/[0.07] text-emerald-200' : 'border-red-400/20 bg-red-400/[0.07] text-red-200'
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

export default function SessionTable({ limit = 10, roomId }) {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [showInvalid, setShowInvalid] = useState(false)
  const [tariffPerKwh, setTariffPerKwh] = useState(8.0)

  useEffect(() => {
    if (!roomId) {
      setSessions([])
      setTariffPerKwh(8.0)
      setLoading(false)
      return
    }
    setLoading(true)
    getSessions({ limit, room_id: roomId })
      .then(r => {
        setSessions(r.sessions || [])
        setTariffPerKwh(Number(r.tariff_per_kwh ?? 8.0))
      })
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
    return <p className="py-4 text-center text-sm text-gray-500">Select a room to list sessions.</p>
  }

  if (loading) {
    return <p className="py-4 text-center text-sm text-gray-500">Loading...</p>
  }
  if (!sessions.length) {
    return <p className="py-4 text-center text-sm text-gray-600">No valid sessions recorded yet</p>
  }

  return (
    <div className="relative">
      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 h-6 rounded-t-xl bg-gradient-to-b from-gray-950/90 to-transparent" aria-hidden />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 h-8 rounded-b-xl bg-gradient-to-t from-gray-950/90 to-transparent" aria-hidden />
      <div className="max-h-[360px] overflow-auto scroll-smooth rounded-xl border border-white/10 bg-black/10 [scrollbar-color:rgba(148,163,184,0.35)_transparent] [scrollbar-width:thin] sm:max-h-[420px]">
        {toRender.length === 0 ? (
          <p className="py-4 text-center text-sm text-gray-600">No valid sessions recorded yet</p>
        ) : (
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="sticky top-0 z-10 border-b border-white/10 bg-gray-950/95 text-left text-[10px] uppercase tracking-[0.16em] text-gray-500 backdrop-blur">
                <th className="px-3 py-2 font-semibold">Start</th>
                <th className="px-3 py-2 font-semibold">End</th>
                <th className="px-3 py-2 font-semibold">Duration</th>
                <th className="px-3 py-2 font-semibold">Delta</th>
                <th className="px-3 py-2 font-semibold">kWh</th>
                <th className="px-3 py-2 font-semibold">Cost</th>
                <th className="px-3 py-2 font-semibold">Stored</th>
                <th className="px-3 py-2 font-semibold">Reason</th>
                <th className="px-3 py-2 font-semibold">Quality</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.06]">
              {grouped.map(group => (
                <FragmentRows key={group.key} group={group} tariffPerKwh={tariffPerKwh} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {invalidRows.length > 0 && (
        <button
          onClick={() => setShowInvalid(v => !v)}
          className="mt-3 flex items-center gap-1.5 text-xs text-gray-500 transition-colors hover:text-gray-300"
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

function FragmentRows({ group, tariffPerKwh }) {
  return (
    <>
      <tr className="sticky top-[33px] z-[9] bg-gradient-to-r from-sky-950/95 via-gray-950/95 to-gray-950/90 backdrop-blur">
        <td colSpan={9} className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-sky-200/90">
          <span className="inline-flex items-center rounded-full border border-sky-400/15 bg-sky-400/[0.06] px-2 py-0.5">
            {group.key}
          </span>
        </td>
      </tr>
      {group.rows.map(s => {
        const delta = s.delta_temp ??
          (s.indoor_temp_start != null && s.indoor_temp_end != null
            ? s.indoor_temp_start - s.indoor_temp_end
            : null)
        const isInvalid = s._quality === 'invalid'
        const duration = formatDuration(s.start_time, s.end_time)
        return (
          <tr
            key={s.session_id}
            className={`group transition-colors hover:bg-white/[0.035] ${isInvalid ? 'opacity-45' : ''}`}
          >
            <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-gray-500">{formatDateTime(s.start_time)}</td>
            <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-gray-500">{formatDateTime(s.end_time)}</td>
            <td className="px-3 py-1.5 text-sm font-semibold tabular-nums text-gray-100">{duration ?? '--'}</td>
            <td className="px-3 py-1.5">
              {delta != null ? (
                <span className="text-sm font-semibold tabular-nums text-cyan-200">{`-${Number(delta).toFixed(1)}\u00b0C`}</span>
              ) : (
                <span className="text-gray-600">--</span>
              )}
            </td>
            <td className="px-3 py-1.5 text-xs tabular-nums text-gray-300">{fmt(s.energy_consumed_kwh, 3)}</td>
            <td className="px-3 py-1.5 text-xs font-semibold tabular-nums text-amber-200">{costDisplay(s, tariffPerKwh)}</td>
            <td className="px-3 py-1.5"><StorageBadge session={s} /></td>
            <td className="px-3 py-1.5">
              <span className={`inline-flex max-w-[150px] items-center truncate rounded-full border px-2 py-0.5 text-[11px] font-semibold ${REASON_COLORS[s.reason_stopped] || 'border-white/10 bg-white/[0.04] text-gray-400'}`}>
                {reasonLabel(s.reason_stopped)}
              </span>
            </td>
            <td className="px-3 py-1.5"><QualityBadge quality={s._quality} /></td>
          </tr>
        )
      })}
    </>
  )
}
