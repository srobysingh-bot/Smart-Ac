import { HashRouter, NavLink, Route, Routes } from 'react-router-dom'
import {
  LayoutDashboard,
  History,
  BarChart2,
  Settings,
  Wind,
} from 'lucide-react'
import { RoomProvider, useRoom } from './context/RoomContext.jsx'
import { RoomDataProvider } from './context/RoomDataContext.jsx'
import Dashboard      from './pages/Dashboard.jsx'
import SessionHistory from './pages/SessionHistory.jsx'
import Analytics      from './pages/Analytics.jsx'
import SettingsPage   from './pages/Settings.jsx'

const NAV_ITEMS = [
  { to: '/',          icon: LayoutDashboard, label: 'Dashboard'   },
  { to: '/history',   icon: History,          label: 'Sessions'    },
  { to: '/analytics', icon: BarChart2,        label: 'Analytics'    },
  { to: '/settings',  icon: Settings,         label: 'Settings'    },
]

function NavItemDesktop({ to, icon: Icon, label }) {
  const { activeRoomId } = useRoom()
  const suffix = activeRoomId ? `?room_id=${encodeURIComponent(activeRoomId)}` : ''
  return (
    <NavLink
      to={`${to}${suffix}`}
      end={to === '/'}
      className={({ isActive }) =>
        `touch-target-inline flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap
         ${isActive
           ? 'bg-blue-600 text-white shadow-lg shadow-blue-900/30'
           : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100'
         }`
      }
    >
      <Icon size={18} className="shrink-0" aria-hidden />
      <span>{label}</span>
    </NavLink>
  )
}

function NavItemMobile({ to, icon: Icon, label }) {
  const { activeRoomId } = useRoom()
  const suffix = activeRoomId ? `?room_id=${encodeURIComponent(activeRoomId)}` : ''
  return (
    <NavLink
      to={`${to}${suffix}`}
      end={to === '/'}
      className={({ isActive }) =>
        `flex flex-col items-center justify-center gap-0.5 min-h-[52px] flex-1 min-w-0 px-1 pt-1 pb-safe text-[10px] font-medium transition-colors tap-highlight-none
         ${isActive ? 'text-blue-400' : 'text-gray-500 active:text-gray-300'}`
      }
    >
      <Icon size={22} strokeWidth={2} aria-hidden />
      <span className="truncate max-w-[4.25rem] text-center leading-tight">{label}</span>
    </NavLink>
  )
}

export default function App() {
  return (
    <HashRouter>
      <RoomProvider>
        <RoomDataProvider>
          <div className="flex flex-col min-h-[100dvh] bg-gray-950 text-gray-100">
          {/* Desktop / tablet — top navigation */}
          <header className="hidden md:flex shrink-0 items-center gap-6 border-b border-gray-800 bg-gray-900/95 backdrop-blur-sm px-4 lg:px-6 py-3">
            <div className="flex items-center gap-2 shrink-0">
              <Wind size={22} className="text-blue-400" aria-hidden />
              <span className="font-bold text-lg text-white">HawaAI</span>
            </div>
            <nav className="flex flex-wrap items-center gap-1 flex-1 min-w-0" aria-label="Main">
              {NAV_ITEMS.map(item => (
                <NavItemDesktop key={item.to} {...item} />
              ))}
            </nav>
            <div className="hidden lg:block text-xs text-gray-600 shrink-0 whitespace-nowrap">
              v1.4.28 · All data local
            </div>
          </header>

          {/* Scrollable content — bottom padding clears fixed mobile nav */}
          <main className="app-main-scroll flex-1 min-w-0 overflow-x-hidden pb-[calc(3.65rem+env(safe-area-inset-bottom,0px))] md:pb-0">
            <Routes>
              <Route path="/"          element={<Dashboard />} />
              <Route path="/history"   element={<SessionHistory />} />
              <Route path="/analytics" element={<Analytics />} />
              <Route path="/settings"  element={<SettingsPage />} />
            </Routes>
          </main>

          {/* Mobile — bottom navigation */}
          <nav
            className="md:hidden fixed inset-x-0 bottom-0 z-40 flex items-stretch justify-around border-t border-gray-800 bg-gray-900/98 backdrop-blur-md pb-safe shadow-[0_-4px_24px_rgba(0,0,0,0.35)]"
            aria-label="Main"
          >
            {NAV_ITEMS.map(item => (
              <NavItemMobile key={item.to} {...item} />
            ))}
          </nav>
        </div>
        </RoomDataProvider>
      </RoomProvider>
    </HashRouter>
  )
}
