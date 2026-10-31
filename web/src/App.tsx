import { Activity as ActivityIcon, CalendarCheck, Check, Circle, Library as LibraryIcon, Lock, Plus, Search, Settings as SettingsIcon, Sparkles, Trash2, Unlock, Video, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api, connectEvents } from './api'
import { Mark } from './components/Mark'
import { dayLabel } from './format'
import { useStore, View } from './store'
import { Chat } from './views/Chat'
import { Activity, Library, Memory, Planner } from './views/Pages'
import { Settings } from './views/Settings'
import { Spotlight } from './views/Spotlight'

export function useConnection() {
  const handleEvent = useStore(state => state.handleEvent)
  const setConnected = useStore(state => state.setConnected)
  const loadState = useStore(state => state.loadState)
  const loadThreads = useStore(state => state.loadThreads)
  useEffect(() => {
    void loadState().catch(() => undefined)
    void loadThreads().catch(() => undefined)
    return connectEvents(handleEvent, setConnected)
  }, [handleEvent, setConnected, loadState, loadThreads])
}

export function useTheme() {
  const theme = useStore(state => state.settings.theme) ?? 'system'
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => {
      const mode = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme
      document.documentElement.dataset.theme = mode
      void api('POST', '/api/shell/theme', { mode }).catch(() => undefined)
    }
    apply()
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [theme])
}

function Toasts() {
  const toasts = useStore(state => state.toasts)
  const dismiss = useStore(state => state.dismissToast)
  return (
    <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
      {toasts.map(toast => (
        <div className={`toast ${toast.tone === 'error' ? 'err' : ''}`} key={toast.id}>
          <div className="toast-body">
            <div className="toast-title">{toast.title}</div>
            <div className="toast-message">{toast.message}</div>
          </div>
          <button className="icon-btn subtle" onClick={() => dismiss(toast.id)} aria-label="Dismiss"><X size={14} /></button>
        </div>
      ))}
    </div>
  )
}

const NAV: { view: View; label: string; icon: typeof LibraryIcon }[] = [
  { view: 'library', label: 'Library', icon: LibraryIcon },
  { view: 'planner', label: 'Planner', icon: CalendarCheck },
  { view: 'memory', label: 'Memory', icon: Sparkles },
  { view: 'activity', label: 'Activity', icon: ActivityIcon },
  { view: 'settings', label: 'Settings', icon: SettingsIcon },
]

function Status() {
  const model = useStore(state => state.model)
  const net = useStore(state => state.net)
  const connected = useStore(state => state.connected)
  const settings = useStore(state => state.settings)
  const recording = useStore(state => state.recording)
  const setView = useStore(state => state.setView)
  let tone = 'ok'
  let text = 'Ready'
  if (!connected) { tone = 'err'; text = 'Reconnecting…' }
  else if (!model?.reachable) { tone = 'warn'; text = 'Starting the model server…' }
  else if (!model.model) { tone = 'warn'; text = 'No model installed' }
  const offline = settings.offline ?? net?.offline ?? true
  return (
    <button className="status" onClick={() => setView('settings')} aria-label={`Status: ${text}. Open settings.`}>
      {recording && <div className="status-line recording"><Video size={12} aria-hidden="true" />Recording the {recording.kind}</div>}
      <div className="status-line"><Circle size={7} fill="currentColor" className={`dot ${tone}`} aria-hidden="true" /><span className="status-model">{model?.model ?? 'No model'}</span><span className="status-state">{text}</span></div>
      <div className="status-line sub">{offline ? <Lock size={11} aria-hidden="true" /> : <Unlock size={11} aria-hidden="true" />}{offline ? 'Offline: nothing leaves this PC' : 'AI on this PC, web access on'}</div>
    </button>
  )
}

