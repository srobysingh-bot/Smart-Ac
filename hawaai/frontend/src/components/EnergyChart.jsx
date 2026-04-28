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
          {p.name}: <strong>{typeof p.value === 'number' ? p.value.toFixed(2) : p.value}</strong>
        </p>
      ))}
    </div>
  )
}

/** Indoor / outdoor / climate setpoint vs time (last N snapshots). */
export function TemperatureTimelineChart({ snapshots = [], targetTemp = null }) {
  const data = snapshots.map(s => ({
    time: formatTime(s.timestamp),
    indoor: s.indoor_temp,
    outdoor: s.outdoor_temp,
    setpoint: s.setpoint,
  }))

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-600 text-sm">
        No temperature samples yet
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={data} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="time"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          interval="preserveStartEnd"
        />
        <YAxis
          domain={['auto', 'auto']}
          tick={{ fill: '#6b7280', fontSize: 11 }}
          width={36}
          unit="°"
        />
        <Tooltip content={<CustomTooltip />} />

        {targetTemp != null && (
          <ReferenceLine
            y={targetTemp}
            stroke="#60a5fa"
            strokeDasharray="6 3"
            strokeWidth={1.5}
          >
            <Label
              value={`Config target ${targetTemp}°`}
              position="insideTopRight"
              fill="#60a5fa"
              fontSize={10}
            />
          </ReferenceLine>
        )}

        <Line
          type="monotone"
          dataKey="indoor"
          name="Indoor °C"
          stroke="#f87171"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="outdoor"
          name="Outdoor °C"
          stroke="#7dd3fc"
          strokeWidth={1.5}
          strokeDasharray="4 2"
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="setpoint"
          name="Climate setpoint °C"
          stroke="#a78bfa"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

/** Power draw + cumulative meter kWh vs time. */
export function EnergyTimelineChart({ snapshots = [] }) {
  const data = snapshots.map(s => ({
    time: formatTime(s.timestamp),
    watts: s.watt_draw ?? s.power_watts,
    kwh: s.energy_kwh,
    ac: s.ac_state ? 1 : 0,
  }))

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-600 text-sm">
        No energy samples yet
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={data} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="time"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          interval="preserveStartEnd"
        />
        <YAxis
          yAxisId="watts"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          width={42}
          label={{ value: 'W', angle: -90, position: 'insideLeft', fill: '#6b7280', fontSize: 10 }}
        />
        <YAxis
          yAxisId="kwh"
          orientation="right"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          width={48}
          domain={['auto', 'auto']}
          label={{ value: 'kWh', angle: 90, position: 'insideRight', fill: '#6b7280', fontSize: 10 }}
        />
        <Tooltip content={<CustomTooltip />} />

        <Bar
          yAxisId="watts"
          dataKey="ac"
          name="AC on"
          fill="#3b82f6"
          opacity={0.12}
          barSize={99999}
          isAnimationActive={false}
        />

        <Line
          yAxisId="watts"
          type="monotone"
          dataKey="watts"
          name="Power (W)"
          stroke="#f59e0b"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          yAxisId="kwh"
          type="monotone"
          dataKey="kwh"
          name="Meter kWh"
          stroke="#34d399"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
          connectNulls
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

/** @deprecated Use TemperatureTimelineChart + EnergyTimelineChart */
export default function EnergyChart({ snapshots = [], targetTemp = null }) {
  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs text-gray-500 mb-2">Temperature</p>
        <TemperatureTimelineChart snapshots={snapshots} targetTemp={targetTemp} />
      </div>
      <div>
        <p className="text-xs text-gray-500 mb-2">Energy</p>
        <EnergyTimelineChart snapshots={snapshots} />
      </div>
    </div>
  )
}
