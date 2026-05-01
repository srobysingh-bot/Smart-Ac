import { useEffect, useState } from 'react'
import { Brain, Loader } from 'lucide-react'
import { getAiDecisions } from '../api/smartcool.js'

function formatTs(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

export default function AiDecisionsCard({ roomId }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    if (!roomId) {
      setRows([])
      return
    }
    let cancel = false
    setLoading(true)
    setErr(null)
    getAiDecisions(roomId, 40)
      .then(r => {
        if (!cancel) setRows(r.decisions || [])
      })
      .catch(e => {
        if (!cancel) setErr(e.message || String(e))
      })
      .finally(() => {
        if (!cancel) setLoading(false)
      })
    return () => { cancel = true }
  }, [roomId])

  if (!roomId) return null

  return (
    <div className="card">
      <div className="flex items-center gap-2 mb-4">
        <Brain size={18} className="text-violet-400" />
        <p className="text-xs text-gray-500 uppercase tracking-wide">AI decision history</p>
        {loading && <Loader size={14} className="animate-spin text-gray-500" />}
      </div>

      {err && (
        <p className="text-sm text-amber-500 mb-2">{err}</p>
      )}

      {!loading && rows.length === 0 && !err && (
        <p className="text-sm text-gray-600 py-6 text-center">
          No stored AI outputs yet. Enable AI and wait for a successful inference.
        </p>
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto text-sm">
          <table className="w-full text-left border-collapse">
            <thead className="text-xs text-gray-500 bg-gray-900/95">
              <tr>
                <th className="py-2 pr-3 font-medium">Time</th>
                <th className="py-2 pr-3 font-medium">Target</th>
                <th className="py-2 pr-3 font-medium">Fan</th>
                <th className="py-2 pr-3 font-medium">Conf.</th>
                <th className="py-2 pr-3 font-medium">Action</th>
                <th className="py-2 pr-3 font-medium">Snap</th>
                <th className="py-2 pr-3 font-medium">User adj.</th>
                <th className="py-2 pr-3 font-medium">User °C</th>
                <th className="py-2 pr-3 font-medium">Δs</th>
                <th className="py-2 pr-3 font-medium">Provider</th>
              </tr>
            </thead>
            <tbody className="text-gray-300">
              {rows.map(d => (
                <tr key={d.id} className="border-t border-gray-800/80">
                  <td className="py-2 pr-3 text-gray-400 whitespace-nowrap">{formatTs(d.ts)}</td>
                  <td className="py-2 pr-3">{d.target_temp != null ? `${Number(d.target_temp).toFixed(1)}°` : '—'}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{d.fan_mode || '—'}</td>
                  <td className="py-2 pr-3">{d.confidence != null ? Number(d.confidence).toFixed(2) : '—'}</td>
                  <td className="py-2 pr-3">{d.action || '—'}</td>
                  <td className="py-2 pr-3 text-xs text-gray-500 font-mono" title={`snapshot ${d.snapshot_id ?? ''}`}>
                    {d.snapshot_id ?? '—'}
                  </td>
                  <td className="py-2 pr-3">{d.user_adjusted ? 'Y' : '—'}</td>
                  <td className="py-2 pr-3">
                    {d.user_target_temp != null ? `${Number(d.user_target_temp).toFixed(1)}°` : '—'}
                  </td>
                  <td className="py-2 pr-3 text-xs">
                    {d.adjustment_delay_seconds != null
                      ? `${Math.round(Number(d.adjustment_delay_seconds))}s`
                      : '—'}
                  </td>
                  <td className="py-2 pr-3 text-xs text-gray-500">
                    {d.provider}{d.model ? ` / ${d.model}` : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
