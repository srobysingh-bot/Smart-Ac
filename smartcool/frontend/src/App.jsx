import { HashRouter, NavLink, Route, Routes } from 'react-router-dom'
import {
  LayoutDashboard,
  History,
  BarChart2,
  Settings,
  Wind,
} from 'lucide-react'
import Dashboard      from './pages/Dashboard.jsx'
import SessionHistory from './pages/SessionHistory.jsx'
import Analytics      from './pages/Analytics.jsx'
import SettingsPage   from './pages/Settings.jsx'

const NAV_ITEMS = [
  { to: '/',          icon: LayoutDashboard, label: 'Dashboard'   },
  { to: '/history',   icon: History,         label: 'Sessions'    },
  { to: '/analytics', icon: BarChart2,        label: 'Analytics'   },
  { to: '/settings',  icon: Settings,         label: 'Settings'    },
]

function NavItem({ to, icon: Icon, label }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors
         ${isActive
           ? 'bg-blue-600 text-white'
           : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100'
         }`
      }
    >
      <Icon size={18} />
      <span>{label}</span>
    </NavLink>
  )
}

export default function App() {
  return (
    <HashRouter>
      <div className="flex h-screen overflow-hidden">
        {/* Sidebar */}
        <aside className="w-56 shrink-0 flex flex-col bg-gray-900 border-r border-gray-800 p-4 gap-1">
          {/* Brand */}
          <div className="flex items-center gap-2 mb-6 px-1">
            <Wind size={22} className="text-blue-400" />
            <span className="font-bold text-lg text-white">SmartCool</span>
          </div>

          {NAV_ITEMS.map(item => (
            <NavItem key={item.to} {...item} />
          ))}

          <div className="mt-auto text-xs text-gray-600 px-1 pt-4">
            v1.0.4 · All data local
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 overflow-y-auto bg-gray-950">
          <Routes>
            <Route path="/"          element={<Dashboard />} />
            <Route path="/history"   element={<SessionHistory />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/settings"  element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
  )
}
