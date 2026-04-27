/**
 * ACStatusCard — displays current AC state and session info.
 *
 * State source (v1.1.17+):
 *   acOn   → /api/status.ac_on   (watts > 500 W when power sensor available)
 *   acIdle → /api/status.ac_idle (watts 50–500 W: fan running, compressor off)
 *
 * Three possible states:
 *   ON   (green)  — compressor running, watts > 500 W
 *   IDLE (amber)  — fan only, compressor resting, watts 50–500 W
 *   OFF  (gray)   — < 50 W or IR off command sent
 *
 * Climate entity is used ONLY for display (temp, mode, fan, swing).
 */
import { useEffect, useState } from 'react'
import { Wind, Timer, Zap, Thermometer, Gauge } from 'lucide-react'

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

function StateChip({ acOn, acIdle }) {
  if (acOn && !acIdle) {
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

export default function ACStatusCard({
  acOn,
  acIdle = false,
  sessionStart,
  runtime,
  wattDraw,
  sessionKwh,
  // Smart cooling (read-only display)
  smartCoolingEnabled = false,
  smartMode,
  smartFanMode,
  smartDelta,
  // Climate entity display data (read-only, never used for state)
  acCurrentTemp,
  acTargetTemp,
  acMode,
  acFanMode,
  acSwingMode,
  hasClimateEntity,
}) {
  const [timer, setTimer] = useState(null)

  // Timer runs while AC is ON or IDLE (session is active)
  const sessionActive = acOn || acIdle

  useEffect(() => {
    if (!sessionActive || !sessionStart) { setTimer(null); return }
    const id = setInterval(() => setTimer(elapsed(sessionStart)), 1000)
    setTimer(elapsed(sessionStart))
    return () => clearInterval(id)
  }, [sessionActive, sessionStart])

  return (
    <div className="card flex flex-col gap-3">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wide">AC Status</p>
        <StateChip acOn={acOn} acIdle={acIdle} />
      </div>

      {/* Smart cooling mode badge — shown only when feature is enabled and AC is active */}
      {smartCoolingEnabled && (acOn || acIdle) && (
        <SmartModeBadge
          mode={smartMode || 'hold'}
          fanMode={smartFanMode}
          delta={smartDelta}
        />
      )}

      {/* Timer / idle message / off message */}
      <div className="flex flex-col gap-2">
        {acOn && !acIdle && timer ? (
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
        ) : (acOn || acIdle) && runtime?.active && runtime?.formatted && runtime.formatted !== '—' ? (
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

        {acOn && !acIdle && sessionKwh > 0 && (
          <div className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Zap size={13} />
            {Number(sessionKwh).toFixed(3)} kWh this session
          </div>
        )}

        {/* Live watt reading when compressor is running */}
        {acOn && !acIdle && wattDraw > 0 && (
          <div className="flex items-center gap-1.5 text-xs text-gray-400">
            <Zap size={11} className="text-yellow-400" />
            {Number(wattDraw).toFixed(0)} W
          </div>
        )}
      </div>

      {/* Climate entity display data — shown when configured and AC active */}
      {hasClimateEntity && (acOn || acIdle) && (
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
