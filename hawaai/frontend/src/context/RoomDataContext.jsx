import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  connectLive,
  getAiStatus,
  getSessionStats,
  getSnapshots,
  getStatus,
} from '../api/smartcool.js'
import { useRoom } from './RoomContext.jsx'

const RoomDataContext = createContext(null)

export function RoomDataProvider({ children }) {
  const { activeRoomId } = useRoom()
  const loadGenRef = useRef(0)
  const lastWsMessageAtRef = useRef(0)
  const lastStatusPollAtRef = useRef(0)
  const statusPollInFlightRef = useRef(false)

  // setRoomData replaces full slice; always pass a complete object (or updater).
  const [state, setRoomData] = useState({
    status: null,
    snapshots: [],
    ai: null,
    stats: null,
    loading: true,
    loadError: null,
    previousStatus: null,
    previousSnapshots: [],
    previousStats: null,
  })

  const resetRoomData = useCallback(() => {
    loadGenRef.current += 1
    setRoomData({
      status: null,
      snapshots: [],
      ai: null,
      stats: null,
      loading: false,
      loadError: null,
      previousStatus: null,
      previousSnapshots: [],
      previousStats: null,
    })
  }, [])

  useEffect(() => {
    if (!activeRoomId) {
      setRoomData({
        status: null,
        snapshots: [],
        ai: null,
        stats: null,
        loading: false,
        loadError: null,
        previousStatus: null,
        previousSnapshots: [],
        previousStats: null,
      })
      return
    }

    const gen = ++loadGenRef.current
    const rid = activeRoomId
    let cancelled = false
    lastWsMessageAtRef.current = 0
    lastStatusPollAtRef.current = 0
    statusPollInFlightRef.current = false
    setRoomData((prev) => ({
      status: null,
      snapshots: [],
      ai: null,
      stats: null,
      loading: true,
      loadError: null,
      previousStatus: prev.status,
      previousSnapshots: prev.snapshots || [],
      previousStats: prev.stats,
    }))

    async function load() {
      try {
        const [statusRes, snapshotsRes, aiRes, statsRes] = await Promise.allSettled([
          getStatus(rid),
          getSnapshots(120, rid),
          getAiStatus(rid),
          getSessionStats(rid),
        ])
        if (statusRes.status !== 'fulfilled') throw statusRes.reason
        if (cancelled || gen !== loadGenRef.current) return
        setRoomData({
          status: statusRes.value,
          snapshots: snapshotsRes.status === 'fulfilled' ? snapshotsRes.value : [],
          ai: aiRes.status === 'fulfilled' ? aiRes.value : null,
          stats: statsRes.status === 'fulfilled' ? statsRes.value : null,
          loading: false,
          loadError: null,
          previousStatus: null,
          previousSnapshots: [],
          previousStats: null,
        })
      } catch (err) {
        console.error('[HawaAI] Room load failed', err)
        if (cancelled || gen !== loadGenRef.current) return
        setRoomData({
          status: null,
          snapshots: [],
          ai: null,
          stats: null,
          loading: false,
          loadError: err,
          previousStatus: null,
          previousSnapshots: [],
          previousStats: null,
        })
      }
    }

    load()

    const refreshStatus = () => {
      if (statusPollInFlightRef.current) return
      statusPollInFlightRef.current = true
      lastStatusPollAtRef.current = Date.now()
      getStatus(rid)
        .then((s) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({
            ...prev,
            status: s,
            loading: false,
            loadError: null,
          }))
        })
        .catch(() => {})
        .finally(() => {
          statusPollInFlightRef.current = false
        })
    }

    const onRoomConfigSaved = (event) => {
      const savedRoomId = normalizeRoomKey(event?.detail?.roomId)
      if (savedRoomId && savedRoomId !== normalizeRoomKey(rid)) return
      refreshStatus()
    }
    window.addEventListener('hawaai:room-config-saved', onRoomConfigSaved)

    const { close } = connectLive(rid, (msg) => {
      if (cancelled || gen !== loadGenRef.current) return
      if (!msg || msg.type !== 'tick') return
      if (msg.room_id != null && normalizeRoomKey(msg.room_id) !== normalizeRoomKey(rid)) return
      lastWsMessageAtRef.current = Date.now()
      setRoomData((prev) => {
        if (gen !== loadGenRef.current) return prev
        if (!prev.status) return prev
        const { type: _tickType, ...tickFields } = msg
        const physicalAcOn =
          tickFields.physical_ac_on ?? tickFields.ac_is_on ?? prev.status.physical_ac_on
        const energyWatts = tickFields.energy_watts ?? prev.status.energy_watts
        return {
          ...prev,
          status: {
            ...prev.status,
            ...tickFields,
            presence: tickFields.presence ?? tickFields.occupied ?? prev.status.presence,
            watt_draw: tickFields.watt_draw ?? energyWatts ?? prev.status.watt_draw,
            energy_watts: energyWatts,
            effective_target:
              tickFields.effective_target ?? tickFields.target_temp ?? prev.status.effective_target,
            runtime: tickFields.runtime ?? prev.status.runtime,
            effective_ac_on:
              tickFields.effective_ac_on ?? prev.status.effective_ac_on,
            physical_ac_on: physicalAcOn,
            ac_is_on: tickFields.ac_is_on ?? prev.status.ac_is_on,
            ac_state: tickFields.ac_state ?? prev.status.ac_state,
            ac_state_source:
              tickFields.ac_state_source ?? prev.status.ac_state_source,
            pending_action: tickFields.pending_action ?? prev.status.pending_action,
            pending_remaining_seconds:
              tickFields.pending_remaining_seconds ?? prev.status.pending_remaining_seconds,
            pending_since_ts: tickFields.pending_since_ts ?? prev.status.pending_since_ts,
          },
        }
      })
    })

    const pollId = window.setInterval(() => {
      const now = Date.now()
      const lastWs = lastWsMessageAtRef.current
      const wsStale = !lastWs || now - lastWs > 8_000
      const reconcileDue = now - lastStatusPollAtRef.current > 30_000
      const tabHidden = typeof document !== 'undefined' && document.hidden
      if (tabHidden && !wsStale && !reconcileDue) return
      if (wsStale || reconcileDue) refreshStatus()
    }, 5_000)

    const snapId = window.setInterval(() => {
      getSnapshots(120, rid)
        .then((snaps) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, snapshots: snaps }))
        })
        .catch(() => {})
    }, 30000)

    const statsId = window.setInterval(() => {
      getSessionStats(rid)
        .then((stats) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, stats }))
        })
        .catch(() => {})
    }, 30000)

    const pollAiId = window.setInterval(() => {
      getAiStatus(rid)
        .then((ai) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, ai }))
        })
        .catch(() => {})
    }, 5000)

    const onVisibilityOrFocus = () => {
      if (typeof document !== 'undefined' && document.hidden) return
      refreshStatus()
      getSnapshots(120, rid)
        .then((snaps) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, snapshots: snaps }))
        })
        .catch(() => {})
      getSessionStats(rid)
        .then((stats) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, stats }))
        })
        .catch(() => {})
    }
    window.addEventListener('focus', onVisibilityOrFocus)
    window.addEventListener('online', onVisibilityOrFocus)
    document.addEventListener('visibilitychange', onVisibilityOrFocus)

    return () => {
      cancelled = true
      window.clearInterval(pollId)
      window.clearInterval(snapId)
      window.clearInterval(statsId)
      window.clearInterval(pollAiId)
      window.removeEventListener('focus', onVisibilityOrFocus)
      window.removeEventListener('online', onVisibilityOrFocus)
      document.removeEventListener('visibilitychange', onVisibilityOrFocus)
      window.removeEventListener('hawaai:room-config-saved', onRoomConfigSaved)
      close()
    }
  }, [activeRoomId])

  const value = useMemo(() => {
    const { status, snapshots, stats, loading, previousStatus, previousSnapshots, previousStats, ...rest } =
      state
    const displayStatus = status ?? (loading && previousStatus ? previousStatus : null)
    const displaySnapshots =
      snapshots?.length > 0 ? snapshots : loading && previousSnapshots?.length > 0 ? previousSnapshots : []
    const displayStats = stats ?? (loading && previousStats != null ? previousStats : null)

    const showSoftLoading = Boolean(loading && previousStatus)

    return {
      resetRoomData,
      ...rest,
      status,
      snapshots,
      stats,
      loading,
      activeRoomId,
      previousStatus,
      displayStatus,
      displaySnapshots,
      displayStats,
      showSoftLoading,
    }
  }, [state, activeRoomId, resetRoomData])

  return <RoomDataContext.Provider value={value}>{children}</RoomDataContext.Provider>
}

export function useRoomData() {
  const ctx = useContext(RoomDataContext)
  if (!ctx) {
    throw new Error('useRoomData must be used within RoomDataProvider')
  }
  return ctx
}
