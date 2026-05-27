import { useEffect, useState } from 'react'
import { getRoomHealth } from '../api/smartcool.js'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Gauge,
  Info,
  Loader,
  ShieldCheck,
  Sparkles,
  Wind,
} from 'lucide-react'

const STATUS_STYLE = {
  learning: {
    Icon: Sparkles,
    badge: 'border-sky-700/60 bg-sky-950/35 text-sky-200',
    accent: 'from-sky-500/18',
  },
  excellent: {
    Icon: CheckCircle2,
    badge: 'border-emerald-700/60 bg-emerald-950/35 text-emerald-200',
    accent: 'from-emerald-500/18',
  },
  watch: {
    Icon: Info,
    badge: 'border-amber-700/60 bg-amber-950/35 text-amber-200',
    accent: 'from-amber-500/18',
  },
  attention: {
    Icon: AlertTriangle,
    badge: 'border-orange-700/60 bg-orange-950/35 text-orange-200',
    accent: 'from-orange-500/18',
  },
  service: {
    Icon: AlertTriangle,
    badge: 'border-red-700/60 bg-red-950/35 text-red-200',
    accent: 'from-red-500/18',
  },
}

function pct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(100, Math.round(n * 100)))
}

function TrendValue({ label, value, suffix = '' }) {
  return (
    <div className="min-w-0 rounded-lg border border-gray-800 bg-gray-950/45 px-3 py-2">
      <p className="truncate text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-500">{label}</p>
      <p className="mt-1 truncate text-sm font-bold text-gray-100">{value ?? '-'}{value != null ? suffix : ''}</p>
    </div>
  )
}

export default function ACHealthCard({ roomId }) {
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!roomId) {
      setHealth(null)
      setLoading(false)
      setError(null)
      return
    }
    let alive = true
    const load = () => {
      getRoomHealth(roomId)
        .then((data) => {
          if (!alive) return
          setHealth(data)
          setError(null)
          setLoading(false)
        })
        .catch((err) => {
          if (!alive) return
          setError(err.message || String(err))
          setLoading(false)
        })
    }
    setLoading(true)
    load()
    const id = window.setInterval(load, 10 * 60 * 1000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [roomId])

  if (!roomId) return null

  if (loading) {
    return (
      <div className="card flex items-center gap-2 text-xs text-gray-500">
        <Loader size={13} className="animate-spin" /> Loading AC health...
      </div>
    )
  }

  if (error) {
    return (
      <div className="card flex items-center gap-2 text-xs text-red-300">
        <AlertTriangle size={13} /> AC health unavailable: {error}
      </div>
    )
  }

  const status = health?.status || 'learning'
  const style = STATUS_STYLE[status] || STATUS_STYLE.learning
  const StatusIcon = style.Icon
  const learning = health?.learning || {}
  const filter = health?.filter || {}
  const telemetry = health?.telemetry_quality || {}
  const metrics = health?.metrics || {}
  const advisories = Array.isArray(health?.advisories) ? health.advisories : []
  const primary = advisories[0]
  const learningProgress = pct(learning.progress)
  const filterProgress = pct(filter.progress)
  const telemetryProgress = pct(telemetry.score)

  return (
    <div className={`card relative overflow-hidden border-gray-800 bg-[radial-gradient(circle_at_15%_0%,rgba(14,165,233,0.12),transparent_32%),linear-gradient(180deg,rgba(17,24,39,0.92),rgba(3,7,18,0.86))]`}>
      <div className={`pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r ${style.accent} via-white/30 to-transparent`} aria-hidden />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-500">AC Health</p>
          <p className="mt-1 truncate text-sm font-semibold text-gray-100">{health?.summary || 'Learning room cooling profile'}</p>
        </div>
        <span className={`inline-flex min-h-[30px] shrink-0 items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${style.badge}`}>
          <StatusIcon size={13} aria-hidden />
          {health?.status_label || 'Learning'}
        </span>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2">
        <TrendValue label="Sessions" value={health?.stable_session_count ?? 0} />
        <TrendValue label="Confidence" value={health?.confidence || 'low'} />
        <TrendValue label="Telemetry" value={telemetry.label || 'Limited'} />
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><ShieldCheck size={11} /> Baseline</span>
            <span>{learningProgress}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-sky-400 transition-all" style={{ width: `${learningProgress}%` }} />
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><Wind size={11} /> Filter</span>
            <span>{Math.round(Number(filter.runtime_hours || 0))}h</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-emerald-400 transition-all" style={{ width: `${filterProgress}%` }} />
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><Activity size={11} /> Quality</span>
            <span>{telemetryProgress}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-violet-400 transition-all" style={{ width: `${telemetryProgress}%` }} />
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-gray-400">
        {metrics.recent_cooling_rate != null && (
          <span className="inline-flex items-center gap-1 rounded-full border border-gray-800 bg-gray-950/45 px-2.5 py-1">
            <Gauge size={11} /> Recent {metrics.recent_cooling_rate} deg/min
          </span>
        )}
        {metrics.similar_sessions != null && (
          <span className="rounded-full border border-gray-800 bg-gray-950/45 px-2.5 py-1">
            Similar sessions {metrics.similar_sessions}
          </span>
        )}
      </div>

      {primary && (
        <div className="mt-4 rounded-lg border border-gray-800 bg-black/20 px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-xs font-semibold text-gray-100">{primary.title}</p>
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-gray-500">{primary.confidence}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-gray-500">{primary.message}</p>
        </div>
      )}
    </div>
  )
}
