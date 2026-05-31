/**
 * ACStatusCard — displays current AC state and session info.
 *
 * State: `acPhase` from backend — off | pending_on | on | pending_off | on_failed (with ac_idle for fan-only).
 *
 * Three possible states:
 *   ON   (green)  — compressor running, watts > 500 W
 *   IDLE (amber)  — fan only, compressor resting, watts 50–500 W
 *   OFF  (gray)   — < 50 W or IR off command sent
 *
 * Climate entity is used ONLY for display (temp, mode, fan, swing).
 */
import { useEffect, useState } from 'react'
import {
  Wind,
  Timer,
  Zap,
  Thermometer,
  Gauge,
  Brain,
  SlidersHorizontal,
  Moon,
  Droplets,
  Flame,
} from 'lucide-react'

function elapsed(startIso) {
  if (!startIso) return null
  const secs = Math.floor((Date.now() - new Date(startIso)) / 1000)
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
}

const MODE_COLORS = {
  cool:     'text-blue-400',
  heat:     'text-orange-400',
  auto:     'text-purple-400',
  dry:      'text-yellow-400',
  fan_only: 'text-teal-400',
  off:      'text-gray-500',
}
const MODE_LABELS = {
  cool: 'Cool', heat: 'Heat', auto: 'Auto',
  dry: 'Dry', fan_only: 'Fan', off: 'Off',
}

// ── Smart mode badge ──────────────────────────────────────────────────────────

const SMART_MODE_CFG = {
  boost:  { label: 'Boost',  bg: 'bg-orange-900/50', text: 'text-orange-300', desc: 'Max airflow' },
  normal: { label: 'Normal', bg: 'bg-blue-900/40',   text: 'text-blue-300',   desc: 'Balanced'   },
  hold:   { label: 'Hold',   bg: 'bg-gray-800',      text: 'text-gray-400',   desc: 'Comfortable'},
}

function SmartModeBadge({ mode, fanMode, delta }) {
  const cfg = SMART_MODE_CFG[mode] || SMART_MODE_CFG.hold
  return (
    <div className={`flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs ${cfg.bg}`}>
      <Gauge size={11} className={cfg.text} />
      <span className={`font-semibold ${cfg.text}`}>{cfg.label}</span>
      {delta != null && (
        <span className="text-gray-500">Δ{delta > 0 ? '+' : ''}{delta.toFixed(1)}°</span>
      )}
      {fanMode && mode !== 'hold' && (
        <span className="text-gray-500">· fan:{fanMode}</span>
      )}
    </div>
  )
}

