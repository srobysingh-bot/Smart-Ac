import { useCallback, useEffect, useState } from 'react'
import { getConfig, getEntities, patchConfig } from '../api/smartcool.js'
import { Save, RefreshCw, AlertCircle, CheckCircle2, Eye, EyeOff } from 'lucide-react'

// ── Reusable field components ─────────────────────────────────────────────────

function Label({ children }) {
  return <label className="text-sm text-gray-400 block mb-1">{children}</label>
}

function SectionHeader({ children }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-widest text-blue-400 border-b border-gray-800 pb-2 mb-4">
      {children}
    </h2>
  )
}

function Input({ label, value, onChange, type = 'text', placeholder, min, max, step }) {
  return (
    <div>
      <Label>{label}</Label>
      <input
        type={type}
        min={min}
        max={max}
        step={step}
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
        value={value ?? ''}
        onChange={e => onChange(type === 'number' ? Number(e.target.value) : e.target.value)}
        placeholder={placeholder}
      />
    </div>
  )
}

function PasswordInput({ label, value, onChange, placeholder }) {
  const [show, setShow] = useState(false)
  return (
    <div>
      <Label>{label}</Label>
      <div className="relative">
        <input
          type={show ? 'text' : 'password'}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 pr-10 text-sm text-gray-100 font-mono focus:outline-none focus:border-blue-500"
          value={value ?? ''}
          onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
        />
        <button
          type="button"
          onClick={() => setShow(s => !s)}
          className="absolute inset-y-0 right-0 flex items-center px-3 text-gray-500 hover:text-gray-300"
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  )
}

function Slider({ label, value, onChange, min, max, step = 0.5, unit = '' }) {
  return (
    <div>
      <div className="flex justify-between mb-1">
        <Label>{label}</Label>
        <span className="text-sm font-semibold text-blue-400">{value}{unit}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value ?? min}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full accent-blue-500"
      />
      <div className="flex justify-between text-xs text-gray-600 mt-0.5">
        <span>{min}{unit}</span>
        <span>{max}{unit}</span>
      </div>
    </div>
  )
}

