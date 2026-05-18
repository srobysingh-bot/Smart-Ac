import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getRoom,
  getEntities,
  getDevices,
  getDeviceEntities,
  getWeather,
  getStatus,
  updateRoom,
  disableRoom,
  enableRoom,
  deleteRoom,
} from '../api/smartcool.js'
import { useRoom } from '../context/RoomContext.jsx'
import { useRoomData } from '../context/RoomDataContext.jsx'
import { Save, RefreshCw, AlertCircle, CheckCircle2, Eye, EyeOff, Trash2 } from 'lucide-react'

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

/** Persisted under each room's `settings` in config (non-entity fields). */
const ROOM_SETTINGS_KEYS = [
  'control_mode', 'ir_backend', 'presence_only_on_dwell_seconds', 'presence_only_max_runtime_minutes',
  'target_temp', 'hysteresis', 'vacancy_timeout_minutes', 'logic_interval_seconds',
  'on_delay_seconds', 'off_delay_seconds',
  'energy_tariff_per_kwh', 'currency', 'use_presence', 'use_outdoor_temp',
  'smart_temp_adjustment', 'smart_cooling_enabled', 'manual_override',
  'ac_brand', 'ac_model', 'weather_provider', 'weather_api_key', 'weather_city',
  'temperature_mode', 'timezone', 'schedule',
  'effective_mode', 'manual_effective_temp', 'effective_max_delta_deg',
  'zone_entity_id', 'zone_required_for_on', 'zone_dwell_seconds',
]

const SCHEDULE_SLOT_ROWS = [
  { key: 'morning_temp', label: 'Morning', hours: '06:00–12:00' },
  { key: 'afternoon_temp', label: 'Afternoon', hours: '12:00–17:00' },
  { key: 'evening_temp', label: 'Evening', hours: '17:00–22:00' },
  { key: 'night_temp', label: 'Night', hours: '22:00–06:00' },
]

function slotKeyForHour24(hour) {
  if (hour >= 22 || hour < 6) return 'night_temp'
  if (hour < 12) return 'morning_temp'
  if (hour < 17) return 'afternoon_temp'
  return 'evening_temp'
}

/** Rough preview of which schedule row is active — uses timezone field or browser zone. */
function previewScheduleSlotKey(cfg) {
  const tz = String(cfg.timezone || '').trim() || Intl.DateTimeFormat().resolvedOptions().timeZone
  let hour = new Date().getHours()
  try {
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: 'numeric', hourCycle: 'h23' }).formatToParts(new Date())
    const hp = parts.find((p) => p.type === 'hour')
    if (hp != null && hp.value !== undefined) hour = Number(hp.value)
  } catch {
    /* keep local hour */
  }
  return { hour, key: slotKeyForHour24(hour) }
}

function zonePhaseDisplay(phase) {
  switch (phase) {
    case 'present':
      return { emoji: '🟢', label: 'Present' }
    case 'waiting':
      return { emoji: '🟡', label: 'Waiting (dwell)' }
    case 'absent':
      return { emoji: '🔴', label: 'Not present' }
    case 'unusable':
      return { emoji: '🟡', label: 'Sensor issue' }
    case 'inactive':
    default:
      return { emoji: '⚪', label: 'Not configured' }
  }
}

function previewBaseDegC(cfg) {
  const mode = cfg.temperature_mode || 'manual'
  if (mode === 'manual') return Number(cfg.target_temp ?? 24)
  const sch = cfg.schedule || {}
  const { key } = previewScheduleSlotKey(cfg)
  const fallback = Number(cfg.target_temp ?? 24)
  const raw = sch[key]
  const n = raw !== undefined && raw !== '' ? Number(raw) : fallback
  return Number.isFinite(n) ? n : fallback
}

const AI_CONFIG_KEYS = [
  'ai_enabled', 'ai_provider', 'ai_ollama_url', 'ai_ollama_model',
  'ai_api_key', 'ai_api_base_url', 'ai_api_model', 'ai_api_timeout', 'ai_api_json_object_format',
]

