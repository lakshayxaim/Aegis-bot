import { CalendarClock, Download, FolderTree, Gauge, Image as ImageIcon, MonitorSmartphone } from 'lucide-react'
import { api } from '../api'
import { Composer } from '../components/Composer'
import { Timeline } from '../components/Timeline'
import { formatBytes, greeting } from '../format'
import { useStore } from '../store'

const STARTERS = [
  { icon: ImageIcon, text: 'Take a screenshot', prompt: 'Take a screenshot' },
  { icon: Gauge, text: "What's using my memory?", prompt: 'What is using the most memory on my PC?' },
  { icon: Download, text: 'Show my newest downloads', prompt: 'List the 5 newest files in my Downloads folder' },
  { icon: CalendarClock, text: 'Remind me to stretch in 30 minutes', prompt: 'Remind me to stretch in 30 minutes' },
  { icon: FolderTree, text: 'Tidy my Downloads folder', prompt: 'Organise my Downloads folder by file type' },
  { icon: MonitorSmartphone, text: "What's on my screen?", prompt: 'What is on my screen right now?' },
]

export function ModelSetup() {
  const model = useStore(state => state.model)
  const pull = useStore(state => state.pull)
  const toast = useStore(state => state.toast)
  if (!model || model.model) return null
  if (!model.ollama_installed) {
    return (
      <div className="setup-card">
        <h3>Aegis needs Ollama to think</h3>
        <p>Ollama runs the language model on this PC. Install it once, from ollama.com, then reopen Aegis. The model runs here, on this PC.</p>
      </div>
    )
  }
  if (!model.reachable) {
    return (
      <div className="setup-card">
        <h3>Starting the local model server…</h3>
        <p>If this message stays, reopen Aegis.</p>
      </div>
    )
  }
  const name = model.recommended
  const progress = pull[name]
  const percent = progress?.total ? Math.round(((progress.completed ?? 0) / progress.total) * 100) : null
  const start = () => void api('POST', '/api/models/pull', { model: name }).catch(error => toast('Could not start the download', error.message, 'error'))
  return (
    <div className="setup-card">
      <h3>One download, and the AI runs on this PC</h3>
      <p>Aegis needs a language model on this PC. <strong>{name}</strong> is about 6.6 GB, and it did the best on real work when tested on a laptop like this. Ollama fetches it directly from its library.</p>
      {progress ? (
        <div className="progress" role="progressbar" aria-valuenow={percent ?? undefined} aria-valuemin={0} aria-valuemax={100} aria-label="Download progress">
          <div className="progress-bar"><span style={{ width: `${percent ?? 4}%` }} /></div>
          <div className="progress-text">{progress.status}{progress.total ? ` · ${formatBytes(progress.completed)} of ${formatBytes(progress.total)}` : ''}</div>
        </div>
      ) : (
        <button className="btn primary" onClick={start}>Download {name}</button>
      )}
    </div>
  )
}

export function Chat() {
  const currentId = useStore(state => state.currentId)
  const send = useStore(state => state.send)
  const toast = useStore(state => state.toast)
  const name = useStore(state => state.settings.user_name)
  const hotkey = useStore(state => state.hotkeys?.spotlight)
  const dispatch = (text: string) => void send(text).catch(error => toast('Could not send that', error.message, 'error'))

  if (!currentId) {
    return (
      <div className="home">
        <div className="home-inner">
          <h1 className="greeting">{greeting()}{name ? `, ${name}` : ''}.</h1>
          <p className="home-sub">Tell me what you want done, here or on the web. I'll do it, and show you the proof.</p>
          <ModelSetup />
          <Composer threadId={null} onSend={dispatch} autoFocus />
          <ul className="starters">
            {STARTERS.map(starter => (
              <li key={starter.text}>
                <button className="starter" onClick={() => dispatch(starter.prompt)}>
                  <starter.icon size={15} aria-hidden="true" />
                  <span>{starter.text}</span>
                </button>
              </li>
            ))}
          </ul>
          {hotkey && <p className="home-hint">Press {hotkey.split('+').map(key => <kbd key={key}>{key}</kbd>)} anywhere and just talk, or start typing. In this box, press <kbd>Space</kbd> to talk.</p>}
        </div>
      </div>
    )
  }

  return (
    <div className="chat">
      <Timeline threadId={currentId} onAnswer={dispatch} />
      <div className="chat-footer">
        <div className="chat-footer-inner">
          <ModelSetup />
          <Composer threadId={currentId} onSend={dispatch} autoFocus />
        </div>
      </div>
    </div>
  )
}