function formatDelayCountdown(totalSec) {
  if (totalSec == null || !Number.isFinite(Number(totalSec))) return '—'
  const s = Math.max(0, Math.ceil(Number(totalSec)))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${String(r).padStart(2, '0')}`
}

const PRE_COOL_DURATIONS = [10, 15, 20, 25, 30, 45]

function preCoolMessage(result) {
  const key = String(result || '')
  if (key === 'skipped_already_cool') return 'Room already cool'
  if (key === 'expired_no_show') return 'Pre-cool ended - no presence detected'
  if (key === 'blocked_by_manual_override') return 'Manual Override active'
  if (key === 'blocked_room_temp_required') return 'Room temp required'
  if (key === 'blocked_pre_cool_disabled') return 'Pre-cool disabled'
  return null
}

function PreCoolControl({
  enabled,
  active,
  defaultDuration,
  remainingSeconds,
  result,
  target,
  blockedReason,
  onStart,
  onCancel,
}) {
  const [duration, setDuration] = useState(defaultDuration || 25)
  const [busy, setBusy] = useState(false)
  const [localResult, setLocalResult] = useState(null)
  const [countdown, setCountdown] = useState(null)

  useEffect(() => {
    const safeDefault = PRE_COOL_DURATIONS.includes(Number(defaultDuration))
      ? Number(defaultDuration)
      : 25
    setDuration(safeDefault)
  }, [defaultDuration])

  useEffect(() => {
    if (!active || remainingSeconds == null || !Number.isFinite(Number(remainingSeconds))) {
      setCountdown(null)
      return undefined
    }
    setCountdown(Math.max(0, Number(remainingSeconds)))
    const id = window.setInterval(() => {
      setCountdown((value) => (value != null ? Math.max(0, value - 1) : value))
    }, 1000)
    return () => window.clearInterval(id)
  }, [active, remainingSeconds])

  const shownResult = localResult || result
  const message = preCoolMessage(shownResult)
  const shownTarget = Number(target)

  const runStart = async () => {
    if (!onStart) return
    setBusy(true)
    setLocalResult(null)
    try {
      const res = await onStart(duration)
      setLocalResult(res?.pre_cool_result || null)
    } catch (err) {
      setLocalResult('error')
    } finally {
      setBusy(false)
    }
  }

  const runCancel = async () => {
    if (!onCancel) return
    setBusy(true)
    try {
      const res = await onCancel()
      setLocalResult(res?.pre_cool_result || 'cancelled')
    } catch (err) {
      setLocalResult('error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-sky-900/45 bg-sky-950/15 px-2.5 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-sky-500/25 bg-sky-400/10 text-sky-200">
            <Wind size={14} aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="text-xs font-semibold text-gray-100">Pre-cool</p>
            {active ? (
              <p className="text-[11px] font-mono text-sky-200">
                {formatDelayCountdown(countdown ?? remainingSeconds)} remaining
              </p>
            ) : message ? (
              <p className="text-[11px] text-gray-400">{message}</p>
            ) : (
              <p className="text-[11px] text-gray-500">
                {Number.isFinite(shownTarget) ? `Target ${shownTarget.toFixed(1)}Â°C` : 'Arrival cooling'}
              </p>
            )}
          </div>
        </div>
        {active ? (
          <button
            type="button"
            disabled={busy}
            onClick={runCancel}
            className="rounded-md border border-gray-700 bg-gray-900 px-2.5 py-1.5 text-xs font-semibold text-gray-200 transition hover:border-gray-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
        ) : (
          <div className="flex items-center gap-1.5">
            <select
              value={duration}
              disabled={!enabled || busy}
              onChange={e => setDuration(Number(e.target.value))}
              className="h-8 rounded-md border border-gray-700 bg-gray-900 px-2 text-xs font-semibold text-gray-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {PRE_COOL_DURATIONS.map(min => (
                <option key={min} value={min}>{min}m</option>
              ))}
            </select>
            <button
              type="button"
              disabled={!enabled || busy}
              onClick={runStart}
              className="h-8 rounded-md border border-sky-500/45 bg-sky-500/15 px-2.5 text-xs font-semibold text-sky-100 transition hover:border-sky-300/70 disabled:cursor-not-allowed disabled:opacity-45"
            >
              Pre-cool
            </button>
          </div>
        )}
      </div>
      {active && blockedReason === 'pre_cool' && (
        <p className="mt-1.5 text-[11px] text-sky-300/80">Vacancy OFF blocked</p>
      )}
      {shownResult === 'error' && (
        <p className="mt-1.5 text-[11px] text-red-300">Pre-cool command failed</p>
      )}
    </div>
  )
}

function fmtTemp(v, digits = 1) {
  const n = Number(v)
  return Number.isFinite(n) ? `${n.toFixed(digits)}°C` : '—'
}

function fmtOffset(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return null
  if (Math.abs(n) < 0.05) return null
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}°C`
}

function fmtHumidity(v) {
  const n = Number(v)
  return Number.isFinite(n) ? `${n.toFixed(0)}%` : '—'
}

