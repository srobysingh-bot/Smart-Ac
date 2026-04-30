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
  const wsConnectedRef = useRef(false)

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
    wsConnectedRef.current = false
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
        const [status, snapshots, ai, stats] = await Promise.all([
          getStatus(rid),
          getSnapshots(120, rid),
          getAiStatus(rid),
          getSessionStats(rid),
        ])
        if (cancelled || gen !== loadGenRef.current) return
        setRoomData({
          status,
          snapshots,
          ai,
          stats,
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

    const { close } = connectLive(rid, (msg) => {
      if (cancelled || gen !== loadGenRef.current) return
      if (!msg || msg.type !== 'tick') return
      if (msg.room_id != null && msg.room_id !== rid) return
      wsConnectedRef.current = true
      setRoomData((prev) => {
        if (gen !== loadGenRef.current) return prev
        if (!prev.status) return prev
        const { type: _tickType, ...tickFields } = msg
        return {
          ...prev,
          status: {
            ...prev.status,
            ...tickFields,
            effective_ac_on:
              tickFields.effective_ac_on ?? prev.status.effective_ac_on,
            physical_ac_on:
              tickFields.physical_ac_on ?? tickFields.ac_is_on ?? prev.status.physical_ac_on,
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
      if (wsConnectedRef.current) return
      getStatus(rid)
        .then((s) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, status: s }))
        })
        .catch(() => {})
    }, 15000)

    const snapId = window.setInterval(() => {
      getSnapshots(120, rid)
        .then((snaps) => {
          if (cancelled || gen !== loadGenRef.current) return
          setRoomData((prev) => ({ ...prev, snapshots: snaps }))
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

    return () => {
      cancelled = true
      window.clearInterval(pollId)
      window.clearInterval(snapId)
      window.clearInterval(pollAiId)
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
