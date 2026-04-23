import { useEffect, useState } from 'react'
import { Wind, Timer, Zap, Thermometer } from 'lucide-react'

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

export default function ACStatusCard({
  acOn,
  sessionStart,
  wattDraw,
  sessionKwh,
  // Live climate entity data (from /api/status ac_* fields)
  acCurrentTemp,
  acTargetTemp,
  acMode,
  acFanMode,
  acSwingMode,
  hasClimateEntity,
}) {
  const [timer, setTimer] = useState(null)

  useEffect(() => {
    if (!acOn || !sessionStart) { setTimer(null); return }
    const id = setInterval(() => setTimer(elapsed(sessionStart)), 1000)
    setTimer(elapsed(sessionStart))
    return () => clearInterval(id)
  }, [acOn, sessionStart])

  return (
    <div className="card flex flex-col gap-3">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wide">AC Status</p>
        <span className={`chip ${acOn ? 'bg-green-900/50 text-green-300' : 'bg-gray-800 text-gray-500'}`}>
          <Wind size={12} />
          {acOn ? 'Running' : 'Off'}
        </span>
      </div>

      {/* Running timer */}
      <div className="flex flex-col gap-2">
        {acOn && timer ? (
          <>
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <Timer size={14} className="text-blue-400" />
              <span>Running for</span>
            </div>
            <span className="text-3xl font-mono font-bold text-blue-400">{timer}</span>
          </>
        ) : (
          <span className="text-gray-600 text-sm">Not running</span>
        )}

        {acOn && sessionKwh > 0 && (
          <div className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Zap size={13} />
            {sessionKwh.toFixed(3)} kWh this session
          </div>
        )}
      </div>

      {/* Live climate entity data — shown when climate entity is configured */}
      {hasClimateEntity && acOn && (
        <div className="border-t border-gray-800 pt-3 grid grid-cols-2 gap-y-1.5 text-xs">
          {/* Current temp */}
          {acCurrentTemp != null && (
            <>
              <span className="text-gray-500 flex items-center gap-1">
                <Thermometer size={11} /> AC reads
              </span>
              <span className="font-semibold text-blue-300">{acCurrentTemp.toFixed(1)}°C</span>
            </>
          )}

          {/* Setpoint */}
          {acTargetTemp != null && (
            <>
              <span className="text-gray-500">Setpoint</span>
              <span className="font-semibold text-gray-200">{acTargetTemp}°C</span>
            </>
          )}

          {/* Mode */}
          {acMode && (
            <>
              <span className="text-gray-500">Mode</span>
              <span className={`font-semibold ${MODE_COLORS[acMode] ?? 'text-gray-300'}`}>
                {MODE_LABELS[acMode] ?? acMode}
              </span>
            </>
          )}

          {/* Fan */}
          {acFanMode && (
            <>
              <span className="text-gray-500 flex items-center gap-1">
                <Wind size={11} /> Fan
              </span>
              <span className="font-semibold text-gray-200">{acFanMode}</span>
            </>
          )}

          {/* Swing */}
          {acSwingMode && (
            <>
              <span className="text-gray-500">Swing</span>
              <span className="font-semibold text-gray-200">{acSwingMode}</span>
            </>
          )}
        </div>
      )}

      {/* Watt draw when running and no climate entity */}
      {acOn && !hasClimateEntity && wattDraw > 0 && (
        <div className="flex items-center gap-1.5 text-xs text-gray-400">
          <Zap size={11} className="text-yellow-400" />
          {wattDraw.toFixed(0)} W
        </div>
      )}
    </div>
  )
}