function Toggle({ label, description, checked, onChange, danger }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className={`text-sm ${danger && checked ? 'text-red-400' : 'text-gray-200'}`}>{label}</p>
        {description && <p className="text-xs text-gray-500 mt-0.5">{description}</p>}
        {danger && checked && (
          <p className="text-xs text-red-400 mt-0.5 font-medium">⚠ All automation is paused</p>
        )}
      </div>
      <button
        onClick={() => onChange(!checked)}
        className={`relative shrink-0 w-11 h-6 rounded-full transition-colors ${
          checked ? (danger ? 'bg-red-600' : 'bg-blue-600') : 'bg-gray-700'
        }`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${
            checked ? 'translate-x-5' : 'translate-x-0'
          }`}
        />
      </button>
    </div>
  )
}

// ── Entity dropdown with search ───────────────────────────────────────────────
function EntitySelect({ label, value, onChange, entities }) {
  const [query, setQuery] = useState('')
  const filtered = query
    ? entities.filter(
        e =>
          e.entity_id.toLowerCase().includes(query.toLowerCase()) ||
          e.friendly_name.toLowerCase().includes(query.toLowerCase())
      )
    : entities

  return (
    <div>
      <Label>{label}</Label>
      <input
        type="search"
        placeholder="Search entities…"
        value={query}
        onChange={e => setQuery(e.target.value)}
        className="w-full mb-2 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
      />
      <select
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
        value={value || ''}
        onChange={e => onChange(e.target.value)}
      >
        <option value="">— Not configured —</option>
        {filtered.map(e => (
          <option key={e.entity_id} value={e.entity_id}>
            {e.friendly_name} ({e.entity_id})
          </option>
        ))}
      </select>
    </div>
  )
}

// ── Hardcoded brand list ──────────────────────────────────────────────────────
const AC_BRANDS = [
  'Daikin', 'LG', 'Samsung', 'Voltas', 'Carrier', 'Hitachi',
  'Mitsubishi Electric', 'Panasonic', 'Haier', 'Blue Star', 'Other',
]

const PROVIDER_OPTIONS = [
  { value: 'openweathermap', label: 'OpenWeatherMap' },
  { value: 'weatherapi',     label: 'WeatherAPI.com' },
  { value: 'tomorrow',       label: 'Tomorrow.io'    },
]

const CURRENCY_OPTIONS = [
  { value: 'INR', label: '₹ Indian Rupee' },
  { value: 'USD', label: '$ US Dollar'    },
  { value: 'EUR', label: '€ Euro'         },
  { value: 'GBP', label: '£ British Pound'},
  { value: 'AED', label: 'AED Dirham'     },
]

// ── Main Settings page ────────────────────────────────────────────────────────
export default function Settings() {
  const [cfg,        setCfg]        = useState({})
  const [entities,   setEntities]   = useState([])
  const [saving,     setSaving]     = useState(false)
  const [saveStatus, setSaveStatus] = useState(null) // 'ok' | 'error' | null
  const [saveMsg,    setSaveMsg]    = useState('')
  const [loading,    setLoading]    = useState(true)

  useEffect(() => {
    Promise.all([getConfig(), getEntities()])
      .then(([c, e]) => {
        // Unmask secrets so the field shows blank (not "***")
        const cleaned = { ...c }
        if (cleaned.weather_api_key === '***') cleaned.weather_api_key = ''
        setCfg(cleaned)
        setEntities(e)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  const patch = useCallback((key, val) => {
    setCfg(prev => ({ ...prev, [key]: val }))
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setSaveStatus(null)
    try {
      const payload = { ...cfg }
      // Remove empty string weather key so we don't overwrite an existing stored key
      if (!payload.weather_api_key) delete payload.weather_api_key
      await patchConfig(payload)
      setSaveStatus('ok')
      setSaveMsg('Settings saved — logic engine updated')
    } catch (err) {
      console.error('Save failed:', err)
      setSaveStatus('error')
      setSaveMsg('Failed to save settings')
    } finally {
      setSaving(false)
      setTimeout(() => setSaveStatus(null), 4000)
    }
  }

  // Domain-filtered entity helpers
  const byDomain = domain => entities.filter(e => e.entity_id.startsWith(`${domain}.`))
  const tempSensors = entities.filter(e => e.entity_id.startsWith('sensor.'))
  const energySensors = entities.filter(
    e => e.entity_id.startsWith('sensor.') &&
      (e.entity_id.includes('power') || e.entity_id.includes('energy') || e.entity_id.includes('watt'))
  )

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-gray-500">
        Loading configuration…
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-8">

      {/* Header + Save button */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Settings</h1>
        <div className="flex items-center gap-2">
          {saveStatus === 'ok' && (
            <span className="flex items-center gap-1 text-green-400 text-sm">
              <CheckCircle2 size={16} /> {saveMsg}
            </span>
          )}
          {saveStatus === 'error' && (
            <span className="flex items-center gap-1 text-red-400 text-sm">
              <AlertCircle size={16} /> {saveMsg}
            </span>
          )}
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
          >
            {saving ? <RefreshCw size={15} className="animate-spin" /> : <Save size={15} />}
            Save
          </button>
        </div>
      </div>

      {/* Sensors & Devices */}
      <div className="card space-y-4">
        <SectionHeader>Sensors &amp; Devices</SectionHeader>
        <EntitySelect
          label="Presence Sensor (binary_sensor.*)"
          value={cfg.presence_entity}
          onChange={v => patch('presence_entity', v)}
          entities={byDomain('binary_sensor')}
        />
        <EntitySelect
          label="Indoor Temperature Sensor (sensor.*)"
          value={cfg.indoor_temp_entity}
          onChange={v => patch('indoor_temp_entity', v)}
          entities={tempSensors}
        />
        <EntitySelect
          label="AC Smart Switch (switch.*)"
          value={cfg.ac_switch_entity}
          onChange={v => patch('ac_switch_entity', v)}
          entities={byDomain('switch')}
        />
        <EntitySelect
          label="Energy / Power Sensor (sensor.*power/energy)"
          value={cfg.energy_sensor_entity}
          onChange={v => patch('energy_sensor_entity', v)}
          entities={energySensors.length ? energySensors : tempSensors}
        />
        <EntitySelect
          label="Broadlink Remote Entity (remote.*)"
          value={cfg.broadlink_entity}
          onChange={v => patch('broadlink_entity', v)}
          entities={byDomain('remote')}
        />
      </div>

      {/* AC Configuration */}
      <div className="card space-y-4">
        <SectionHeader>AC Configuration</SectionHeader>
        <div>
          <Label>AC Brand</Label>
          <select
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
            value={cfg.ac_brand || ''}
            onChange={e => { patch('ac_brand', e.target.value); patch('ac_model', '') }}
          >
            <option value="">— Select brand —</option>
            {AC_BRANDS.map(b => (
              <option key={b} value={b}>{b}</option>
            ))}
          </select>
        </div>
        <Input
          label="AC Model (optional)"
          value={cfg.ac_model}
          onChange={v => patch('ac_model', v)}
          placeholder="e.g. Split 1.5T Inverter"
        />
        <Input
          label="Room Name"
          value={cfg.room_name}
          onChange={v => patch('room_name', v)}
          placeholder="e.g. Living Room"
        />
      </div>

      {/* Logic Settings */}
      <div className="card space-y-5">
        <SectionHeader>Logic Settings</SectionHeader>
        <Slider
          label="Target Temperature"
          value={cfg.target_temp ?? 24}
          onChange={v => patch('target_temp', v)}
          min={16} max={30} step={1} unit="°C"
        />
        <Slider
          label="Hysteresis Band"
          value={cfg.hysteresis ?? 1.5}
          onChange={v => patch('hysteresis', v)}
          min={0.5} max={3.0} step={0.5} unit="°C"
        />
        <Slider
          label="Vacancy Timeout"
          value={cfg.vacancy_timeout_minutes ?? 5}
          onChange={v => patch('vacancy_timeout_minutes', v)}
          min={1} max={60} step={1} unit=" min"
        />
        <div className="space-y-3 pt-2">
          <Toggle
            label="Use Presence Detection"
            description="Turn AC off when room is vacant for the timeout period"
            checked={cfg.use_presence ?? true}
            onChange={v => patch('use_presence', v)}
          />
          <Toggle
            label="Use Outside Temperature Logic"
            description="Skips cooling when outdoor temp is already comfortable"
            checked={cfg.use_outdoor_temp ?? true}
            onChange={v => patch('use_outdoor_temp', v)}
          />
          <Toggle
            label="Manual Override"
            description="Disable all automation"
            checked={cfg.manual_override ?? false}
            onChange={v => patch('manual_override', v)}
            danger
          />
        </div>
      </div>

      {/* Weather API */}
      <div className="card space-y-4">
        <SectionHeader>Outside Temperature API</SectionHeader>
        <div>
          <Label>Provider</Label>
          <select
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
            value={cfg.weather_provider || 'openweathermap'}
            onChange={e => patch('weather_provider', e.target.value)}
          >
            {PROVIDER_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <PasswordInput
          label="API Key"
          value={cfg.weather_api_key}
          onChange={v => patch('weather_api_key', v)}
          placeholder="Paste your weather API key"
        />
        <p className="text-xs text-gray-500 -mt-2">
          Leave blank to keep the existing key.
        </p>
        <Input
          label="City or Lat,Lon"
          value={cfg.weather_city}
          onChange={v => patch('weather_city', v)}
          placeholder="e.g. Chennai  or  13.08,80.27"
        />
      </div>

      {/* Billing */}
      <div className="card space-y-4">
        <SectionHeader>Billing</SectionHeader>
        <Input
          label="Tariff (per kWh)"
          value={cfg.energy_tariff_per_kwh}
          onChange={v => patch('energy_tariff_per_kwh', v)}
          type="number"
          min={0}
          step={0.5}
          placeholder="8.0"
        />
        <div>
          <Label>Currency</Label>
          <select
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
            value={cfg.currency || 'INR'}
            onChange={e => patch('currency', e.target.value)}
          >
            {CURRENCY_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Advanced */}
      <div className="card space-y-4">
        <SectionHeader>Advanced</SectionHeader>
        <Input
          label="Logic Interval (seconds)"
          value={cfg.logic_interval_seconds ?? 60}
          onChange={v => patch('logic_interval_seconds', v)}
          type="number"
          min={10}
          max={300}
          step={10}
          placeholder="60"
        />
        <p className="text-xs text-gray-500">
          How often the decision engine checks sensors. Lower = more responsive, higher = lower CPU.
        </p>
      </div>
    </div>
  )
}
