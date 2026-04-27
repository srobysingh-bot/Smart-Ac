import {
  ComposedChart, Line, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Label,
} from 'recharts'

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-400 mb-1">{label}</p>
      {payload.map(p => (
        <p key={p.dataKey} style={{ color: p.color || p.fill }}>
          {p.name}: <strong>{typeof p.value === 'number' ? p.value.toFixed(1) : p.value}</strong>
        </p>
      ))}
    </div>
  )
}

export default function EnergyChart({ snapshots = [], targetTemp = null }) {
  const data = snapshots.map(s => ({
    time:     formatTime(s.timestamp),
    indoor:   s.indoor_temp,
    outdoor:  s.outdoor_temp,
    watts:    s.watt_draw,
    ac:       s.ac_state ? 1 : 0,
  }))

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-44 text-gray-600 text-sm">
        No data yet — start a session to see real-time charts
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <ComposedChart data={data} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="time"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          interval="preserveStartEnd"
        />
        {/* Left axis: temperature */}
        <YAxis
          yAxisId="temp"
          domain={['auto', 'auto']}
          tick={{ fill: '#6b7280', fontSize: 11 }}
          width={32}
          unit="°"
        />
        {/* Right axis: watts */}
        <YAxis
          yAxisId="watts"
          orientation="right"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          width={45}
          unit=" W"
        />
        <Tooltip content={<CustomTooltip />} />

        {/* AC on/off shading bar */}
        <Bar
          yAxisId="watts"
          dataKey="ac"
          name="AC"
          fill="#3b82f6"
          opacity={0.12}
          barSize={99999}
          isAnimationActive={false}
        />

        {/* Watt draw line */}
        <Line
          yAxisId="watts"
          type="monotone"
          dataKey="watts"
          name="Watts"
          stroke="#f59e0b"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />

        {/* Target temp reference line */}
        {targetTemp != null && (
          <ReferenceLine
            yAxisId="temp"
            y={targetTemp}
            stroke="#60a5fa"
            strokeDasharray="6 3"
            strokeWidth={1.5}
          >
            <Label
              value={`Target ${targetTemp}°`}
              position="insideTopRight"
              fill="#60a5fa"
              fontSize={10}
            />
          </ReferenceLine>
        )}

        {/* Indoor temp line */}
        <Line
          yAxisId="temp"
          type="monotone"
          dataKey="indoor"
          name="Indoor °C"
          stroke="#f87171"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />

        {/* Outdoor temp line */}
        <Line
          yAxisId="temp"
          type="monotone"
          dataKey="outdoor"
          name="Outdoor °C"
          stroke="#7dd3fc"
          strokeWidth={1.5}
          strokeDasharray="4 2"
          dot={false}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