function StateSourceHint({ source, show }) {
  if (!show || !source) return null
  const cfg = {
    power: {
      Icon: Zap,
      label: 'Power confirmed',
      cls: 'text-yellow-400/90',
    },
    power_telemetry: {
      Icon: Zap,
      label: 'Physical remote',
      cls: 'text-yellow-400/90',
    },
    physical_remote: {
      Icon: Zap,
      label: 'Physical remote',
      cls: 'text-yellow-400/90',
    },
    inferred: {
      Icon: Brain,
      label: 'Estimated',
      cls: 'text-purple-400/90',
    },
    system: {
      Icon: SlidersHorizontal,
      label: 'System controlled',
      cls: 'text-gray-500',
    },
  }[source]
  if (!cfg) return null
  const { Icon, label, cls } = cfg
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] uppercase tracking-wide ${cls}`}
      title={label}
    >
      <Icon size={11} aria-hidden />
      {label}
    </span>
  )
}

const COMFORT_LEVEL_STYLE = {
  comfortable: 'text-emerald-300 bg-emerald-950/35 border-emerald-800/55',
  humid: 'text-sky-200 bg-sky-950/35 border-sky-800/50',
  sticky: 'text-amber-200 bg-amber-950/35 border-amber-800/55',
  dry: 'text-orange-200 bg-orange-950/35 border-orange-800/55',
  disabled: 'text-gray-400 bg-gray-900/45 border-gray-800',
  unknown: 'text-gray-400 bg-gray-900/45 border-gray-800',
}

function labelize(raw) {
  const s = String(raw || '').replace(/_/g, ' ').trim()
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Unknown'
}

function ComfortRuntimePanel({
  sleepActive,
  sleepPhase,
  sleepOffset,
  humidityPercent,
  feelsLikeTemp,
  dewPoint,
  comfortLevel,
  humidityBand,
  humidityOffset,
  dryModeRecommended,
  thermalLoadLevel,
  thermalLoadConfidence,
  thermalLoadActive,
  thermalLoadSummary,
  thermalLoadOffset,
  coolingSaturated,
  roomActive = false,
}) {
  const hasSleep = sleepPhase || Math.abs(Number(sleepOffset) || 0) >= 0.05
  const hasHumidity =
    humidityPercent != null ||
    feelsLikeTemp != null ||
    dewPoint != null ||
    comfortLevel ||
    humidityBand ||
    dryModeRecommended
  const shownThermalLevel = roomActive ? thermalLoadLevel : (thermalLoadSummary ? 'standby' : 'idle')
  const shownThermalConfidence = roomActive ? thermalLoadConfidence : 'monitoring'
  const shownThermalSummary = roomActive
    ? (coolingSaturated ? 'Max comfort cooling active' : (thermalLoadSummary || 'Monitoring room load'))
    : (thermalLoadSummary || 'Standby')

  const hasThermalLoad =
    thermalLoadLevel ||
    thermalLoadActive ||
    coolingSaturated ||
    Math.abs(Number(thermalLoadOffset) || 0) >= 0.05

  const sleepOffsetLabel = fmtOffset(sleepOffset)
  const humidityOffsetLabel = fmtOffset(humidityOffset)
  const thermalOffsetLabel = fmtOffset(thermalLoadOffset)
  const explanations = []
  if (sleepOffsetLabel) explanations.push(`${sleepOffsetLabel} sleep optimization`)
  if (humidityOffsetLabel) explanations.push(`${humidityOffsetLabel} humidity adjustment`)
  if (thermalOffsetLabel) explanations.push(`${thermalOffsetLabel} room load compensation`)

  if (!hasSleep && !hasHumidity && !hasThermalLoad && explanations.length === 0) return null

  const comfortKey = String(comfortLevel || 'unknown').toLowerCase()
  const comfortCls = COMFORT_LEVEL_STYLE[comfortKey] || COMFORT_LEVEL_STYLE.unknown
  const loadKey = String(shownThermalLevel || 'low').toLowerCase()
  const loadCls = {
    low: 'border-emerald-800/50 bg-emerald-950/25 text-emerald-200',
    medium: 'border-amber-800/55 bg-amber-950/30 text-amber-200',
    high: 'border-orange-800/55 bg-orange-950/35 text-orange-200',
    standby: 'border-gray-800 bg-gray-900/60 text-gray-400',
    idle: 'border-gray-800 bg-gray-900/60 text-gray-400',
  }[loadKey] || 'border-gray-800 bg-gray-900/60 text-gray-400'

  return (
    <div className="rounded-lg border border-gray-800/80 bg-gray-950/35 px-2.5 py-2 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] uppercase tracking-wide text-gray-500">Comfort intelligence</p>
        {explanations.length > 0 && (
          <span className="text-[10px] text-violet-300 bg-violet-950/35 border border-violet-800/45 rounded px-1.5 py-0.5">
            AI explanation
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
        {hasSleep && (
          <div className="rounded-md border border-indigo-900/45 bg-indigo-950/20 px-2 py-1.5 min-w-0">
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1 text-indigo-200">
                <Moon size={12} aria-hidden />
                Sleep
              </span>
              <span className={sleepActive ? 'text-emerald-300' : 'text-gray-500'}>
                {sleepActive ? 'Active' : 'Idle'}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px]">
              <span className="text-gray-500 truncate">{labelize(sleepPhase || 'inactive')}</span>
              <span className="font-mono text-gray-100">{sleepOffsetLabel || '+0.0°C'}</span>
            </div>
          </div>
        )}

        {hasHumidity && (
          <div className="rounded-md border border-cyan-900/45 bg-cyan-950/15 px-2 py-1.5 min-w-0">
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1 text-cyan-200">
                <Droplets size={12} aria-hidden />
                Humidity
              </span>
              <span className="font-mono text-gray-100">{fmtHumidity(humidityPercent)}</span>
            </div>
            <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px]">
              <span className="text-gray-500">Feels</span>
              <span className="font-mono text-gray-100 text-right">{fmtTemp(feelsLikeTemp)}</span>
              <span className="text-gray-500">Dew point</span>
              <span className="font-mono text-gray-100 text-right">{fmtTemp(dewPoint)}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              <span className={`rounded border px-1.5 py-0.5 text-[10px] ${comfortCls}`}>
                {labelize(comfortLevel)}
              </span>
              {humidityBand && (
                <span className="rounded border border-gray-800 bg-gray-900/60 px-1.5 py-0.5 text-[10px] text-gray-400">
                  {labelize(humidityBand)}
                </span>
              )}
              {dryModeRecommended && (
                <span className="inline-flex items-center gap-1 rounded border border-amber-700/55 bg-amber-950/35 px-1.5 py-0.5 text-[10px] text-amber-200">
                  <Flame size={10} aria-hidden />
                  Dry mode
                </span>
              )}
            </div>
          </div>
        )}

        {hasThermalLoad && (
          <div className="rounded-md border border-emerald-900/35 bg-emerald-950/10 px-2 py-1.5 min-w-0">
            <div className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1 text-emerald-200">
                <Gauge size={12} aria-hidden />
                Room load
              </span>
              <span className={`rounded border px-1.5 py-0.5 text-[10px] ${loadCls}`}>
                {labelize(shownThermalLevel || 'idle')}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px]">
              <span className="truncate text-gray-500">
                {shownThermalSummary}
              </span>
              <span className="shrink-0 text-gray-400">{labelize(shownThermalConfidence || 'monitoring')}</span>
            </div>
          </div>
        )}
      </div>

      {explanations.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {explanations.map((line) => (
            <span
              key={line}
              className="rounded border border-violet-800/45 bg-violet-950/25 px-2 py-0.5 text-[11px] text-violet-100"
            >
              {line}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function StateChip({ acPhase = 'off', acIdle }) {
  if (acPhase === 'pending_on') {
    return (
      <span className="chip bg-amber-900/50 text-amber-200">
        <Timer size={12} /> Waiting to turn ON
      </span>
    )
  }
  if (acPhase === 'on_failed') {
    return (
      <span className="chip bg-red-950/55 text-red-200">
        <Timer size={12} /> Failed to turn ON
      </span>
    )
  }
  const running = acPhase === 'on' || acPhase === 'pending_off'
  if (running && !acIdle) {
    return (
      <span className="chip bg-green-900/50 text-green-300">
        <Wind size={12} /> Running
      </span>
    )
  }
  if (acIdle) {
    return (
      <span className="chip bg-yellow-900/50 text-yellow-300">
        <Wind size={12} /> Idle
      </span>
    )
  }
  return (
    <span className="chip bg-gray-800 text-gray-500">
      <Wind size={12} /> Off
    </span>
  )
}

function formatEpochLine(epochSec, label) {
  if (epochSec == null || !Number.isFinite(Number(epochSec))) return null
  const d = new Date(Number(epochSec) * 1000)
  if (Number.isNaN(d.getTime())) return null
  const clock = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const secAgo = Math.floor((Date.now() - d.getTime()) / 1000)
  const ago =
    secAgo < 90 ? `${secAgo}s ago`
      : secAgo < 3600 ? `${Math.floor(secAgo / 60)} min ago`
        : `${Math.floor(secAgo / 3600)}h ago`
  return `${label}: ${clock} (${ago})`
}

export default function ACStatusCard({
  acPhase = 'off',
  acIdle = false,
  acStateSource,
  sessionStart,
  runtime,
  wattDraw,
  sessionKwh,
  lastAcOnAt,
  lastAcOffAt,
  pendingAction,
  pendingRemainSec,
  preCoolEnabled,
  preCoolActive,
  preCoolDurationMinutes,
  preCoolRemainingSeconds,
  preCoolTarget,
  preCoolResult,
  vacancyOffBlockedReason,
  onPreCoolStart,
  onPreCoolCancel,
  // Smart cooling (read-only display)
  smartCoolingEnabled = false,
  smartMode,
  smartFanMode,
  smartDelta,
  // Passive comfort intelligence (read-only display)
  sleepOptimizationActive,
  sleepPhase,
  sleepOffset,
  humidityPercent,
  feelsLikeTemp,
  dewPoint,
  humidityOffset,
  comfortLevel,
  humidityBand,
  dryModeRecommended,
  thermalLoadLevel,
  thermalLoadConfidence,
  thermalLoadActive,
  thermalLoadSummary,
  thermalLoadOffset,
  coolingSaturated,
  // Climate entity display data (read-only, never used for state)
  acCurrentTemp,
  acTargetTemp,
  acMode,
  acFanMode,
  acSwingMode,
  hasClimateEntity,
}) {
  const [timer, setTimer] = useState(null)
  const runningCompress = acPhase === 'on' || acPhase === 'pending_off'
  const acOnLine = formatEpochLine(lastAcOnAt, runningCompress && !acIdle ? 'Running since' : 'Last ON')
  const acOffLine = formatEpochLine(lastAcOffAt, 'Last OFF')

  // Timer runs while compressor is running (incl. pending OFF) or idle fan
  const sessionActive = runningCompress || acIdle

  useEffect(() => {
    if (!sessionActive || !sessionStart) { setTimer(null); return }
    const id = setInterval(() => setTimer(elapsed(sessionStart)), 1000)
    setTimer(elapsed(sessionStart))
    return () => clearInterval(id)
  }, [sessionActive, sessionStart])

  const [adjPendingRemain, setAdjPendingRemain] = useState(null)
  useEffect(() => {
    if (pendingRemainSec == null || !Number.isFinite(Number(pendingRemainSec))) {
      setAdjPendingRemain(null)
      return
    }
    setAdjPendingRemain(Math.max(0, Number(pendingRemainSec)))
  }, [pendingRemainSec, pendingAction, acPhase])

  useEffect(() => {
    if (adjPendingRemain == null || adjPendingRemain <= 0 || !pendingAction) return undefined
    const id = window.setInterval(() => {
      setAdjPendingRemain((r) => (r != null ? Math.max(0, r - 1) : r))
    }, 1000)
    return () => window.clearInterval(id)
  }, [adjPendingRemain, pendingAction, acPhase])

  const pendingLabel =
    pendingAction === 'on' || acPhase === 'pending_on'
      ? 'Waiting to turn ON'
      : pendingAction === 'off'
        ? 'Waiting to turn OFF'
        : null

  return (
    <div className="card flex flex-col gap-3">
      {/* Header row */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-xs text-gray-500 uppercase tracking-wide">AC Status</p>
        <div className="flex flex-col items-end gap-1">
          <StateChip acPhase={acPhase} acIdle={acIdle} />
          <StateSourceHint source={acStateSource} show={runningCompress && !acIdle} />
        </div>
      </div>

      {pendingLabel && adjPendingRemain != null && (
        <div className="rounded-lg border border-amber-700/45 bg-amber-950/25 px-3 py-2 text-sm">
          <p className="text-amber-200/95 font-medium">{pendingLabel}</p>
          <p className="text-xs text-gray-400 mt-0.5 font-mono">
            <Timer size={12} className="inline mr-1 text-amber-400 align-text-bottom" aria-hidden />
            {formatDelayCountdown(adjPendingRemain)} remaining
          </p>
        </div>
      )}

      {acPhase === 'on_failed' && (
        <div className="rounded-lg border border-red-800/50 bg-red-950/30 px-3 py-2 text-sm">
          <p className="text-red-100/95 font-medium">Failed to turn ON</p>
          <p className="text-xs text-red-200/80 mt-0.5">
            One ON command was sent without compressor/power confirmation. Automation waits for a new
            trigger (temperature / schedule / user) before trying again.
          </p>
        </div>
      )}

      {/* Smart cooling mode badge — shown only when feature is enabled and AC is active */}
      {smartCoolingEnabled && (runningCompress || acIdle) && (
        <SmartModeBadge
          mode={smartMode || 'hold'}
          fanMode={smartFanMode}
          delta={smartDelta}
        />
      )}

      <PreCoolControl
        enabled={preCoolEnabled}
        active={preCoolActive}
        defaultDuration={preCoolDurationMinutes}
        remainingSeconds={preCoolRemainingSeconds}
        result={preCoolResult}
        target={preCoolTarget}
        blockedReason={vacancyOffBlockedReason}
        onStart={onPreCoolStart}
        onCancel={onPreCoolCancel}
      />

      <ComfortRuntimePanel
        sleepActive={sleepOptimizationActive}
        sleepPhase={sleepPhase}
        sleepOffset={sleepOffset}
        humidityPercent={humidityPercent}
        feelsLikeTemp={feelsLikeTemp}
        dewPoint={dewPoint}
        humidityOffset={humidityOffset}
        comfortLevel={comfortLevel}
        humidityBand={humidityBand}
        dryModeRecommended={dryModeRecommended}
        thermalLoadLevel={thermalLoadLevel}
        thermalLoadConfidence={thermalLoadConfidence}
        thermalLoadActive={thermalLoadActive}
        thermalLoadSummary={thermalLoadSummary}
        thermalLoadOffset={thermalLoadOffset}
        coolingSaturated={coolingSaturated}
        roomActive={runningCompress || acIdle || !!sessionStart}
      />

      {(acOnLine || acOffLine) && (
        <div className="rounded-lg border border-gray-800/80 bg-gray-900/40 px-2.5 py-2 space-y-1">
          <p className="text-[10px] uppercase tracking-wide text-gray-500">AC events</p>
          {acOnLine && <p className="text-xs text-gray-300">{acOnLine}</p>}
          {acOffLine && <p className="text-xs text-gray-400">{acOffLine}</p>}
        </div>
      )}

      {/* Timer / idle message / off message */}
      <div className="flex flex-col gap-2">
        {runningCompress && !acIdle && timer ? (
          <>
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <Timer size={14} className="text-blue-400" />
              <span>Running for</span>
            </div>
            <span className="text-3xl font-mono font-bold text-blue-400">{timer}</span>
            {runtime?.active && runtime?.formatted && (
              <span className="text-xs text-gray-500">~{runtime.formatted} session</span>
            )}
          </>
        ) : acIdle && timer ? (
          <>
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <Timer size={14} className="text-yellow-400" />
              <span>Idle for</span>
            </div>
            <span className="text-3xl font-mono font-bold text-yellow-400">{timer}</span>
            {runtime?.active && runtime?.formatted && (
              <span className="text-xs text-gray-500">~{runtime.formatted} session</span>
            )}
            <span className="text-xs text-gray-500">
              Compressor resting · fan running
              {wattDraw > 0 ? ` · ${Number(wattDraw).toFixed(0)} W` : ''}
            </span>
          </>
        ) : (runningCompress || acIdle) && runtime?.active && runtime?.formatted && runtime.formatted !== '—' ? (
          <>
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <Timer size={14} className="text-blue-400" />
              <span>Session</span>
            </div>
            <span className="text-2xl font-mono font-bold text-blue-400">{runtime.formatted}</span>
            <span className="text-xs text-gray-500">Live timer syncs when session start is available</span>
          </>
        ) : (
          <span className="text-gray-600 text-sm">Not running</span>
        )}

        {runningCompress && !acIdle && sessionKwh > 0 && (
          <div className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Zap size={13} />
            {Number(sessionKwh).toFixed(3)} kWh this session
          </div>
        )}

        {/* Live watt reading when compressor is running */}
        {runningCompress && !acIdle && wattDraw > 0 && (
          <div className="flex items-center gap-1.5 text-xs text-gray-400">
            <Zap size={11} className="text-yellow-400" />
            {Number(wattDraw).toFixed(0)} W
          </div>
        )}
      </div>

      {/* Climate entity display data — shown when configured and AC active */}
      {hasClimateEntity && (runningCompress || acIdle) && (
        <div className="border-t border-gray-800 pt-3 grid grid-cols-2 gap-y-1.5 text-xs">
          {acCurrentTemp != null && (
            <>
              <span className="text-gray-500 flex items-center gap-1">
                <Thermometer size={11} /> AC reads
              </span>
              <span className="font-semibold text-blue-300">{Number(acCurrentTemp).toFixed(1)}°C</span>
            </>
          )}
          {acTargetTemp != null && (
            <>
              <span className="text-gray-500">Setpoint</span>
              <span className="font-semibold text-gray-200">{acTargetTemp}°C</span>
            </>
          )}
          {acMode && (
            <>
              <span className="text-gray-500">Mode</span>
              <span className={`font-semibold ${MODE_COLORS[acMode] ?? 'text-gray-300'}`}>
                {MODE_LABELS[acMode] ?? acMode}
              </span>
            </>
          )}
          {acFanMode && (
            <>
              <span className="text-gray-500 flex items-center gap-1">
                <Wind size={11} /> Fan
              </span>
              <span className="font-semibold text-gray-200">{acFanMode}</span>
            </>
          )}
          {acSwingMode && (
            <>
              <span className="text-gray-500">Swing</span>
              <span className="font-semibold text-gray-200">{acSwingMode}</span>
            </>
          )}
        </div>
      )}
    </div>
  )
}
