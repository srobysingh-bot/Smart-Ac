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
    badge: 'border-cyan-500/35 bg-cyan-400/10 text-cyan-100 shadow-[0_0_22px_rgba(34,211,238,0.10)]',
    accent: 'from-cyan-400/45 via-sky-300/20',
    ring: '#22d3ee',
  },
  excellent: {
    Icon: CheckCircle2,
    badge: 'border-emerald-500/35 bg-emerald-400/10 text-emerald-100 shadow-[0_0_22px_rgba(52,211,153,0.10)]',
    accent: 'from-emerald-400/45 via-teal-300/20',
    ring: '#34d399',
  },
  watch: {
    Icon: Info,
    badge: 'border-amber-500/35 bg-amber-400/10 text-amber-100 shadow-[0_0_22px_rgba(251,191,36,0.10)]',
    accent: 'from-amber-400/45 via-yellow-300/20',
    ring: '#fbbf24',
  },
  attention: {
    Icon: AlertTriangle,
    badge: 'border-orange-500/35 bg-orange-400/10 text-orange-100 shadow-[0_0_22px_rgba(251,146,60,0.12)]',
    accent: 'from-orange-400/45 via-amber-300/20',
    ring: '#fb923c',
  },
  service: {
    Icon: AlertTriangle,
    badge: 'border-red-500/40 bg-red-500/10 text-red-100 shadow-[0_0_22px_rgba(248,113,113,0.12)]',
    accent: 'from-red-400/45 via-rose-300/20',
    ring: '#f87171',
  },
}

const CONFIDENCE_COPY = {
  low: 'Confidence improving',
  medium: 'Profile stabilizing',
  high: 'High confidence',
}

const TELEMETRY_COPY = {
  Limited: 'Collecting telemetry',
  Fair: 'Telemetry improving',
  Good: 'Telemetry steady',
  Strong: 'Telemetry strong',
}

function pct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(100, Math.round(n * 100)))
}

function friendlyConfidence(value) {
  return CONFIDENCE_COPY[String(value || '').toLowerCase()] || 'Confidence improving'
}

function friendlyTelemetry(value) {
  return TELEMETRY_COPY[value] || value || 'Collecting telemetry'
}

function ProgressRing({ value, color, label }) {
  const safe = pct(value)
  const radius = 21
  const circumference = 2 * Math.PI * radius
  const offset = circumference * (1 - safe / 100)
  return (
    <div className="relative grid h-16 w-16 shrink-0 place-items-center">
      <svg className="-rotate-90" width="64" height="64" viewBox="0 0 64 64" aria-hidden>
        <circle cx="32" cy="32" r={radius} stroke="rgba(31,41,55,0.95)" strokeWidth="6" fill="none" />
        <circle
          cx="32"
          cy="32"
          r={radius}
          stroke={color}
          strokeWidth="6"
          fill="none"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          className="transition-all duration-700 ease-out"
          style={{ filter: `drop-shadow(0 0 8px ${color}55)` }}
        />
      </svg>
      <div className="absolute text-center">
        <p className="text-base font-bold leading-none text-white">{safe}%</p>
        <p className="mt-1 text-[9px] font-semibold uppercase tracking-[0.14em] text-gray-500">{label}</p>
      </div>
    </div>
  )
}

function LearningMark() {
  return (
    <div className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.08] shadow-[0_0_24px_rgba(34,211,238,0.10)]">
      <Sparkles size={20} className="text-cyan-200" aria-hidden />
    </div>
  )
}

