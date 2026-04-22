import { useCallback, useEffect, useState } from 'react'
import { getBrands, getConfig, getEntities, patchConfig } from '../api/smartcool.js'
import ACSelector from '../components/ACSelector.jsx'
import { Save, RefreshCw, AlertCircle, CheckCircle2 } from 'lucide-react'

// ── Reusable field components ─────────────────────────────────────────────────

function Label({ children }) {
  return <label className="text-sm text-gray-400 block mb-1">{children}</label>
}

function Select({ label, value, onChange, options, placeholder = 'Select…' }) {
  return (
    <div>
      <Label>{label}</Label>
      <select
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
        value={value || ''}
        onChange={e => onChange(e.target.value)}
      >
        <option value="">{placeholder}</option>
        {options.map(o => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  )
}

function Input({ label, value, onChange, type = 'text', placeholder }) {
  return (
    <div>
      <Label>{label}</Label>
      <input
        type={type}
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
        value={value ?? ''}
        onChange={e => onChange(type === 'number' ? Number(e.target.value) : e.target.value)}
        placeholder={placeholder}
      />
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
        min={min} max={max} step={step}
        value={value ?? min}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full accent-blue-500"
      />
      <div className="flex justify-between text-xs text-gray-600 mt-0.5">
        <span>{min}{unit}</span><span>{max}{unit}</span>
      </div>
    </div>
  )
}

function Toggle({ label, description, checked, onChange }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm text-gray-200">{label}</p>
        {description && <p className="text-xs text-gray-500 mt-0.5">{description}</p>}
      </div>
      <button
        onClick={() => onChange(!checked)}
        className={`relative shrink-0 w-11 h-6 rounded-full transition-colors ${checked ? 'bg-blue-600' : 'bg-gray-700'}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${checked ? 'translate-x-5' : 'translate-x-0'}`} />
      </button>
    </div>
  )
}

function SectionHeader({ children }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-widest text-blue-400 border-b border-gray-800 pb-2 mb-4">
      {children}
    </h2>
  )
}

// ── Entity dropdown helper ────────────────────────────────────────────────────
function EntitySelect({ label, value, onChange, entityList }) {
  const options = entityList.map(e => ({
    value: e.entity_id,
    label: `${e.friendly_name} (${e.entity_id})`,
  }))
  return <Select label={label} value={value} onChange={onChange} options={options} />
}

// ── Main Settings page ────────────────────────────────────────────────────────
export default function Settings() {
  const [cfg,        setCfg]        = useState({})
  const [brands,     setBrands]     = useState([])
  const [entities,   setEntities]   = useState([])
  const [saving,     setSaving]     = useState(false)
  const [saveStatus, setSaveStatus] = useState(null) // 'ok' | 'error'
  const [loading,    setLoading]    = useState(true)

  useEffect(() => {
    Promise.all([getConfig(), getBrands(), getEntities()])
      .then(([c, b, e]) => {
        setCfg(c)
        setBrands(b)
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
      await patchConfig(cfg)
      setSaveStatus('ok')
    } catch {
      setSaveStatus('error')
    } finally {
      setSaving(false)
      setTimeout(() => setSaveStatus(null), 3000)
    }
  }

  const filterEntities = (domain) =>
    entities.filter(e => e.entity_id.startsWith(`${domain}.`))

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-gray-500">
        Loading configuration…
      </div>
    )
  }

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

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Settings</h1>
        <div className="flex items-center gap-2">
          {saveStatus === 'ok' && (
            <span className="flex items-center gap-1 text-green-400 text-sm">
              <CheckCircle2 size={16} /> Saved
            </span>
          )}
          {saveStatus === 'error' && (
            <span className="flex items-center gap-1 text-red-400 text-sm">
              <AlertCircle size={16} /> Failed
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
          label="Presence Sensor Entity"
          value={cfg.presence_entity}
          onChange={v => patch('presence_entity', v)}
          entityList={filterEntities('binary_sensor')}
        />
        <EntitySelect
          label="Indoor Temperature Sensor"
          value={cfg.indoor_temp_entity}
          onChange={v => patch('indoor_temp_entity', v)}
          entityList={filterEntities('sensor')}
        />
        <EntitySelect
          label="AC Smart Switch"
          value={cfg.ac_switch_entity}
          onChange={v => patch('ac_switch_entity', v)}
          entityList={filterEntities('switch')}
        />
        <EntitySelect
          label="Energy / Power Sensor"
          value={cfg.energy_sensor_entity}
          onChange={v => patch('energy_sensor_entity', v)}
          entityList={filterEntities('sensor')}
        />
        <EntitySelect
          label="Broadlink Remote Entity"
          value={cfg.broadlink_entity}
          onChange={v => patch('broadlink_entity', v)}
          entityList={filterEntities('remote')}
        />
      </div>

      {/* AC Configuration */}
      <div className="card space-y-4">
        <SectionHeader>AC Configuration</SectionHeader>
        <ACSelector
          brands={brands}
          selectedBrand={cfg.ac_brand}
          selectedModel={cfg.ac_model}
          onBrandChange={v => { patch('ac_brand', v); patch('ac_model', '') }}
          onModelChange={v => patch('ac_model', v)}
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
          min={1} max={30} step={1} unit=" min"
        />
        <div className="space-y-3 pt-2">
          <Toggle
            label="Use Presence Detection"
            description="Turn AC off when room is vacant"
            checked={cfg.use_presence ?? true}
            onChange={v => patch('use_presence', v)}
          />
          <Toggle
            label="Use Outside Temperature Logic"
            description="Skip cooling when outdoor temp is comfortable"
            checked={cfg.use_outdoor_temp ?? true}
            onChange={v => patch('use_outdoor_temp', v)}
          />
          <Toggle
            label="Manual Override"
            description="Disable all automation — SmartCool pauses"
            checked={cfg.manual_override ?? false}
            onChange={v => patch('manual_override', v)}
          />
        </div>
      </div>

      {/* Weather API */}
      <div className="card space-y-4">
        <SectionHeader>Outside Temperature API</SectionHeader>
        <Select
          label="Provider"
          value={cfg.weather_provider}
          onChange={v => patch('weather_provider', v)}
          options={PROVIDER_OPTIONS}
        />
        <Input
          label="API Key"
          value={cfg.weather_api_key === '***' ? '' : cfg.weather_api_key}
          onChange={v => patch('weather_api_key', v)}
          placeholder="Enter API key"
          type="password"
        />
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
          placeholder="8.0"
        />
        <Select
          label="Currency"
          value={cfg.currency}
          onChange={v => patch('currency', v)}
          options={CURRENCY_OPTIONS}
        />
      </div>

      {/* HA Token */}
      <div className="card space-y-4">
        <SectionHeader>Home Assistant</SectionHeader>
        <Input
          label="Long-Lived Access Token"
          value={cfg.ha_token === '***' ? '' : cfg.ha_token}
          onChange={v => patch('ha_token', v)}
          placeholder="Paste your HA token (only needed outside the add-on)"
          type="password"
        />
        <p className="text-xs text-gray-500">
          When running as an HA add-on the supervisor token is used automatically.
          Only provide a manual token for local development.
        </p>
      </div>
    </div>
  )
}
