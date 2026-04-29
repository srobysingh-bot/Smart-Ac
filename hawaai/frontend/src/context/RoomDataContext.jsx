import {
  createContext,
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

  const [state, setState] = useState({
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

  useEffect(() => {
    if (!activeRoomId) {
      setState({
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

    setState((prev) => ({
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
        setState({
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
        setState({
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

    const pollStatus = window.setInterval(() => {
      getStatus(rid)
        .then((s) => {
          if (cancelled || gen !== loadGenRef.current) return
          setState((prev) => ({ ...prev, status: s }))
        })
        .catch(() => {})
    }, 5000)

    const pollSnapshots = window.setInterval(() => {
      getSnapshots(120, rid)
        .then((snaps) => {
          if (cancelled || gen !== loadGenRef.current) return
          setState((prev) => ({ ...prev, snapshots: snaps }))
        })
        .catch(() => {})
    }, 30000)

    const pollAi = window.setInterval(() => {
      getAiStatus(rid)
        .then((ai) => {
          if (cancelled || gen !== loadGenRef.current) return
          setState((prev) => ({ ...prev, ai }))
        })
        .catch(() => {})
    }, 5000)

    const { close } = connectLive(rid, (msg) => {
      if (cancelled || gen !== loadGenRef.current) return
      if (!msg || msg.type !== 'tick') return

      const { type: _t, room_id: tickRoom, ...rest } = msg
      if (!tickRoom || tickRoom !== rid) return

      setState((prev) => {
        if (gen !== loadGenRef.current) return prev
        if (!tickRoom || tickRoom !== rid) return prev
        const cur = prev.status
        const merged = cur ? { ...cur, ...rest } : { ...rest }
        if (merged.room_id != null && merged.room_id !== rid) return prev
        return { ...prev, status: merged }
      })
    })

    return () => {
      cancelled = true
      window.clearInterval(pollStatus)
      window.clearInterval(pollSnapshots)
      window.clearInterval(pollAi)
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
  }, [state, activeRoomId])

  return <RoomDataContext.Provider value={value}>{children}</RoomDataContext.Provider>
}

export function useRoomData() {
  const ctx = useContext(RoomDataContext)
  if (!ctx) {
    throw new Error('useRoomData must be used within RoomDataProvider')
  }
  return ctx
}
