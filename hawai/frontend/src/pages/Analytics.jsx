import { useEffect, useState } from 'react'
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis,
  Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import { getDailyStats, getSessionStats, downloadExport } from '../api/smartcool.js'
import { Download, TrendingDown, Zap, Clock } from 'lucide-react'

// ── Summary KPI card ──────────────────────────────────────────────────────────
function KpiCard({ icon: Icon, label, value, sub, color = 'text-blue-400' }) {
  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide">
        <Icon size={14} className={color} />
        {label}
      </div>
      <span className={`text-3xl font-bold ${color}`}>{value}</span>
      {sub && <span className="text-xs text-gray-500">{sub}</span>}
    </div>
  )
}

// ── Custom tooltip ────────────────────────────────────────────────────────────
function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-400 mb-1">{label}</p>
      {payload.map(p => (
        <p key={p.dataKey} style={{ color: p.color }}>
          {p.name}: <strong>{p.value}</strong>
        </p>
      ))}
    </div>
  )
}

export default function Analytics() {
  const [daily,    setDaily]    = useState([])
  const [stats,    setStats]    = useState(null)
  const [days,     setDays]     = useState(7)
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    getDailyStats(days).then(setDaily).catch(console.error)
    getSessionStats().then(setStats).catch(console.error)
  }, [days])

  const handleExport = async (fmt) => {
    setExporting(true)
    try { await downloadExport(fmt) }
    catch (e) { console.error(e) }
    finally { setExporting(false) }
  }

  const ml    = stats?.ml    || {}
  const today = stats?.today || {}

  const totalKwh  = daily.reduce((s, d) => s + (d.kwh  || 0), 0)
  const totalCost = daily.reduce((s, d) => s + (d.cost || 0), 0)

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Analytics</h1>

        <div className="flex items-center gap-3">
          {/* Period selector */}
          <div className="flex bg-gray-900 border border-gray-800 rounded-lg overflow-hidden text-sm">
            {[7, 14, 30].map(d => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-3 py-1.5 ${days === d ? 'bg-blue-600 text-white' : 'text-gray-400 hover:bg-gray-800'}`}
              >
                {d}d
              </button>
            ))}
          </div>

          {/* Export buttons */}
          <div className="flex gap-2">
            <button
              onClick={() => handleExport('csv')}
              disabled={exporting}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
            >
              <Download size={14} />
              CSV
            </button>
            <button
              onClick={() => handleExport('json')}
              disabled={exporting}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
            >
              <Download size={14} />
              JSON
            </button>
          </div>
        </div>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-4 gap-4">
        <KpiCard
          icon={Zap}
          label={`${days}d Total Energy`}
          value={`${totalKwh.toFixed(2)} kWh`}
          color="text-yellow-400"
        />
        <KpiCard
          icon={TrendingDown}
          label={`${days}d Total Cost`}
          value={`₹${totalCost.toFixed(2)}`}
          color="text-orange-400"
        />
        <KpiCard
          icon={Clock}
          label="Avg Cool Time"
          value={`${ml.avg_cool_time ?? 0} min`}
          sub="per session"
          color="text-blue-400"
        />
        <KpiCard
          icon={TrendingDown}
          label="ML Data Quality"
          value={`${ml.data_completeness ?? 0}%`}
          sub={`${ml.total_sessions ?? 0} sessions`}
          color="text-green-400"
        />
      </div>

      {/* Daily energy bar chart */}
      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Daily Energy (kWh)</p>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={daily} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 11 }} tickFormatter={d => d?.slice(5)} />
            <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} width={40} />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="kwh" name="kWh" fill="#3b82f6" radius={[4,4,0,0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Daily cost line chart */}
      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Daily Cost (₹)</p>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={daily} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 11 }} tickFormatter={d => d?.slice(5)} />
            <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} width={40} />
            <Tooltip content={<ChartTooltip />} />
            <Line
              type="monotone"
              dataKey="cost"
              name="Cost"
              stroke="#f59e0b"
              strokeWidth={2}
              dot={{ r: 3, fill: '#f59e0b' }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Avg cool time + sessions bar */}
      <div className="grid grid-cols-2 gap-4">
        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Sessions per Day</p>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={daily} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 11 }} tickFormatter={d => d?.slice(5)} />
              <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} width={30} allowDecimals={false} />
              <Tooltip content={<ChartTooltip />} />
              <Bar dataKey="sessions" name="Sessions" fill="#8b5cf6" radius={[4,4,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Avg Cool Time (min)</p>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={daily} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 11 }} tickFormatter={d => d?.slice(5)} />
              <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} width={35} />
              <Tooltip content={<ChartTooltip />} />
              <Line
                type="monotone"
                dataKey="avg_cool_time"
                name="Avg Cool"
                stroke="#34d399"
                strokeWidth={2}
                dot={{ r: 3, fill: '#34d399' }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* ML data export info */}
      <div className="card border-dashed border-green-800">
        <div className="flex items-start gap-4">
          <Download size={20} className="text-green-400 mt-0.5 shrink-0" />
          <div>
            <p className="font-medium text-green-300 mb-1">ML Training Data Export</p>
            <p className="text-sm text-gray-400 mb-3">
              Each row is one cooling session with full environmental context —
              ready to train predictive cooling models.
            </p>
            <p className="text-xs text-gray-500 font-mono">
              session_id · date · indoor_temp_start · indoor_temp_end · outdoor_temp ·
              target_temp · time_to_cool_min · energy_kwh · peak_watts · ac_brand · ac_model …
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
