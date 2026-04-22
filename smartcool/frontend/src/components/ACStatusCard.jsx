import { useEffect, useState } from 'react'
import { Wind, Timer, Zap } from 'lucide-react'

function elapsed(startIso) {
  if (!startIso) return null
  const secs = Math.floor((Date.now() - new Date(startIso)) / 1000)
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
}

export default function ACStatusCard({ acOn, sessionStart, wattDraw, sessionKwh }) {
  const [timer, setTimer] = useState(null)

  useEffect(() => {
    if (!acOn || !sessionStart) { setTimer(null); return }
    const id = setInterval(() => setTimer(elapsed(sessionStart)), 1000)
    setTimer(elapsed(sessionStart))
    return () => clearInterval(id)
  }, [acOn, sessionStart])

  return (
    <div className="card flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wide">AC Status</p>
        <span className={`chip ${acOn ? 'bg-green-900/50 text-green-300' : 'bg-gray-800 text-gray-500'}`}>
          <Wind size={12} />
          {acOn ? 'Running' : 'Off'}
        </span>
      </div>

      <div className="flex-1 flex flex-col justify-center gap-2">
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
          <div className="flex items-center gap-1.5 text-sm text-yellow-400 mt-1">
            <Zap size={13} />
            {sessionKwh.toFixed(3)} kWh this session
          </div>
        )}
      </div>
    </div>
  )
}