function Sidebar() {
  const threads = useStore(state => state.threads)
  const currentId = useStore(state => state.currentId)
  const view = useStore(state => state.view)
  const runs = useStore(state => state.runs)
  const openThread = useStore(state => state.openThread)
  const newThread = useStore(state => state.newThread)
  const deleteThread = useStore(state => state.deleteThread)
  const setView = useStore(state => state.setView)
  const loadThreads = useStore(state => state.loadThreads)
  const [query, setQuery] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)

  // Deleting cannot be undone, so it takes a second click on the same spot.
  const askToDelete = (id: string) => {
    if (confirming === id) {
      setConfirming(null)
      void deleteThread(id)
      return
    }
    setConfirming(id)
    window.setTimeout(() => setConfirming(current => (current === id ? null : current)), 3000)
  }

  useEffect(() => {
    const timer = window.setTimeout(() => void loadThreads(query).catch(() => undefined), query ? 180 : 0)
    return () => window.clearTimeout(timer)
  }, [query, loadThreads])

  const groups = useMemo(() => {
    const output: [string, typeof threads][] = []
    for (const thread of threads) {
      if (thread.title === 'New chat') continue
      const label = dayLabel(thread.updated)
      const last = output[output.length - 1]
      if (last && last[0] === label) last[1].push(thread)
      else output.push([label, [thread]])
    }
    return output
  }, [threads])

  return (
    <aside className="sidebar">
      <div className="brand">
        <Mark size={21} />
        <span className="brand-name">Aegis</span>
        <button className="icon-btn" onClick={newThread} aria-label="New conversation" title="New conversation (Ctrl+N)"><Plus size={17} /></button>
      </div>
      <div className="search">
        <Search size={14} aria-hidden="true" />
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search conversations" aria-label="Search conversations" />
      </div>
      <nav className="threads" aria-label="Conversations">
        {groups.length === 0 && <div className="threads-empty">{query ? 'No matches.' : 'Conversations will appear here.'}</div>}
        {groups.map(([label, entries]) => (
          <div key={label} className="thread-group">
            <div className="thread-label">{label}</div>
            {entries.map(thread => (
              <div className={`thread ${thread.id === currentId && view === 'chat' ? 'active' : ''}`} key={thread.id}>
                <button className="thread-open" onClick={() => void openThread(thread.id)} aria-current={thread.id === currentId && view === 'chat' ? 'page' : undefined}>
                  {runs[thread.id] && <span className="pulse small" aria-label="Running" />}
                  <span className="thread-title">{thread.title}</span>
                </button>
                <button className={`icon-btn subtle thread-delete ${confirming === thread.id ? 'confirm' : ''}`} onClick={() => askToDelete(thread.id)} aria-label={confirming === thread.id ? `Really delete ${thread.title}?` : `Delete ${thread.title}`} title={confirming === thread.id ? 'Click again to delete' : 'Delete'}>{confirming === thread.id ? <Check size={13} /> : <Trash2 size={13} />}</button>
              </div>
            ))}
          </div>
        ))}
      </nav>
      <nav className="side-nav" aria-label="Sections">
        {NAV.map(item => (
          <button key={item.view} className={view === item.view ? 'active' : ''} aria-current={view === item.view ? 'page' : undefined} onClick={() => setView(item.view)}>
            <item.icon size={15} aria-hidden="true" />{item.label}
          </button>
        ))}
      </nav>
      <Status />
    </aside>
  )
}

export function App() {
  useConnection()
  useTheme()
  const view = useStore(state => state.view)
  const newThread = useStore(state => state.newThread)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault()
        newThread()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [newThread])

  return (
    <div className="app">
      <Sidebar />
      <main className="main">
        {view === 'chat' && <Chat />}
        {view === 'library' && <Library />}
        {view === 'planner' && <Planner />}
        {view === 'memory' && <Memory />}
        {view === 'activity' && <Activity />}
        {view === 'settings' && <Settings />}
      </main>
      <Toasts />
    </div>
  )
}

export function SpotlightApp() {
  useConnection()
  useTheme()
  return <Spotlight />
}