// ── Main Settings page (room-scoped: GET/PUT /api/rooms/{room_id}) ─────────────
export default function Settings() {
  const { activeRoomId: roomId, setActiveRoom, rooms: roomsList, refreshRooms } = useRoom()
  const { resetRoomData } = useRoomData()

  const [cfg,        setCfg]        = useState({})
  const [entities,   setEntities]   = useState([])
  const [roomTitle,  setRoomTitle]  = useState('')
  const [loadError,  setLoadError]  = useState(null)
  const [saving,     setSaving]     = useState(false)
  const [saveStatus, setSaveStatus] = useState(null)
  const [saveMsg,    setSaveMsg]    = useState('')
  const [loading,    setLoading]    = useState(true)
  const [outdoorTemp, setOutdoorTemp] = useState(null)
  const [roomDisabled, setRoomDisabled] = useState(false)
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [deletePurgeAnalytics, setDeletePurgeAnalytics] = useState(false)
  const [roomActionBusy, setRoomActionBusy] = useState(false)

  // Per-dropdown search state (each search is independent)
  const [presenceSearch,    setPresenceSearch]    = useState('')
  const [zoneSearch,        setZoneSearch]        = useState('')
  const [tempSearch,        setTempSearch]        = useState('')
  const [humiditySearch,   setHumiditySearch]    = useState('')
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

  /** FP2 zone UI: shown when a zone entity is set OR user enables advanced for this room. */
  const [zonePanelAdvanced, setZonePanelAdvanced] = useState(false)
  /** Live zone runtime from GET /api/status (dwell progress, phase). */
  const [zoneLive, setZoneLive] = useState(null)

  useEffect(() => {
    setZonePanelAdvanced(false)
  }, [roomId])

  useEffect(() => {
    if (String(cfg.zone_entity_id || '').trim()) {
      setZonePanelAdvanced(true)
    }
  }, [cfg.zone_entity_id])

  useEffect(() => {
    getWeather()
      .then(w => setOutdoorTemp(w.outdoor_temp ?? null))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!roomId || roomDisabled) {
      setZoneLive(null)
      return
    }
    let alive = true
    const poll = () => {
      getStatus(roomId)
        .then(s => {
          if (alive) setZoneLive(s.zone_status ?? null)
        })
        .catch(() => {
          if (alive) setZoneLive(null)
        })
    }
    poll()
    const t = setInterval(poll, 2000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [roomId, roomDisabled])

  useEffect(() => {
    if (!roomId) {
      setCfg({})
      setRoomTitle('')
      setRoomDisabled(false)
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setDevicesError(null)
    setLoadError(null)
    Promise.all([
      getRoom(roomId),
      getEntities(),
      getDevices().catch(err => {
        setDevicesError(String(err))
        return []
      }),
    ])
      .then(([detail, e, devs]) => {
        if (!alive) return
        const c = { ...detail.effective }
        if (c.weather_api_key === '***') c.weather_api_key = ''
        if (c.ai_api_key === '***') c.ai_api_key = ''
        setCfg(c)
        setRoomTitle(detail.room?.name || roomId)
        setRoomDisabled(Boolean(detail.room?.disabled))
        setEntities(e)
        setAllDevices(devs)
        setSelectedDevice(
          devs.find(d => d.device_id === String(c.energy_device_id || '').trim()) || null,
        )
      })
      .catch(err => {
        console.error(err)
        if (alive) {
          setCfg({})
          setLoadError(err?.message || String(err))
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => { alive = false }
  }, [roomId])

  const patch = useCallback((key, val) => {
    setCfg(prev => ({ ...prev, [key]: val }))
  }, [])

  const patchScheduleTemp = useCallback((key, val) => {
    setCfg(prev => ({
      ...prev,
      schedule: {
        ...(prev.schedule && typeof prev.schedule === 'object' ? prev.schedule : {}),
        [key]: val,
      },
    }))
  }, [])

  const handleSave = async () => {
    if (!roomId) return
    setSaving(true)
    setSaveStatus(null)
    try {
      const settings = {}
      for (const k of ROOM_SETTINGS_KEYS) {
        if (cfg[k] !== undefined) settings[k] = cfg[k]
      }
      if ((cfg.effective_mode || 'auto') === 'auto') {
        settings.manual_effective_temp = null
      }
      if (!settings.weather_api_key) delete settings.weather_api_key

      if (settings.zone_dwell_seconds != null && settings.zone_dwell_seconds !== '') {
        const zd = Math.max(5, Math.min(300, Math.round(Number(settings.zone_dwell_seconds))))
        settings.zone_dwell_seconds = zd
      }
      if (!String(settings.zone_entity_id || '').trim()) {
        delete settings.zone_entity_id
        settings.zone_required_for_on = false
      }

      const ai_config = {}
      for (const k of AI_CONFIG_KEYS) {
        if (cfg[k] !== undefined) ai_config[k] = cfg[k]
      }
      if (!ai_config.ai_api_key) delete ai_config.ai_api_key

      const saved = await updateRoom(roomId, {
        name: (cfg.room_name || roomTitle || 'Room').trim(),
        climate_entity: (cfg.ac_entity || cfg.climate_entity || '').trim(),
        presence_entity: cfg.presence_entity?.trim() || null,
        indoor_temp_entity: cfg.indoor_temp_entity?.trim() || null,
        indoor_humidity_entity: cfg.indoor_humidity_entity?.trim() || null,
        energy_device_id: (cfg.energy_device_id || selectedDevice?.device_id || '').trim() || null,
        energy_device_name: (cfg.energy_device_name || selectedDevice?.name || '').trim() || null,
        energy_power_entity: cfg.energy_power_entity?.trim() || null,
        energy_kwh_entity: cfg.energy_kwh_entity?.trim() || null,
        settings,
        ai_config,
      })
      setSaveStatus('ok')
      setSaveMsg(
        saved?.config_warnings?.length
          ? `Room settings saved with warning: ${saved.config_warnings.join('; ')}`
          : 'Room settings saved - logic engine updated',
      )
      const detail = await getRoom(roomId)
      const c = { ...detail.effective }
      if (c.weather_api_key === '***') c.weather_api_key = ''
      if (c.ai_api_key === '***') c.ai_api_key = ''
      setCfg(c)
      setRoomTitle(detail.room?.name || roomId)
      setRoomDisabled(Boolean(detail.room?.disabled))
      setSelectedDevice(
        allDevices.find(d => d.device_id === String(c.energy_device_id || '').trim()) || selectedDevice,
      )
      refreshRooms().catch(() => {})
    } catch (err) {
      console.error('Save failed:', err)
      setSaveStatus('error')
      setSaveMsg('Failed to save settings')
    } finally {
      setSaving(false)
      setTimeout(() => setSaveStatus(null), 4000)
    }
  }

  const toggleAutomationPaused = async () => {
    if (!roomId || roomActionBusy) return
    setRoomActionBusy(true)
    setSaveStatus(null)
    try {
      if (roomDisabled) {
        await enableRoom(roomId)
        setRoomDisabled(false)
      } else {
        await disableRoom(roomId)
        setRoomDisabled(true)
      }
      await refreshRooms()
      const detail = await getRoom(roomId).catch(() => null)
      if (detail?.room) setRoomDisabled(Boolean(detail.room.disabled))
    } catch (err) {
      console.error(err)
      setSaveStatus('error')
      setSaveMsg(err?.message || 'Could not update automation state')
      setTimeout(() => setSaveStatus(null), 4000)
    } finally {
      setRoomActionBusy(false)
    }
  }

  const openDeleteRoomModal = () => {
    setDeletePurgeAnalytics(false)
    setDeleteModalOpen(true)
  }

  const executeRemoveRoom = async () => {
    if (!roomId || roomActionBusy) return
    setRoomActionBusy(true)
    try {
      await deleteRoom(roomId, { purge: deletePurgeAnalytics })
      setDeleteModalOpen(false)
      resetRoomData()
      const list = await refreshRooms()
      const activeStillValid = Array.isArray(list) && list.some((x) => x.id === roomId)
      if (!activeStillValid) {
        if (list?.length && list[0]?.id) setActiveRoom(list[0].id)
        else setActiveRoom(null)
      }
    } catch (err) {
      console.error(err)
      setSaveStatus('error')
      setSaveMsg(err?.message || 'Failed to remove room')
      setTimeout(() => setSaveStatus(null), 5000)
    } finally {
      setRoomActionBusy(false)
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
    patch('energy_device_id', device?.device_id || '')
    patch('energy_device_name', device?.name || '')
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

  const hasZoneEntity = Boolean(String(cfg.zone_entity_id || '').trim())
  const showZonePanel = zonePanelAdvanced || hasZoneEntity
  const zoneDwellClamped = Math.min(
    300,
    Math.max(5, Math.round(Number(cfg.zone_dwell_seconds ?? 20)) || 20),
  )
  const zoneRequiredOn = Boolean(cfg.zone_required_for_on)
  const zoneControlActive = hasZoneEntity || zoneRequiredOn

  const zoneEntityMeta = useMemo(() => {
    const id = String(cfg.zone_entity_id || '').trim()
    if (!id) return null
    return entities.find(e => e.entity_id === id) || null
  }, [entities, cfg.zone_entity_id])

  const zoneDcNorm =
    zoneEntityMeta?.device_class != null && String(zoneEntityMeta.device_class).trim() !== ''
      ? String(zoneEntityMeta.device_class).toLowerCase()
      : null
  const warnZoneGatingNoSensor = zoneRequiredOn && !hasZoneEntity
  const warnZoneWrongDeviceClass =
    Boolean(hasZoneEntity && zoneDcNorm && zoneDcNorm !== 'occupancy' && zoneDcNorm !== 'presence')
  const hintZoneUnknownDeviceClass = Boolean(hasZoneEntity && !zoneDcNorm)

  if (loading && roomId) {
    return (
      <div className="flex items-center justify-center min-h-[40vh] text-gray-500 px-6">
        Loading configuration…
      </div>
    )
  }

  if (!roomId) {
    return (
      <div className="container-app max-w-2xl px-4 sm:px-6 py-6 pb-24 md:pb-8 space-y-4 min-w-0">
        <h1 className="text-xl font-bold">Settings</h1>
        <p className="text-sm text-gray-400">Select a room to edit. Settings apply only to that room — nothing is shared between rooms.</p>
        {roomsList.length === 0 ? (
          <div className="card text-sm text-gray-400">No rooms yet. Add one from the Dashboard.</div>
        ) : (
          <div className="card space-y-2">
            <label className="text-xs text-gray-500 uppercase">Room</label>
            <select
              className="w-full bg-gray-800 border border-blue-500/40 rounded-lg px-3 py-2 text-sm text-gray-100"
              value=""
              onChange={e => {
                const id = e.target.value
                if (!id) return
                setActiveRoom(id)
              }}
            >
              <option value="">Choose a room…</option>
              {roomsList.map(r => (
                <option key={r.id} value={r.id}>
                  {r.name || r.id}{r.disabled ? ' (paused)' : ''}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="container-app max-w-2xl px-4 sm:px-6 py-6 pb-24 md:pb-12 space-y-8 min-w-0">

      {/* Header + room switcher + Save */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-xl font-bold flex flex-wrap items-center gap-2">
            <span>
              Settings — <span className="text-blue-300">{roomTitle || roomId}</span>
            </span>
            {zoneControlActive && (
              <span
                className="text-[11px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-md bg-blue-900/50 border border-blue-700/60 text-blue-200"
                title="This room has zone entry options enabled or a zone sensor selected"
              >
                Zone control active
              </span>
            )}
          </h1>
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
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-gray-500">Switch room:</span>
          <select
            className="bg-gray-800 border border-blue-500/40 rounded-lg px-2 py-1.5 text-xs text-gray-100"
            value={roomId}
            onChange={e => {
              const id = e.target.value
              if (!id) return
              setActiveRoom(id)
            }}
          >
            {roomsList.map(r => (
              <option key={r.id} value={r.id}>
                {r.name || r.id}{r.disabled ? ' (paused)' : ''}
              </option>
            ))}
          </select>
          <button
            type="button"
            title="Remove this room from HawaAI"
            disabled={roomActionBusy}
            onClick={openDeleteRoomModal}
            className="p-2 rounded-lg bg-gray-800 border border-red-900/40 text-red-400 hover:bg-red-950/30 hover:border-red-700 disabled:opacity-40"
          >
            <Trash2 size={18} />
          </button>
          <span className="text-[11px] text-gray-600 font-mono truncate max-w-[200px]" title={roomId}>
            id: {roomId}
          </span>
        </div>

        {roomDisabled && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-900/20 border border-amber-700/50 text-sm text-amber-200/95">
            <AlertCircle size={16} className="shrink-0" />
            Automation is paused for this room — ticks are off; settings and history stay on disk.
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={roomActionBusy}
            onClick={() => void toggleAutomationPaused()}
            className={`text-sm font-medium px-4 py-2 rounded-lg border transition-colors disabled:opacity-45 ${
              roomDisabled
                ? 'bg-emerald-900/30 border-emerald-700 text-emerald-200 hover:bg-emerald-900/50'
                : 'bg-gray-800 border-amber-700/50 text-amber-200 hover:bg-amber-950/30'
            }`}
          >
            {roomDisabled ? 'Resume automation' : 'Pause automation'}
          </button>
          <span className="text-xs text-gray-600 max-w-[220px] leading-snug">
            Pause is the safe default — no scheduler ticks, data kept. Use the trash icon only to remove the room from configuration.
          </span>
        </div>
      </div>

      {loadError && (
        <div className="flex items-center gap-2 px-4 py-3 bg-red-900/30 border border-red-700 rounded-lg text-sm text-red-300">
          <AlertCircle size={16} />
          Could not load this room ({roomId}). It may have been removed.
        </div>
      )}

      {/* AC Control — single pipeline */}
      <div className="card space-y-4">
        <SectionHeader>AC Control</SectionHeader>
        <p className="text-xs text-gray-500 -mt-2">
          Single control path: HawaAI calls your Home Assistant climate entity (e.g. Aerostate).
          Your integration handles the physical AC.
        </p>

        <div className="space-y-2">
          <EntityDropdown
            label="AC entity (climate.*)"
            value={cfg.ac_entity || cfg.climate_entity || ''}
            onChange={v => patch('ac_entity', v)}
            entities={byDomain('climate')}
            search={climateSearch}
            onSearchChange={setClimateSearch}
          />
          <p className="text-xs text-gray-500">
            <span className="text-blue-400 font-medium">HawaAI</span>
            {' → '}
            <span className="text-blue-400 font-medium">Climate entity</span>
            {' → '}
            <span className="text-gray-400">AC</span>
          </p>

          <div>
            <Label>IR backend profile</Label>
            <select
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
              value={cfg.ir_backend || 'aerostate'}
              onChange={e => patch('ir_backend', e.target.value)}
            >
              <option value="aerostate">AeroState</option>
              <option value="tuya">Tuya</option>
            </select>
            <p className="text-xs text-gray-500 mt-1">
              AeroState sends one climate command for Broadlink-backed rooms. Tuya uses staged mode, temperature, and supported fan commands.
            </p>
          </div>

          {/* Connection status badge */}
          {(cfg.ac_entity || cfg.climate_entity) ? (
            <div className="flex items-center gap-2 px-3 py-2 bg-green-900/20 border border-green-800 rounded-lg">
              <span className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
              <span className="text-xs text-green-300">
                Connected — <code className="font-mono">{cfg.ac_entity || cfg.climate_entity}</code>
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
        <p className="text-xs text-gray-500 -mt-2">
          <span className="text-gray-400">Presence sensor</span> drives room occupancy and{' '}
          <span className="text-amber-200/90">AC OFF / vacancy</span> behavior. It is separate from zone entry (below).
        </p>

        <p className="text-xs text-gray-500 border border-gray-800/90 rounded-lg px-3 py-2 bg-gray-900/35 leading-relaxed">
          <span className="text-gray-400 font-medium">If using FP2:</span> use the sensor that reflects{' '}
          <span className="text-gray-300">general room occupancy</span> for Presence above; use a{' '}
          <span className="text-gray-300">specific zone</span> binary_sensor for Zone Control below (presence vs zone are different roles than old single-zone-only setups).
        </p>

        {/* FP2 zone entry — optional ON-only gate */}
        {showZonePanel ? (
          <div className="border border-blue-900/35 rounded-xl p-4 space-y-4 bg-blue-950/10">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="text-xs font-semibold uppercase tracking-widest text-blue-400">
                  Zone Entry Control (Optional)
                </p>
                <p className="text-xs text-gray-500 mt-1 max-w-xl">
                  <span className="text-gray-300">Zone sensor</span> validates entry for{' '}
                  <span className="text-blue-300">AC ON</span> only.{' '}
                  <span className="text-gray-400">AC OFF</span> still uses general presence above.
                </p>
              </div>
              {!hasZoneEntity && (
                <button
                  type="button"
                  onClick={() => setZonePanelAdvanced(false)}
                  className="text-xs text-gray-500 hover:text-gray-300 shrink-0"
                >
                  Hide
                </button>
              )}
            </div>

            {!roomDisabled && hasZoneEntity && (
              <div className="rounded-lg border border-blue-900/40 bg-blue-950/20 px-3 py-2 text-xs space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-gray-500 font-medium shrink-0">Zone status:</span>
                  {zoneLive ? (
                    <span className="text-gray-200">
                      {zonePhaseDisplay(zoneLive.phase).emoji}{' '}
                      {zonePhaseDisplay(zoneLive.phase).label}
                    </span>
                  ) : (
                    <span className="text-gray-500">Waiting for live data…</span>
                  )}
                </div>
                {zoneLive?.phase === 'waiting' && zoneLive.dwell_target_seconds != null && (
                  <p className="text-gray-400">
                    Dwell progress:{' '}
                    <span className="text-blue-300 font-mono tabular-nums">
                      {Math.min(
                        Math.round(Number(zoneLive.dwell_elapsed_seconds) || 0),
                        Number(zoneLive.dwell_target_seconds),
                      )}
                      {' / '}
                      {zoneLive.dwell_target_seconds} sec
                    </span>
                  </p>
                )}
              </div>
            )}

            {warnZoneGatingNoSensor && (
              <div className="flex items-start gap-2 text-xs text-amber-200/95 bg-amber-950/25 border border-amber-800/50 rounded-lg px-3 py-2">
                <AlertCircle size={14} className="shrink-0 mt-0.5" />
                Zone gating enabled but no zone sensor selected.
              </div>
            )}
            {warnZoneWrongDeviceClass && (
              <div className="flex items-start gap-2 text-xs text-amber-200/95 bg-amber-950/25 border border-amber-800/50 rounded-lg px-3 py-2">
                <AlertCircle size={14} className="shrink-0 mt-0.5" />
                <span>
                  Selected entity may not behave like a presence sensor
                  {zoneDcNorm ? ` (${zoneDcNorm}).` : '.'}
                </span>
              </div>
            )}
            {hintZoneUnknownDeviceClass && (
              <p className="text-xs text-gray-500">
                Home Assistant did not report <code className="text-gray-400">device_class</code> for this entity — prefer{' '}
                <span className="text-gray-400">occupancy</span> or <span className="text-gray-400">presence</span>.
              </p>
            )}

            <EntityDropdown
              label="Zone Presence Sensor (FP2) (binary_sensor.*)"
              value={cfg.zone_entity_id || ''}
              onChange={v => patch('zone_entity_id', v)}
              entities={byDomain('binary_sensor')}
              search={zoneSearch}
              onSearchChange={setZoneSearch}
            />
            <p className="text-xs text-gray-500 -mt-2">
              Select the FP2 zone entity used to allow AC ON only after confirmed presence in that zone.
            </p>

            <Toggle
              label="Require Zone Presence for AC ON"
              description="When enabled, cooling ON is gated until zone dwell confirms. When disabled, zone is logged only."
              checked={Boolean(cfg.zone_required_for_on)}
              onChange={v => patch('zone_required_for_on', v)}
            />

            <Input
              label="Zone Dwell Time (seconds)"
              type="number"
              min={5}
              max={300}
              step={1}
              value={zoneDwellClamped}
              onChange={v => patch('zone_dwell_seconds', v)}
            />
            <p className="text-xs text-gray-500 -mt-2">
              How long the zone must stay active before ON is allowed (5–300). Default 20.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {warnZoneGatingNoSensor && (
              <div className="flex items-start gap-2 text-xs text-amber-200/95 bg-amber-950/25 border border-amber-800/50 rounded-lg px-3 py-2">
                <AlertCircle size={14} className="shrink-0 mt-0.5" />
                Zone gating enabled but no zone sensor selected.
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-gray-800 bg-gray-900/40 px-3 py-2">
              <p className="text-xs text-gray-500">
                Optional: FP2 <span className="text-blue-300">zone entry</span> gating for AC ON (separate from presence).
              </p>
              <button
                type="button"
                onClick={() => setZonePanelAdvanced(true)}
                className="text-xs font-medium text-blue-400 hover:text-blue-300 shrink-0"
              >
                Show zone controls
              </button>
            </div>
          </div>
        )}

        {/* Indoor temp */}
        {(cfg.control_mode || 'thermostat') !== 'presence_only' && (
          <EntityDropdown
            label="Indoor Temperature Sensor (sensor.*)"
            value={cfg.indoor_temp_entity}
            onChange={v => patch('indoor_temp_entity', v)}
            entities={allSensors}
            search={tempSearch}
            onSearchChange={setTempSearch}
          />
        )}

        {/* Indoor humidity (optional, ML / comfort) */}
        <EntityDropdown
          label="Indoor Humidity (optional, sensor.*)"
          value={cfg.indoor_humidity_entity || ''}
          onChange={v => patch('indoor_humidity_entity', v)}
          entities={allSensors}
          search={humiditySearch}
          onSearchChange={setHumiditySearch}
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

        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Control mode</p>
          <div>
            <Label>Mode</Label>
            <select
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
              value={cfg.control_mode || 'thermostat'}
              onChange={e => patch('control_mode', e.target.value)}
            >
              <option value="thermostat">Thermostat</option>
              <option value="presence_only">Presence only</option>
            </select>
          </div>
          {(cfg.control_mode || 'thermostat') === 'presence_only' && (
            <p className="text-xs text-blue-200/90 bg-blue-950/25 border border-blue-900/40 rounded-lg px-3 py-2">
              Presence-only mode is for rooms without a temperature sensor. AC follows occupancy only.
            </p>
          )}
        </div>

        {(cfg.control_mode || 'thermostat') === 'presence_only' && (
          <div className="border border-gray-800 rounded-xl p-4 space-y-4">
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Presence-only timing</p>
            <Slider
              label="Presence dwell before ON"
              value={cfg.presence_only_on_dwell_seconds ?? 20}
              onChange={v => patch('presence_only_on_dwell_seconds', v)}
              min={0}
              max={300}
              step={5}
              unit=" sec"
            />
            <Slider
              label="Max runtime failsafe"
              value={cfg.presence_only_max_runtime_minutes ?? 240}
              onChange={v => patch('presence_only_max_runtime_minutes', v)}
              min={15}
              max={1440}
              step={15}
              unit=" min"
            />
          </div>
        )}

        {(cfg.control_mode || 'thermostat') !== 'presence_only' && (
        <>
        <Slider
          label="Target Temperature"
          value={cfg.target_temp ?? 24}
          onChange={v => setCfg(prev => ({ ...prev, temperature_mode: 'manual', target_temp: v }))}
          min={16} max={30} step={1} unit="°C"
        />
        <p className="text-xs text-gray-500 -mt-3">
          {(cfg.temperature_mode || 'manual') === 'manual'
            ? 'Manual setpoint — AC compares indoor temp against this (± hysteresis) after outdoor smart adjustment.'
            : 'Baseline / fallback degrees — also fills new schedule defaults. In Schedule modes, timing uses rows below.'}
          {' '}
          {(cfg.temperature_mode || 'manual') !== 'manual' && (
            <span className="text-amber-400/95">Moving this slider switches to Manual so this value drives control.</span>
          )}
        </p>

        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">
            Effective control target
          </p>
          <p className="text-xs text-gray-500 -mt-2 leading-relaxed">
            Thermostat decisions still use <strong className="text-gray-400">indoor temp vs effective ± hysteresis</strong>
            — unchanged. Here you choose how that <strong className="text-gray-400">effective</strong> point is set from
            today&apos;s <strong className="text-gray-400">schedule base</strong> (same row as the Temperature Schedule
            preview). <strong className="text-gray-400">Auto</strong> adds weather + AI nudges but never more than Max Δ
            above base. <strong className="text-gray-400">Manual</strong> fixes effective in [base … base+Max Δ].
          </p>
          <div>
            <Label>Comfort mode</Label>
            <select
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
              value={cfg.effective_mode || 'auto'}
              onChange={(e) => {
                const v = e.target.value
                if (v === 'manual') {
                  const base = previewBaseDegC(cfg)
                  setCfg((prev) => ({
                    ...prev,
                    effective_mode: v,
                    manual_effective_temp:
                      prev.manual_effective_temp != null && prev.manual_effective_temp !== ''
                        ? Number(prev.manual_effective_temp)
                        : base,
                  }))
                } else {
                  setCfg((prev) => ({ ...prev, effective_mode: v, manual_effective_temp: null }))
                }
              }}
            >
              <option value="auto">Auto (weather + AI, capped)</option>
              <option value="manual">Manual (fixed °C vs base band)</option>
            </select>
          </div>
          <Slider
            label="Max °C above schedule base"
            value={cfg.effective_max_delta_deg ?? 3}
            onChange={(v) => patch('effective_max_delta_deg', v)}
            min={1}
            max={5}
            step={0.5}
            unit="°C"
          />
          {(() => {
            const base = previewBaseDegC(cfg)
            const md = Number(cfg.effective_max_delta_deg ?? 3)
            const hi = base + md
            if ((cfg.effective_mode || 'auto') !== 'manual') return null
            let cur =
              cfg.manual_effective_temp != null && cfg.manual_effective_temp !== ''
                ? Number(cfg.manual_effective_temp)
                : base
            if (!Number.isFinite(cur)) cur = base
            cur = Math.min(Math.max(cur, base), hi)
            return (
              <Slider
                label="Manual effective temperature"
                value={cur}
                onChange={(v) => {
                  const x = Math.min(Math.max(Number(v), base), hi)
                  patch('manual_effective_temp', x)
                }}
                min={base}
                max={hi}
                step={0.5}
                unit="°C"
              />
            )
          })()}
          <p className="text-xs text-gray-500">
            Preview schedule base ≈{' '}
            <span className="font-mono text-blue-300">{previewBaseDegC(cfg).toFixed(1)}°C</span>
            {' '}(server resolves the active slot on save and every tick.)
          </p>
        </div>

        {/* Temperature schedule */}
        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <SectionHeader>Temperature Schedule</SectionHeader>
          <p className="text-xs text-gray-500 -mt-2">
            Fixed time bands (local clock). Edit °C only. Use <strong className="text-gray-400">Manual</strong> to ignore schedule
            and use the slider above alone.
          </p>
          <div>
            <Label>Mode</Label>
            <select
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
              value={cfg.temperature_mode || 'manual'}
              onChange={e => patch('temperature_mode', e.target.value)}
            >
              <option value="manual">Manual</option>
              <option value="schedule">Schedule</option>
              <option value="schedule_ai">Schedule + AI</option>
            </select>
            <p className="text-xs text-gray-500 mt-1 leading-relaxed">
              <strong className="text-gray-400">Manual</strong> — one fixed °C target (above).&nbsp;
              <strong className="text-gray-400">Schedule</strong> — four time bands with your °C per band + outdoor smart curve.&nbsp;
              <strong className="text-gray-400">Schedule + AI</strong> — same as Schedule, plus optional model nudge within ±1°C (enable AI below).
            </p>
          </div>
          <Input
            label="Schedule timezone (IANA, optional)"
            value={cfg.timezone ?? ''}
            onChange={v => patch('timezone', v)}
            placeholder="e.g. Asia/Kolkata — empty uses TZ env or UTC on the server"
          />
          {(() => {
            const tm = cfg.temperature_mode || 'manual'
            if (tm === 'manual') return null
            const { hour, key: activeKey } = previewScheduleSlotKey(cfg)
            return (
              <p className="text-xs text-amber-200/90 bg-amber-900/20 border border-amber-800/50 rounded-lg px-3 py-2">
                Preview — local hour&nbsp;
                <span className="font-mono">{String(hour).padStart(2, '0')}:00</span>
                {' → active row '}
                <span className="font-semibold text-amber-100">
                  {SCHEDULE_SLOT_ROWS.find(r => r.key === activeKey)?.label ?? '—'}
                </span>
                {' '}(uses timezone above when set).
              </p>
            )
          })()}
          <div className="space-y-2">
            <p className="text-xs font-semibold text-gray-400">Slot temperatures (°C)</p>
            {SCHEDULE_SLOT_ROWS.map(row => (
              <div
                key={row.key}
                className={`grid grid-cols-[1fr_auto_auto] items-center gap-2 rounded-lg px-2 py-1.5 ${
                  (cfg.temperature_mode || 'manual') !== 'manual' &&
                  previewScheduleSlotKey(cfg).key === row.key
                    ? 'bg-blue-900/25 border border-blue-700/40'
                    : ''
                }`}
              >
                <span className="text-sm text-gray-200">{row.label}</span>
                <span className="text-xs text-gray-500 tabular-nums">{row.hours}</span>
                <input
                  type="number"
                  min={16}
                  max={30}
                  step={1}
                  disabled={(cfg.temperature_mode || 'manual') === 'manual'}
                  className="w-20 bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-sm text-gray-100 text-right disabled:opacity-40"
                  value={
                    cfg.schedule?.[row.key] !== undefined && cfg.schedule?.[row.key] !== ''
                      ? cfg.schedule[row.key]
                      : (cfg.target_temp ?? 24)
                  }
                  onChange={e => patchScheduleTemp(row.key, Number(e.target.value))}
                />
              </div>
            ))}
          </div>
        </div>

        <Slider
          label="Hysteresis Band"
          value={cfg.hysteresis ?? 1.5}
          onChange={v => patch('hysteresis', v)}
          min={0.5} max={3.0} step={0.5} unit="°C"
        />
        <p className="text-xs text-gray-500 -mt-3">
          Larger band = fewer AC cycles. E.g. target 24°C + 1.5° band: ON at 25.5°C, OFF at 22.5°C.
        </p>
        </>
        )}

        <div className="border border-gray-800 rounded-xl p-4 space-y-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400">Control timing</p>
          <p className="text-xs text-gray-500 -mt-2">
            After the thermostat decides ON or OFF, HawaAI waits this long before sending the climate command.
            Safety (vacancy) and user commands skip the delay. Values are stored in seconds; shown in minutes.
          </p>
          <Slider
            label="Turn ON delay"
            value={(Number(cfg.on_delay_seconds) || 0) / 60}
            onChange={v => patch('on_delay_seconds', Math.round(v * 60))}
            min={0}
            max={10}
            step={0.25}
            unit=" min"
          />
          <Slider
            label="Turn OFF delay"
            value={(Number(cfg.off_delay_seconds) || 0) / 60}
            onChange={v => patch('off_delay_seconds', Math.round(v * 60))}
            min={0}
            max={10}
            step={0.25}
            unit=" min"
          />
        </div>

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
          {(cfg.control_mode || 'thermostat') !== 'presence_only' && (
          <>
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
            label="Enable AI Optimization"
            description="Soft setpoint and fan hints from an AI model. Choose Ollama (local) or an OpenAI-compatible cloud API below — no credentials are hardcoded."
            checked={cfg.ai_enabled ?? false}
            onChange={async (v) => {
              patch('ai_enabled', v)
              try {
                await updateRoom(roomId, { ai_config: { ai_enabled: v } })
              } catch (e) {
                console.error(e)
              }
            }}
          />
          <div>
            <Label>AI Provider</Label>
            <select
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-blue-500"
              value={cfg.ai_provider === 'api' ? 'api' : 'ollama'}
              onChange={e => patch('ai_provider', e.target.value)}
            >
              <option value="ollama">Ollama (local)</option>
              <option value="api">API (OpenAI-compatible)</option>
            </select>
            <p className="text-xs text-gray-500 mt-1">
              OpenAI-compatible HTTPS only. Fast option (user-configured): Groq — base URL{' '}
              <code className="text-gray-400">https://api.groq.com/openai/v1</code>, model e.g.{' '}
              <code className="text-gray-400">llama3-8b-8192</code>. Not hardcoded; enter in fields below.
            </p>
          </div>
          {cfg.ai_provider === 'api' ? (
            <>
              <PasswordInput
                label="API key"
                value={cfg.ai_api_key ?? ''}
                onChange={v => patch('ai_api_key', v)}
                placeholder="Required when provider is API"
              />
              <Input
                label="API base URL"
                value={cfg.ai_api_base_url ?? ''}
                onChange={v => patch('ai_api_base_url', v)}
                placeholder="https://api.groq.com/openai/v1"
              />
              <Input
                label="API model name"
                value={cfg.ai_api_model ?? ''}
                onChange={v => patch('ai_api_model', v)}
                placeholder="e.g. llama3-8b-8192"
              />
              <Input
                label="API request timeout (seconds)"
                value={cfg.ai_api_timeout ?? 60}
                onChange={v => patch('ai_api_timeout', v)}
                type="number"
                min={5}
                max={120}
                step={1}
              />
              <Toggle
                label="Request JSON object mode (response_format)"
                description="Enable only if your API supports OpenAI json_object (e.g. some OpenAI models). Off by default for Groq and other fast endpoints."
                checked={cfg.ai_api_json_object_format === true}
                onChange={v => patch('ai_api_json_object_format', v)}
              />
            </>
          ) : (
            <>
              <Input
                label="Ollama base URL (leave empty to use default: 172.30.32.1)"
                value={cfg.ai_ollama_url ?? ''}
                onChange={v => patch('ai_ollama_url', v)}
                placeholder="http://172.30.32.1:11434"
              />
              <Input
                label="Ollama model (optional; default gemma:2b — fast, structured JSON on Pi)"
                value={cfg.ai_ollama_model ?? ''}
                onChange={v => patch('ai_ollama_model', v)}
                placeholder="gemma:2b"
              />
            </>
          )}
          </>
          )}
          <Toggle
            label="Manual Override"
            description="Disable all automation"
            checked={cfg.manual_override ?? false}
            onChange={v => patch('manual_override', v)}
            danger
          />
        </div>

        {/* ── Smart Adjustment Preview ───────────────────────────────────── */}
        {(cfg.control_mode || 'thermostat') !== 'presence_only' && cfg.smart_temp_adjustment !== false && cfg.use_outdoor_temp !== false && (() => {
          const t = previewBaseDegC(cfg)
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
      {(cfg.control_mode || 'thermostat') !== 'presence_only' && (
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
      )}

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

      {deleteModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          role="presentation"
          aria-hidden={!deleteModalOpen}
          onClick={() => { if (!roomActionBusy) setDeleteModalOpen(false) }}
        >
          <div
            role="dialog"
            aria-labelledby="del-room-title"
            className="bg-gray-900 border border-gray-700 rounded-xl p-5 max-w-md w-full shadow-xl space-y-4"
            onClick={e => e.stopPropagation()}
          >
            <h2 id="del-room-title" className="text-lg font-semibold text-gray-100">
              Delete room?
            </h2>
            <p className="text-sm text-gray-400 leading-relaxed">
              The AC will turn off safely if HawaAI was controlling it. The room is disabled first so the scheduler stops.
            </p>

            <div className="space-y-2 text-sm">
              <label className="flex gap-3 items-start cursor-pointer">
                <input
                  type="radio"
                  className="mt-1 shrink-0 accent-blue-500"
                  checked={!deletePurgeAnalytics}
                  onChange={() => setDeletePurgeAnalytics(false)}
                />
                <span>
                  <span className="text-gray-100 font-medium">Keep history</span>
                  <span className="text-gray-500"> (recommended) — sessions and snapshots stay for analytics.</span>
                </span>
              </label>
              <label className="flex gap-3 items-start cursor-pointer">
                <input
                  type="radio"
                  className="mt-1 shrink-0 accent-red-600"
                  checked={deletePurgeAnalytics}
                  onChange={() => setDeletePurgeAnalytics(true)}
                />
                <span>
                  <span className="text-red-400 font-medium">Delete all data</span>
                  <span className="text-gray-500"> — sessions, snapshots, and AI audit rows for this room are removed permanently.</span>
                </span>
              </label>
            </div>

            <div className="flex flex-wrap justify-end gap-2 pt-2">
              <button
                type="button"
                disabled={roomActionBusy}
                onClick={() => setDeleteModalOpen(false)}
                className="px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 text-sm text-gray-200 hover:bg-gray-800/80"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={roomActionBusy}
                onClick={() => void executeRemoveRoom()}
                className="px-4 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-medium disabled:opacity-45"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  )
}
