import { useCallback, useEffect, useState } from 'react'
import { getConfig, getEntities, getDevices, getDeviceEntities, patchConfig, getStatus } from '../api/smartcool.js'
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

// ── Entity dropdown: search state lives HERE, passed in as props ──────────────
// Each field uses its own search string so they don't interfere.
function EntityDropdown({ label, value, onChange, entities, search, onSearchChange }) {
  const q = search.toLowerCase()
  const filtered = q
    ? entities.filter(
        e =>
          e.entity_id.toLowerCase().includes(q) ||
          (e.friendly_name || '').toLowerCase().includes(q)
      )
    : entities

  return (
    <div>
      <Label>{label}</Label>
      {/* Search input — onChange updates parent state, causing real filtered render */}
      <input
        type="text"
        placeholder="Type to filter…"
        value={search}
        onChange={e => onSearchChange(e.target.value)}
        className="w-full mb-2 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
      />
      <select
        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
        value={value || ''}
        onChange={e => {
          onChange(e.target.value)
          onSearchChange('') // clear search after selection
        }}
      >
        <option value="">— Not configured —</option>
        {filtered.map(e => (
          <option key={e.entity_id} value={e.entity_id}>
            {e.friendly_name} ({e.entity_id})
          </option>
        ))}
      </select>
      {q && filtered.length === 0 && (
        <p className="text-xs text-gray-600 mt-1">No matches — try a different search term</p>
      )}
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
  const [saveStatus, setSaveStatus] = useState(null)
  const [saveMsg,    setSaveMsg]    = useState('')
  const [loading,    setLoading]    = useState(true)
  const [outdoorTemp, setOutdoorTemp] = useState(null)

  // Per-dropdown search state (each search is independent)
  const [presenceSearch,    setPresenceSearch]    = useState('')
  const [tempSearch,        setTempSearch]        = useState('')
  const [climateSearch,     setClimateSearch]     = useState('')
  const [energyPowerSearch, setEnergyPowerSearch] = useState('')
  const [energyKwhSearch,   setEnergyKwhSearch]   = useState('')

  // Energy device registry selector
  const [allDevices,      setAllDevices]      = useState([])
  const [devicesError,    setDevicesError]    = useState(null)   // string | null
  const [deviceSearch,    setDeviceSearch]    = useState('')
  const [selectedDevice,  setSelectedDevice]  = useState(null)   // { device_id, name, ... }
  const [deviceEntities,  setDeviceEntities]  = useState([])     // entities from selected device
  const [entitiesError,   setEntitiesError]   = useState(null)   // string | null
  const [loadingEntities, setLoadingEntities] = useState(false)

  useEffect(() => {
    Promise.all([getConfig(), getEntities()])
      .then(([c, e]) => {
        const cleaned = { ...c }
        if (cleaned.weather_api_key === '***') cleaned.weather_api_key = ''
        setCfg(cleaned)
        setEntities(e)
      })
      .catch(console.error)
      .finally(() => setLoading(false))

    // Load HA device registry for energy device selector
    getDevices()
      .then(setAllDevices)
      .catch(err => setDevicesError(String(err)))

    // Fetch outdoor temp for Smart Adjustment preview
    getStatus()
      .then(s => setOutdoorTemp(s.outdoor_temp ?? null))
      .catch(() => {})
  }, [])

  const patch = useCallback((key, val) => {
    setCfg(prev => ({ ...prev, [key]: val }))
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setSaveStatus(null)
    try {
      const payload = { ...cfg }
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

  // ── Entity filter helpers ─────────────────────────────────────────────────
  const byDomain = domain => entities.filter(e => e.entity_id.startsWith(`${domain}.`))

  const allSensors = entities.filter(e => e.entity_id.startsWith('sensor.'))

  // Live power sensors — watts / current / breaker / circuit
  const powerSensors = allSensors.filter(e => {
    const id   = e.entity_id.toLowerCase()
    const name = (e.friendly_name || '').toLowerCase()
    return (
      id.includes('power')   || id.includes('watt')    ||
      id.includes('current') || id.includes('breaker') ||
      id.includes('circuit') || id.includes('30a')     ||
      name.includes('power') || name.includes('watt')  ||
      name.includes('current')|| name.includes('breaker')||
      name.includes('circuit')|| name.includes('30a')
    )
  })

  // Cumulative kWh sensors — energy / usage / total / consumption
  const kwhSensors = allSensors.filter(e => {
    const id   = e.entity_id.toLowerCase()
    const name = (e.friendly_name || '').toLowerCase()
    return (
      id.includes('kwh')         || id.includes('energy')      ||
      id.includes('usage')       || id.includes('total')       ||
      id.includes('consumption') ||
      name.includes('kwh')       || name.includes('energy')    ||
      name.includes('usage')     || name.includes('total')     ||
      name.includes('consumption')
    )
  })

  const onDeviceSelect = async (device) => {
    setSelectedDevice(device)
    setDeviceEntities([])
    setEntitiesError(null)
    if (!device) return

    setLoadingEntities(true)
    try {
      const devEnts = await getDeviceEntities(device.device_id)
      setDeviceEntities(devEnts)

      // Auto-detect power (watts) entity — match by unit first, then by entity_id pattern
      const powerEnt = devEnts.find(e => {
        const id   = e.entity_id.toLowerCase()
        const unit = (e.unit || '').toLowerCase()
        return unit === 'w' || unit === 'watt' || unit === 'watts' ||
               (id.includes('power') && !id.includes('usage') &&
                !id.includes('total') && !id.includes('kwh'))
      })

      // Auto-detect kWh entity — match by unit first, then by entity_id pattern
      const kwhEnt = devEnts.find(e => {
        const id   = e.entity_id.toLowerCase()
        const unit = (e.unit || '').toLowerCase()
        return unit === 'kwh' || id.includes('kwh') || id.includes('power_usage') ||
               id.includes('energy') ||
               (id.includes('total') && !id.includes('voltage') && !id.includes('current'))
      })

      if (powerEnt) patch('energy_power_entity', powerEnt.entity_id)
      if (kwhEnt)   patch('energy_kwh_entity',   kwhEnt.entity_id)
    } catch (err) {
      setEntitiesError(`Failed to load device entities: ${err.message || err}`)
    }
    setLoadingEntities(false)
  }

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

      {/* AC Control — single pipeline */}
      <div className="card space-y-4">
        <SectionHeader>AC Control</SectionHeader>
        <p className="text-xs text-gray-500 -mt-2">
          HawaAI sends all commands through your climate entity.
          Home Assistant (Aerostate) handles the Broadlink IR transport automatically.
        </p>

        <div className="space-y-2">
          <EntityDropdown
            label="Climate Entity (climate.*)"
            value={cfg.climate_entity}
            onChange={v => patch('climate_entity', v)}
            entities={byDomain('climate')}
            search={climateSearch}
            onSearchChange={setClimateSearch}
          />
          <p className="text-xs text-gray-500">
            Pipeline: <span className="text-blue-400 font-medium">HawaAI</span>
            {' → '}
            <span className="text-blue-400 font-medium">Aerostate</span>
            {' → '}
            <span className="text-gray-400">Broadlink</span>
            {' → '}
            <span className="text-gray-400">AC</span>
          </p>

          {/* Connection status badge */}
          {cfg.climate_entity ? (
            <div className="flex items-center gap-2 px-3 py-2 bg-green-900/20 border border-green-800 rounded-lg">
              <span className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
              <span className="text-xs text-green-300">
                Connected — <code className="font-mono">{cfg.climate_entity}</code>
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 py-2 bg-red-900/20 border border-red-800 rounded-lg">
              <span className="w-2 h-2 rounded-full bg-red-400 shrink-0" />
              <span className="text-xs text-red-300">
                Not configured — AC control is disabled until a climate entity is selected
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Sensors & Devices */}
      <div className="card space-y-4">
        <SectionHeader>Sensors &amp; Devices</SectionHeader>

        {/* Presence sensor */}
        <EntityDropdown
          label="Presence Sensor (binary_sensor.*)"
          value={cfg.presence_entity}
          onChange={v => patch('presence_entity', v)}
          entities={byDomain('binary_sensor')}
          search={presenceSearch}
          onSearchChange={setPresenceSearch}
        />

        {/* Indoor temp */}
        <EntityDropdown
          label="Indoor Temperature Sensor (sensor.*)"
          value={cfg.indoor_temp_entity}
          onChange={v => patch('indoor_temp_entity', v)}
          entities={allSensors}
          search={tempSearch}
          onSearchChange={setTempSearch}
        />

        {/* ── Energy Monitoring ─────────────────────────────────────────────── */}
        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Energy Monitoring</p>

          {/* Step 1 — pick device from registry */}
          <div>
            <Label>Select Energy Device</Label>
            <input
              type="text"
              placeholder="Type to search devices…"
              value={deviceSearch}
              onChange={e => setDeviceSearch(e.target.value)}
              className="w-full mb-2 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
            />
            {devicesError ? (
              <div className="flex items-start gap-2 px-3 py-2 bg-red-900/30 border border-red-700 rounded-lg text-xs text-red-300">
                <AlertCircle size={13} className="shrink-0 mt-0.5" />
                <span>Could not load HA devices: {devicesError}</span>
              </div>
            ) : (
              <select
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
                value={selectedDevice?.device_id || ''}
                onChange={e => {
                  const dev = allDevices.find(d => d.device_id === e.target.value) || null
                  setDeviceSearch('')
                  onDeviceSelect(dev)
                }}
              >
                <option value="">
                  {allDevices.length === 0
                    ? '— Loading devices… —'
                    : '— Select your circuit breaker / smart plug —'}
                </option>
                {allDevices
                  .filter(d => {
                    if (!deviceSearch) return true
                    const q = deviceSearch.toLowerCase()
                    return d.name.toLowerCase().includes(q) ||
                           d.manufacturer.toLowerCase().includes(q) ||
                           d.model.toLowerCase().includes(q)
                  })
                  .map(d => (
                    <option key={d.device_id} value={d.device_id}>
                      {d.name}{d.manufacturer ? ` · ${d.manufacturer}` : ''}{d.model ? ` ${d.model}` : ''}
                    </option>
                  ))
                }
              </select>
            )}
            <p className="text-xs text-gray-500 mt-1">
              Select your energy monitoring device — entities are auto-detected from it.
            </p>
          </div>

          {/* Step 2 — show entities from selected device */}
          {loadingEntities && (
            <p className="text-xs text-gray-400 animate-pulse">Loading entities from device…</p>
          )}

          {entitiesError && (
            <div className="flex items-start gap-2 px-3 py-2 bg-red-900/30 border border-red-700 rounded-lg text-xs text-red-300">
              <AlertCircle size={13} className="shrink-0 mt-0.5" />
              <span>{entitiesError}</span>
            </div>
          )}

          {selectedDevice && !loadingEntities && !entitiesError && deviceEntities.length > 0 && (
            <div className="px-3 py-3 bg-green-900/20 border border-green-800 rounded-lg text-xs space-y-2">
              <div className="text-green-300 font-semibold">
                Found {deviceEntities.length} entities from &quot;{selectedDevice.name}&quot;:
              </div>
              {deviceEntities.map(e => (
                <div key={e.entity_id} className="flex justify-between text-gray-300">
                  <span className="font-mono">{e.entity_id}</span>
                  <span className="text-gray-500">{e.state}{e.unit ? ` ${e.unit}` : ''}</span>
                </div>
              ))}
            </div>
          )}

          {selectedDevice && !loadingEntities && !entitiesError && deviceEntities.length === 0 && (
            <div className="px-3 py-2 bg-orange-900/30 border border-orange-700 rounded-lg text-xs text-orange-300">
              No entities found for this device — select manually below.
            </div>
          )}

          {/* Divider */}
          <p className="text-xs text-gray-600 text-center">— confirm or manually override —</p>

          {/* Live Power (Watts) */}
          <div>
            <EntityDropdown
              label="Live Power Sensor (Watts)"
              value={cfg.energy_power_entity}
              onChange={v => patch('energy_power_entity', v)}
              entities={powerSensors.length > 0 ? powerSensors : allSensors}
              search={energyPowerSearch}
              onSearchChange={setEnergyPowerSearch}
            />
            <p className="text-xs text-gray-500 mt-1">
              Entity showing current watts — e.g. &quot;power&quot; from your breaker
            </p>
          </div>

          {/* Energy Usage (kWh) */}
          <div>
            <EntityDropdown
              label="Energy Usage Sensor (kWh)"
              value={cfg.energy_kwh_entity}
              onChange={v => patch('energy_kwh_entity', v)}
              entities={kwhSensors.length > 0 ? kwhSensors : allSensors}
              search={energyKwhSearch}
              onSearchChange={setEnergyKwhSearch}
            />
            <p className="text-xs text-gray-500 mt-1">
              Entity showing kWh consumed — e.g. &quot;Power Usage&quot; or &quot;Total&quot;
            </p>
          </div>

          {/* Breaker info */}
          <div className="flex items-start gap-2 px-3 py-2 bg-blue-900/20 border border-blue-800 rounded-lg text-xs text-blue-300">
            <span className="shrink-0">ℹ</span>
            <span>
              This is a whole-room breaker — energy figures include all devices (PC, lights, AC).
              For AC-only accuracy, use a dedicated smart plug on the AC unit.
            </span>
          </div>
        </div>

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

        {/* Timer first — easy to find */}
        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Automation timer</p>
          <Slider
            label="Logic Check Interval"
            value={cfg.logic_interval_seconds ?? 60}
            onChange={v => patch('logic_interval_seconds', v)}
            min={30} max={300} step={10} unit=" sec"
          />
          <p className="text-xs text-gray-500 -mt-3">
            How often HawaAI runs the decision loop (temperature, presence, AC control). Lower = more responsive.
          </p>
        </div>

        <Slider
          label="Target Temperature"
          value={cfg.target_temp ?? 24}
          onChange={v => patch('target_temp', v)}
          min={16} max={30} step={1} unit="°C"
        />
        <p className="text-xs text-gray-500 -mt-3">
          AC turns ON above target + hysteresis, OFF below target − hysteresis.
        </p>

        <Slider
          label="Hysteresis Band"
          value={cfg.hysteresis ?? 1.5}
          onChange={v => patch('hysteresis', v)}
          min={0.5} max={3.0} step={0.5} unit="°C"
        />
        <p className="text-xs text-gray-500 -mt-3">
          Larger band = fewer AC cycles. E.g. target 24°C + 1.5° band: ON at 25.5°C, OFF at 22.5°C.
        </p>

        <Slider
          label="Vacancy Timeout"
          value={cfg.vacancy_timeout_minutes ?? 5}
          onChange={v => patch('vacancy_timeout_minutes', v)}
          min={1} max={60} step={1} unit=" min"
        />
        <p className="text-xs text-gray-500 -mt-3">
          Minutes room must be empty before AC turns off automatically.
        </p>

        {/* ── Automation toggles ─────────────────────────────────────────── */}
        <div className="border-t border-gray-800 pt-4 space-y-3">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Automation Toggles</p>

          <Toggle
            label="Use Presence Detection"
            description="Turn AC off when room is vacant for the timeout period"
            checked={cfg.use_presence ?? true}
            onChange={v => patch('use_presence', v)}
          />
          <Toggle
            label="Use Outside Temperature Logic"
            description="Skips cooling when outdoor temperature is already comfortable"
            checked={cfg.use_outdoor_temp ?? true}
            onChange={v => patch('use_outdoor_temp', v)}
          />
          <Toggle
            label="Smart Temperature Adjustment"
            description="Raise / lower effective target based on outdoor conditions to save electricity"
            checked={cfg.smart_temp_adjustment !== false}
            onChange={v => patch('smart_temp_adjustment', v)}
          />
          <Toggle
            label="Smart Cooling (fan optimizer)"
            description="Boost fan when room is far from target; backs off near setpoint (uses climate entity only)"
            checked={cfg.smart_cooling_enabled ?? false}
            onChange={v => patch('smart_cooling_enabled', v)}
          />
          <Toggle
            label="Manual Override"
            description="Disable all automation"
            checked={cfg.manual_override ?? false}
            onChange={v => patch('manual_override', v)}
            danger
          />
        </div>

        {/* ── Smart Adjustment Preview ───────────────────────────────────── */}
        {cfg.smart_temp_adjustment !== false && (() => {
          const t = cfg.target_temp ?? 24
          const outdoor = outdoorTemp
          let adj = 0
          if (outdoor !== null) {
            if (outdoor < 30) adj = +1
            else if (outdoor < 35) adj = +0.5
            else if (outdoor <= 40) adj = 0
            else adj = -1
          }
          const eff = t + adj
          return (
            <div className="rounded-xl border border-blue-800 bg-blue-900/20 p-4 space-y-3">
              <p className="text-xs font-semibold text-blue-300 uppercase tracking-wide">
                Smart Adjustment Preview
              </p>

              {outdoor !== null ? (
                <div className="grid grid-cols-2 gap-y-1 text-xs">
                  <span className="text-gray-400">Current outdoor</span>
                  <span className="font-semibold text-gray-200">{outdoor.toFixed(1)}°C</span>
                  <span className="text-gray-400">Your target</span>
                  <span className="font-semibold text-gray-200">{t}°C</span>
                  <span className="text-gray-400">Adjustment</span>
                  <span className={`font-semibold ${adj > 0 ? 'text-green-400' : adj < 0 ? 'text-orange-400' : 'text-gray-400'}`}>
                    {adj > 0 ? `+${adj}` : adj}°C
                    {adj > 0 ? ' (comfortable outside — saving energy)' : adj < 0 ? ' (very hot — cooling harder)' : ' (no change)'}
                  </span>
                  <span className="text-gray-400">Effective target</span>
                  <span className="font-bold text-blue-400 text-sm">{eff}°C</span>
                </div>
              ) : (
                <p className="text-xs text-gray-500">Configure Weather API to see live outdoor temperature.</p>
              )}

              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="text-gray-500">
                    <th className="text-left py-1 font-normal">Outdoor</th>
                    <th className="text-left py-1 font-normal">Adjustment</th>
                    <th className="text-left py-1 font-normal">Reason</th>
                  </tr>
                </thead>
                <tbody className="text-gray-300 divide-y divide-gray-800">
                  <tr><td className="py-1">{'< 30°C'}</td><td className="text-green-400">+1°C</td><td className="text-gray-500">Comfortable outside — save energy</td></tr>
                  <tr><td className="py-1">30–35°C</td><td className="text-green-400">+0.5°C</td><td className="text-gray-500">Warm — slight relaxation</td></tr>
                  <tr><td className="py-1">35–40°C</td><td className="text-gray-400">0°C</td><td className="text-gray-500">Hot — no change</td></tr>
                  <tr><td className="py-1">{'> 40°C'}</td><td className="text-orange-400">−1°C</td><td className="text-gray-500">Very hot — cool more aggressively</td></tr>
                </tbody>
              </table>
            </div>
          )
        })()}
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

    </div>
  )
}
