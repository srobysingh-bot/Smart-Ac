/**
 * Smart target adjustment — effective setpoint vs config + reason (read-only).
 */
import { Sparkles } from 'lucide-react'

export default function SmartAdjustmentCard({
  smartAdjustment = false,
  targetTemp,
  effectiveTarget,
  reason,
}) {
  const eff = effectiveTarget != null ? Number(effectiveTarget) : null
  const base = targetTemp != null ? Number(targetTemp) : null
  const delta = eff != null && base != null ? eff - base : null

  return (
    <div className="card flex flex-col gap-3 border-violet-900/40 bg-violet-950/15">
      <div className="flex items-center gap-2">
        <Sparkles size={14} className="text-violet-400" />
        <p className="text-xs text-gray-500 uppercase tracking-wide">Smart target</p>
      </div>
      <div className="grid grid-cols-2 gap-2 text-sm">
        <span className="text-gray-500">Smart mode</span>
        <span className={`font-semibold ${smartAdjustment ? 'text-violet-300' : 'text-gray-500'}`}>
          {smartAdjustment ? 'ON' : 'OFF'}
        </span>
        <span className="text-gray-500">Config target</span>
        <span className="font-mono text-gray-200">{base != null ? `${base}°C` : '—'}</span>
        <span className="text-gray-500">Effective target</span>
        <span className="font-mono font-semibold text-violet-300">
          {eff != null ? `${eff}°C` : '—'}
          {delta != null && Math.abs(delta) >= 0.05 && (
            <span className="text-xs text-gray-500 ml-1">
              ({delta > 0 ? '+' : ''}{delta.toFixed(1)}°)
            </span>
          )}
        </span>
      </div>
      {reason && (
        <p className="text-xs text-gray-400 leading-relaxed border-t border-gray-800 pt-2">
          {reason}
        </p>
      )}
    </div>
  )
}
