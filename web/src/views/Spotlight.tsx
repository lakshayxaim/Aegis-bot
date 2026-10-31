import { ArrowUpRight, Check, GripVertical, Square, TriangleAlert, X } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from '../api'
import { ApprovalCard, QuestionCard } from '../components/Ask'
import { Composer } from '../components/Composer'
import { Mark } from '../components/Mark'
import { Markdown } from '../components/Markdown'
import { FileCard } from '../components/Receipt'
import { useStore } from '../store'

const REUSE_WINDOW_MS = 10 * 60 * 1000
const WIDTH = { bar: 600, pill: 380, card: 500 }
type Mode = keyof typeof WIDTH
const shell = (body: Record<string, unknown>) => void api('POST', '/api/shell/spotlight', body).catch(() => undefined)

declare global {
  interface Window { __aegisSpotlight?: (options: { finish?: boolean; listen?: boolean; focus?: boolean; blur?: boolean; watch?: string; mainActive?: boolean }) => void }
}

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const seconds = Math.max(0, Math.round((now - since) / 1000))
  return <span className="pill-time">{seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`}</span>
}

export function Spotlight() {
  const send = useStore(state => state.send)
  const stop = useStore(state => state.stop)
  const toast = useStore(state => state.toast)
  const opacity = useStore(state => state.settings.overlay_opacity) ?? 92
  const [threadId, setThreadId] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('bar')
  // The overlay was opened to watch something the main window started, not by the user asking here.
  const [watching, setWatching] = useState(false)
  const lastUsed = useRef(0)
  const panel = useRef<HTMLDivElement>(null)
  const hideTimer = useRef<number | null>(null)
  const items = useStore(state => (threadId ? state.items[threadId] : undefined)) ?? []
  const draft = useStore(state => (threadId ? state.drafts[threadId] : undefined))
  const statusText = useStore(state => (threadId ? state.statusText[threadId] : undefined))
  const running = useStore(state => (threadId ? Boolean(state.runs[threadId]) : false))
  const started = useStore(state => (threadId ? state.runStarted[threadId] : undefined))
  // Toasts have nowhere to appear in this window, so the latest one shows inline.
  const notice = useStore(state => state.toasts[state.toasts.length - 1])
  const [seed, setSeed] = useState(0)

  const lastUser = items.map(item => item.role).lastIndexOf('user')
  const recent = lastUser >= 0 ? items.slice(lastUser + 1) : []
  const tools = recent.filter(item => item.role === 'tool' && !['ask_user', 'more_tools'].includes(item.data?.name))
  const reply = [...recent].reverse().find(item => item.role === 'assistant')
  const pending = tools.find(item => item.data?.status === 'awaiting_approval' && item.data?.approval)
  const question = reply?.data?.status === 'question' ? reply : undefined
  const needsYou = Boolean(pending || question)
  const files = tools.flatMap(item => (item.data?.undone ? [] : item.data?.files ?? []))
  const failed = reply && ['failed', 'error', 'partial'].includes(reply.data?.status)

  const clearHide = () => {
    if (hideTimer.current != null) window.clearTimeout(hideTimer.current)
    hideTimer.current = null
  }
  const hide = useCallback(() => {
    clearHide()
    setWatching(false)
    setMode('bar')
    shell({ action: 'hide' })
  }, [])

  useEffect(() => {
    document.documentElement.classList.add('spotlight-mode')
  }, [])

  useEffect(() => {
    document.documentElement.style.setProperty('--glass', `${opacity}%`)
    // Something to read needs a steadier background than a status line does.
    document.documentElement.style.setProperty('--glass-card', `${Math.min(100, opacity + 6)}%`)
  }, [opacity])

  const live = useRef({ running, needsYou, mode, watching, threadId })
  live.current = { running, needsYou, mode, watching, threadId }

  useEffect(() => {
    window.__aegisSpotlight = ({ finish, listen, focus, blur, watch, mainActive }) => {
      const state = useStore.getState()
      const now = live.current
      if (watch) {
        clearHide()
        setWatching(true)
        setThreadId(watch)
        setMode('pill')
        void state.openThread(watch)
        return
      }
      if (mainActive) {
        if (now.watching && !now.needsYou) hide()
        return
      }
      if (blur) {
        if (now.mode === 'bar' && now.running) setMode('pill')
        return
      }
      if (finish) {
        void state.stopVoice()
        return
      }
      clearHide()
      if (focus && (now.needsYou || now.running)) {
        setMode('card')
        return
      }
      setWatching(false)
      if (Date.now() - lastUsed.current > REUSE_WINDOW_MS) {
        setThreadId(null)
        useStore.setState({ currentId: null })
      }
      setMode('bar')
      setSeed(value => value + 1)
      if (listen) void state.startVoice()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (live.current.running || live.current.needsYou) setMode('pill')
      else hide()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [hide])

  // Stays up when focus moves on, for as long as there is something worth showing.
  useEffect(() => {
    shell({ action: mode === 'bar' && !running ? 'unpin' : 'pin' })
  }, [mode, running])

  // Hands the keyboard back to whatever the user was doing, and takes it only for typing.
  const previousMode = useRef<Mode>('bar')
  useEffect(() => {
    if (mode === previousMode.current) return
    previousMode.current = mode
    if (mode === 'pill') shell({ action: 'release' })
  }, [mode])

  useLayoutEffect(() => {
    const element = panel.current
    if (!element) return
    const report = () => shell({ action: 'resize', height: Math.ceil(element.getBoundingClientRect().height), width: WIDTH[live.current.mode] })
    const observer = new ResizeObserver(report)
    observer.observe(element)
    report()
    return () => observer.disconnect()
  }, [mode])

  // Anything waiting on the user opens up by itself, without taking the keyboard from what they are doing.
  useEffect(() => {
    if (needsYou) {
      clearHide()
      setMode('card')
    }
  }, [needsYou, pending?.id, question?.id])

  // What happens when the work ends: a short result is a line that fades, a long one a card to read.
  const lastReply = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (running || !reply || needsYou || reply.id === lastReply.current) return
    lastReply.current = reply.id
    if (mode === 'bar') return
    const short = reply.text.length <= 70 && !files.length && !failed
    setMode(short ? 'pill' : 'card')
    clearHide()
    hideTimer.current = window.setTimeout(hide, short ? 5000 : 15000)
  }, [running, reply, needsYou, mode, files.length, failed, hide])

  const dispatch = useCallback((text: string) => {
    lastUsed.current = Date.now()
    clearHide()
    setWatching(false)
    setMode('pill')
    void send(text, live.current.threadId).then(id => setThreadId(id)).catch(error => {
      setMode('bar')
      toast('Could not send that', error.message, 'error')
    })
  }, [send, toast])

  const answer = useCallback((text: string) => {
    dispatch(text)
  }, [dispatch])

  const keepOpen = () => clearHide()
  const openInMain = () => {
    if (threadId) void useStore.getState().openThread(threadId)
    setMode('bar')
    shell({ action: 'open_main' })
  }
  const grip = <span className="grip" title="Drag to move" onMouseDown={() => shell({ action: 'drag' })}><GripVertical size={13} aria-hidden="true" /></span>

  if (mode === 'bar') {
    return (
      <div className="spotlight glass bar" ref={panel}>
        <div className="spotlight-input">
          {grip}
          <Mark size={18} />
          <Composer key={seed} threadId={threadId} onSend={dispatch} placeholder="Ask Aegis…" autoFocus compact />
        </div>
        {notice && <div className={`spotlight-notice ${notice.tone === 'error' ? 'err' : ''}`} role="status"><strong>{notice.title}.</strong> {notice.message}</div>}
      </div>
    )
  }

  const active = [...tools].reverse().find(item => ['running', 'awaiting_approval'].includes(item.data?.status))
  const doing: string = active?.data?.progress || active?.data?.doing || statusText || 'Working…'
  const done = tools.filter(item => ['ok', 'unverified'].includes(item.data?.status)).length

  if (mode === 'pill') {
    const finished = !running && reply
    return (
      <div className={`spotlight glass pill ${finished ? 'finished' : ''}`} ref={panel} onMouseEnter={keepOpen} onClick={() => setMode('card')} role="status">
        {grip}
        {finished
          ? failed ? <TriangleAlert size={15} className="glyph warn" aria-hidden="true" /> : <Check size={15} className="glyph ok" aria-hidden="true" />
          : <span className="pulse" aria-hidden="true" />}
        <span className="pill-text">{finished ? reply.text : doing}</span>
        {!finished && done > 0 && <span className="pill-count">{done} done</span>}
        {!finished && started && <Elapsed since={started * 1000} />}
        {running && threadId
          ? <button className="pill-btn" onClick={event => { event.stopPropagation(); void stop(threadId) }} aria-label="Stop" title="Stop"><Square size={10} fill="currentColor" /></button>
          : <button className="pill-btn" onClick={event => { event.stopPropagation(); hide() }} aria-label="Close" title="Close"><X size={13} /></button>}
      </div>
    )
  }

  return (
    <div className="spotlight glass card" ref={panel} onMouseEnter={keepOpen} onMouseDown={() => shell({ action: 'hold' })}>
      <div className="card-head">
        {grip}
        {lastUser >= 0 && <span className="spotlight-said" title={items[lastUser].text}>{items[lastUser].text}</span>}
        {running && !pending && <span className="card-doing"><span className="pulse small" aria-hidden="true" />{doing}</span>}
        <button className="pill-btn" onClick={() => (running || needsYou ? setMode('pill') : hide())} aria-label="Make smaller" title="Make smaller (Esc)"><X size={13} /></button>
      </div>
      <div className="spotlight-output">
        {pending && <ApprovalCard key={pending.id} item={pending} />}
        {question && <QuestionCard item={question} onAnswer={answer} active />}
        {!pending && !question && tools.length > 0 && (
          <ol className="steps">
            {tools.slice(-5).map(item => (
              <li key={item.id} className={`step status-${item.data?.status}`}>
                {item.data?.status === 'running' ? <span className="pulse small" aria-hidden="true" /> : ['ok', 'unverified'].includes(item.data?.status) ? <Check size={12} className="glyph ok" aria-hidden="true" /> : <X size={12} className="glyph err" aria-hidden="true" />}
                <span>{item.data?.status === 'running' ? item.data?.progress || item.data?.doing : item.text}</span>
              </li>
            ))}
          </ol>
        )}
        {draft && <div className="turn-assistant streaming"><Markdown text={draft} /><span className="caret" aria-hidden="true" /></div>}
        {reply && !draft && !question && <div className="turn-assistant"><Markdown text={reply.text} /></div>}
        {!running && files.length > 0 && <div className="receipt-files">{files.slice(-3).map((file: any) => <FileCard key={file.id} file={file} />)}</div>}
      </div>
      {question && (
        <div className="card-answer">
          <Composer key={question.id} threadId={threadId} onSend={answer} placeholder="Your answer…" compact />
        </div>
      )}
      {threadId && !running && !question && <button className="link-btn card-open" onClick={openInMain}>Open in Aegis<ArrowUpRight size={13} aria-hidden="true" /></button>}
    </div>
  )
}
