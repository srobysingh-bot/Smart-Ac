import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { getRooms } from '../api/smartcool.js'

export const ROOM_STORAGE_KEY = 'hawaai_active_room'

const RoomContext = createContext(null)

export function RoomProvider({ children }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const { pathname } = useLocation()
  const [rooms, setRooms] = useState([])
  const [roomsLoading, setRoomsLoading] = useState(true)
  const [activeRoomId, setActiveRoomId] = useState(null)

  const initRef = useRef(false)

  const refreshRooms = useCallback(async () => {
    try {
      const r = await getRooms()
      const list = r.rooms || []
      setRooms(list)
      return list
    } catch (e) {
      console.warn('[HawaAI] getRooms failed:', e)
      return []
    }
  }, [])

  useEffect(() => {
    let alive = true
    setRoomsLoading(true)
    refreshRooms().finally(() => {
      if (alive) setRoomsLoading(false)
    })
    return () => { alive = false }
  }, [refreshRooms])

  // One-time: URL room_id (valid) > localStorage > single room
  useEffect(() => {
    if (!rooms.length) return
    if (initRef.current) return
    initRef.current = true

    const urlId = (searchParams.get('room_id') || '').trim()
    const lsRaw =
      typeof localStorage !== 'undefined'
        ? (localStorage.getItem(ROOM_STORAGE_KEY) || '').trim()
        : ''
    const isValid = (id) => Boolean(id && rooms.some((x) => x.id === id))

    let pick = null
    if (isValid(urlId)) pick = urlId
    else if (isValid(lsRaw)) pick = lsRaw
    else if (rooms.length >= 1) pick = rooms[0].id

    setActiveRoomId(pick)
    if (pick) {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ROOM_STORAGE_KEY, pick)
      }
      if (searchParams.get('room_id') !== pick) {
        setSearchParams(
          (prev) => {
            const next = new URLSearchParams(prev)
            next.set('room_id', pick)
            return next
          },
          { replace: true },
        )
      }
    } else {
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(ROOM_STORAGE_KEY)
      }
      if (searchParams.has('room_id')) {
        setSearchParams(
          (prev) => {
            const next = new URLSearchParams(prev)
            next.delete('room_id')
            return next
          },
          { replace: true },
        )
      }
    }
  }, [rooms, searchParams, setSearchParams])

  // ── Persist / restore ─────────────────────────────────────────────────────
  // Cross-tab: another tab updates localStorage → adopt valid room here.
  useEffect(() => {
    const onStorage = (e) => {
      if (!e.key || e.key !== ROOM_STORAGE_KEY) return
      const next = (e.newValue || '').trim()
      if (!rooms.length) return
      if (next && rooms.some((r) => r.id === next) && next !== activeRoomId) {
        setActiveRoom(next)
      }
      if (!next && activeRoomId) {
        setActiveRoom(null)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [rooms, activeRoomId, setActiveRoom])

  // Tab visibility / BF cache: if localStorage has a valid room id and state mismatches, reconcile.
  useEffect(() => {
    const reconcile = () => {
      if (document.visibilityState !== 'visible') return
      if (!rooms.length) return
      const saved =
        typeof localStorage !== 'undefined'
          ? (localStorage.getItem(ROOM_STORAGE_KEY) || '').trim()
          : ''
      if (!saved || !rooms.some((x) => x.id === saved)) return
      if (activeRoomId === saved) return
      setActiveRoom(saved)
    }
    document.addEventListener('visibilitychange', reconcile)
    window.addEventListener('pageshow', reconcile)
    return () => {
      document.removeEventListener('visibilitychange', reconcile)
      window.removeEventListener('pageshow', reconcile)
    }
  }, [rooms, activeRoomId, setActiveRoom])

  // Adopt room from query when it changes (browser back/forward, manual URL edit)
  useEffect(() => {
    if (!initRef.current || !rooms.length) return
    const urlId = (searchParams.get('room_id') || '').trim()
    const isValid = (id) => Boolean(id && rooms.some((x) => x.id === id))
    if (isValid(urlId) && urlId !== activeRoomId) {
      setActiveRoomId(urlId)
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ROOM_STORAGE_KEY, urlId)
      }
    }
  }, [searchParams, rooms, activeRoomId])

  // Active room no longer exists
  useEffect(() => {
    if (!initRef.current || !rooms.length) return
    if (!activeRoomId) return
    if (rooms.some((r) => r.id === activeRoomId)) return

    const pick = rooms.length === 1 ? rooms[0].id : null
    setActiveRoomId(pick)
    if (pick) {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ROOM_STORAGE_KEY, pick)
      }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set('room_id', pick)
          return next
        },
        { replace: true },
      )
    } else {
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(ROOM_STORAGE_KEY)
      }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.delete('room_id')
          return next
        },
        { replace: true },
      )
    }
  }, [rooms, activeRoomId, setSearchParams])

  // After tab change, re-attach ?room_id= if missing (HashRouter strips query on bare links)
  useEffect(() => {
    if (!initRef.current || !activeRoomId) return
    const cur = (searchParams.get('room_id') || '').trim()
    if (cur === activeRoomId) return
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.set('room_id', activeRoomId)
        return next
      },
      { replace: true },
    )
  }, [pathname, activeRoomId, searchParams, setSearchParams])

  const setActiveRoom = useCallback((id) => {
    const nextId = id ? String(id).trim() || null : null
    setActiveRoomId(nextId)
    if (typeof localStorage !== 'undefined') {
      if (nextId) localStorage.setItem(ROOM_STORAGE_KEY, nextId)
      else localStorage.removeItem(ROOM_STORAGE_KEY)
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (nextId) next.set('room_id', nextId)
        else next.delete('room_id')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const value = useMemo(
    () => ({
      rooms,
      roomsLoading,
      refreshRooms,
      activeRoomId,
      setActiveRoom,
    }),
    [rooms, roomsLoading, refreshRooms, activeRoomId, setActiveRoom],
  )

  return <RoomContext.Provider value={value}>{children}</RoomContext.Provider>
}

export function useRoom() {
  const ctx = useContext(RoomContext)
  if (!ctx) {
    throw new Error('useRoom must be used within RoomProvider')
  }
  return ctx
}
