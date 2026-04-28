import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import SearchPage from './pages/Search'
import BrowsePage from './pages/Browse'
import IngestPage from './pages/Ingest'
import StatsPage from './pages/Stats'
import GraphPage from './pages/Graph'

const navItems = [
  { to: '/search', label: 'Search' },
  { to: '/browse', label: 'Browse' },
  { to: '/graph', label: 'Graph' },
  { to: '/ingest', label: 'Ingest' },
  { to: '/stats', label: 'Stats' },
]

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-10 bg-bg border-b border-border">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <h1 className="text-lg font-semibold text-text">Memory Vault</h1>
          <nav className="flex gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `px-4 py-2 rounded-md text-sm border transition-colors ${
                    isActive
                      ? 'bg-bg3 text-text border-accent'
                      : 'bg-bg2 text-text2 border-border hover:text-text hover:border-text2'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/search" replace />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/browse" element={<BrowsePage />} />
          <Route path="/graph" element={<GraphPage />} />
          <Route path="/ingest" element={<IngestPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="*" element={<Navigate to="/search" replace />} />
        </Routes>
      </main>
    </div>
  )
}
