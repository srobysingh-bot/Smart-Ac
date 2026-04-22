import { Thermometer } from 'lucide-react'

function Arc({ pct, color, r = 60, strokeWidth = 10 }) {
  const cx = 70; const cy = 70
  const circumference = Math.PI * r          // half circle
  const dash = Math.min(pct, 1) * circumference
  return (
    <circle
      cx={cx} cy={cy} r={r}
      fill="none"
      stroke={color}
      strokeWidth={strokeWidth}
      strokeDasharray={`${dash} ${circumference}`}
      strokeLinecap="round"
      transform={`rotate(180 ${cx} ${cy})`}
      style={{ transition: 'stroke-dasharray 0.6s ease' }}
    />
  )
}

export default function TempGauge({ indoor, outdoor, target }) {
  const MIN_T = 16; const MAX_T = 40
  const indoorPct  = indoor  != null ? (indoor  - MIN_T) / (MAX_T - MIN_T) : 0
  const outdoorPct = outdoor != null ? (outdoor - MIN_T) / (MAX_T - MIN_T) : 0

  const tempColor = (t) => {
    if (t == null) return '#4b5563'
    if (t <= 22) return '#34d399'
    if (t <= 27) return '#60a5fa'
    if (t <= 32) return '#f59e0b'
    return '#f87171'
  }

  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wide">Temperature</p>
        {target != null && (
          <span className="text-xs text-gray-500">Target: {target}°C</span>
        )}
      </div>

      {/* SVG half-ring gauge */}
      <div className="relative flex justify-center">
        <svg width="140" height="80" viewBox="0 0 140 80">
          {/* Track */}
          <path
            d="M 10 70 A 60 60 0 0 1 130 70"
            fill="none" stroke="#1f2937" strokeWidth="10" strokeLinecap="round"
          />
          {/* Outdoor arc (outer, lighter) */}
          <Arc pct={outdoorPct} color="#7dd3fc" r={60} strokeWidth={6} />
          {/* Indoor arc (inner, bolder) */}
          <Arc pct={indoorPct} color={tempColor(indoor)} r={52} strokeWidth={10} />
        </svg>

        <div className="absolute bottom-0 flex flex-col items-center">
          <span className="text-2xl font-bold" style={{ color: tempColor(indoor) }}>
            {indoor != null ? `${indoor.toFixed(1)}°` : '—'}
          </span>
          <span className="text-xs text-gray-500">indoor</span>
        </div>
      </div>

      {/* Outdoor legend */}
      <div className="flex justify-between text-xs text-gray-500 pt-1">
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full bg-sky-300" />
          Outside {outdoor != null ? `${outdoor.toFixed(1)}°C` : '—'}
        </span>
        <span className="flex items-center gap-1">
          <Thermometer size={12} className="text-gray-500" />
          {indoor != null && target != null
            ? (indoor > target
                ? <span className="text-orange-400">+{(indoor - target).toFixed(1)}°</span>
                : <span className="text-green-400">{(indoor - target).toFixed(1)}°</span>)
            : '—'}
        </span>
      </div>
    </div>
  )
}