function MetricPill({ label, value, tone = 'gray' }) {
  const toneClass = {
    cyan: 'border-cyan-400/20 bg-cyan-400/[0.07] text-cyan-100',
    green: 'border-emerald-400/20 bg-emerald-400/[0.07] text-emerald-100',
    amber: 'border-amber-400/20 bg-amber-400/[0.07] text-amber-100',
    gray: 'border-white/10 bg-white/[0.04] text-gray-200',
  }[tone]
  return (
    <div className={`min-w-0 rounded-full border px-3 py-1.5 ${toneClass}`}>
      <span className="mr-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-gray-500">{label}</span>
      <span className="text-xs font-semibold">{value ?? '-'}</span>
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
  const confidenceLabel = friendlyConfidence(health?.confidence)
  const telemetryLabel = friendlyTelemetry(telemetry.label)
  const isLearning = health?.phase === 'learning'
  const headline = health?.phase === 'learning'
    ? 'Building room cooling profile'
    : (health?.status_label || 'Healthy')
  const minimumSessions = Number(learning.minimum_sessions || 0)
  const stableSessions = Number(health?.stable_session_count || 0)
  const sessionsText = minimumSessions > 0
    ? `${stableSessions} / ${minimumSessions} sessions`
    : `${stableSessions} sessions`
  const responseValue = metrics.recent_runtime_per_degree != null
    ? `${metrics.recent_runtime_per_degree} min/deg`
    : 'Monitoring'
  const consistencyValue = health?.confidence === 'high'
    ? 'Stable'
    : health?.confidence === 'medium'
      ? 'Improving'
      : 'Monitoring'

  return (
    <div className="card relative overflow-hidden border-white/10 bg-[radial-gradient(circle_at_12%_0%,rgba(34,211,238,0.13),transparent_30%),radial-gradient(circle_at_92%_12%,rgba(16,185,129,0.09),transparent_28%),linear-gradient(180deg,rgba(15,23,42,0.88),rgba(3,7,18,0.92))] shadow-[0_18px_55px_rgba(0,0,0,0.28)] backdrop-blur">
      <div className={`pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r ${style.accent} via-white/30 to-transparent`} aria-hidden />
      <div className="flex items-start gap-3">
        {isLearning ? (
          <LearningMark />
        ) : (
          <ProgressRing value={telemetry.score} color={style.ring} label="Quality" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-gray-500">AC Health</p>
              <p className="mt-1 text-base font-semibold leading-tight text-gray-50">{headline}</p>
            </div>
            <span className={`inline-flex min-h-[28px] shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${style.badge}`}>
              <StatusIcon size={12} aria-hidden />
              {health?.status_label || 'Learning'}
            </span>
          </div>
          <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-gray-400">
            {health?.summary || 'Building this room-specific cooling profile from stable completed sessions.'}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <MetricPill label="Confidence" value={confidenceLabel} tone={health?.confidence === 'high' ? 'green' : 'cyan'} />
            <MetricPill label={isLearning ? 'Progress' : 'Consistency'} value={isLearning ? sessionsText : consistencyValue} tone="cyan" />
          </div>
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <MetricPill label={isLearning ? 'Telemetry' : 'Response'} value={isLearning ? telemetryLabel : responseValue} tone={telemetryProgress >= 75 ? 'green' : 'amber'} />
        <MetricPill label={isLearning ? 'Signal' : 'Telemetry'} value={isLearning ? 'Collecting data' : telemetryLabel} tone="gray" />
        <MetricPill label="Filter" value={`${Math.round(Number(filter.runtime_hours || 0))}h`} tone={filterProgress >= 90 ? 'amber' : 'gray'} />
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><ShieldCheck size={11} /> Room profile</span>
            <span>{isLearning ? sessionsText : `${learningProgress}%`}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-cyan-300 shadow-[0_0_10px_rgba(103,232,249,0.55)] transition-all" style={{ width: `${learningProgress}%` }} />
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><Wind size={11} /> Filter</span>
            <span>{Math.round(Number(filter.runtime_hours || 0))}h</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.45)] transition-all" style={{ width: `${filterProgress}%` }} />
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-[11px] text-gray-500">
            <span className="inline-flex items-center gap-1"><Activity size={11} /> Quality</span>
            <span>{telemetryProgress}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div className="h-full rounded-full bg-amber-300 shadow-[0_0_10px_rgba(252,211,77,0.45)] transition-all" style={{ width: `${telemetryProgress}%` }} />
          </div>
        </div>
      </div>

      <p className="mt-3 text-[11px] leading-relaxed text-gray-500">
        Health insights become more accurate after stable completed cooling sessions.
      </p>

      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-gray-400">
        {metrics.recent_cooling_rate != null && (
          <span className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1">
            <Gauge size={11} /> Recent {metrics.recent_cooling_rate} deg/min
          </span>
        )}
        {metrics.similar_sessions != null && (
          <span className="rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1">
            Similar sessions {metrics.similar_sessions}
          </span>
        )}
      </div>

      {primary && (
        <div className="mt-3 rounded-lg border border-white/10 bg-black/25 px-3 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-xs font-semibold text-gray-100">{primary.title}</p>
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-gray-500">{friendlyConfidence(primary.confidence)}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-gray-400">{primary.message}</p>
        </div>
      )}
    </div>
  )
}
