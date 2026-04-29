/**
 * Live temperature plan — mode, active schedule slot, base vs effective target, AI nudge.
 */
import { CalendarClock, Target, Sparkles } from 'lucide-react'

const MODE_LABEL = {
  manual: 'Manual',
  schedule: 'Schedule',
  schedule_ai: 'Schedule + AI',
}

const SLOT_LABEL = {
  manual: '—',
  morning: 'Morning',
  afternoon: 'Afternoon',
  evening: 'Evening',
  night: 'Night',
}

export default function TemperaturePlanCard({ status }) {
  if (!status) {
    return (
      <div className="card border border-emerald-900/50 bg-emerald-950/15">
        <p className="text-xs text-gray-500 uppercase tracking-wide">Temperature plan</p>
        <p className="text-sm text-gray-600 mt-2">Waiting for status…</p>
      </div>
    )
  }

  const mode = status.temperature_mode || 'manual'
  const slot = status.schedule_slot
  const base = status.schedule_base_temp ?? status.target_temp
  const effWeather = status.effective_after_weather
  const effective = status.effective_target
  const aiOn = !!(status.ai_enabled && mode === 'schedule_ai')
  const aiApplied = !!status.ai_adjust_applied && aiOn

  const slotKey = slot ? (SLOT_LABEL[slot] ?? slot) : '—'

  return (
    <div className="card border border-emerald-900/40 bg-emerald-950/15 flex flex-col gap-3 w-full min-w-0">
      <div className="flex items-center gap-2">
        <CalendarClock size={15} className="text-emerald-400 shrink-0" />
        <p className="text-xs text-gray-500 uppercase tracking-wide">Temperature plan</p>
      </div>

      <div className="flex flex-wrap gap-2 items-center">
        <span className="px-2.5 py-1 rounded-lg text-xs font-semibold bg-gray-800 text-emerald-200 border border-emerald-800/60">
          {MODE_LABEL[mode] ?? mode}
        </span>
        {mode !== 'manual' && (
          <span
            className="px-2.5 py-1 rounded-lg text-xs font-semibold bg-emerald-900/60 text-emerald-100 border border-emerald-700/50 capitalize"
            title="Active band (fixed local clock)"
          >
            {slotKey}
          </span>
        )}
        {aiOn && (
          <span className="px-2 py-0.5 rounded text-[10px] uppercase tracking-wide bg-violet-900/40 text-violet-300 border border-violet-800/50">
            AI layer
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 text-sm min-w-0">
        <div className="min-w-0">
          <span className="text-xs text-gray-500 flex items-center gap-1">
            <Target size={11} /> Base target
          </span>
          <span className="font-mono font-semibold text-gray-100 mt-0.5 block">
            {base != null ? `${Number(base).toFixed(1)}°C` : '—'}
          </span>
          <span className="text-[10px] text-gray-600">Slider or slot</span>
        </div>
        <div className="min-w-0">
          <span className="text-xs text-gray-500">After weather</span>
          <span className="font-mono font-semibold text-emerald-200/95 mt-0.5 block">
            {effWeather != null ? `${Number(effWeather).toFixed(1)}°C` : '—'}
          </span>
          <span className="text-[10px] text-gray-600">Outdoor curve</span>
        </div>
        <div className="min-w-0 sm:col-span-2 lg:col-span-2">
          <span className="text-xs text-gray-500 flex items-center gap-1 flex-wrap">
            <Sparkles size={11} className="text-violet-400 shrink-0" />
            Effective target
          </span>
          <span className="font-mono font-bold text-emerald-300 text-lg mt-0.5 block">
            {effective != null ? `${Number(effective).toFixed(1)}°C` : '—'}
            {aiApplied && (
              <span className="ml-2 text-xs font-semibold px-2 py-0.5 rounded-full bg-violet-900/50 text-violet-200 border border-violet-700/50">
                ±1°C AI
              </span>
            )}
          </span>
          <span className="text-[10px] text-gray-600">
            {aiApplied
              ? 'Schedule + weather + small bounded model nudge (max ±1°C).'
              : aiOn
                ? 'AI enabled — waiting for model / cache, or room at comfort band.'
                : 'What the engine uses for on/off + climate commands.'}
          </span>
        </div>
      </div>
    </div>
  )
}
