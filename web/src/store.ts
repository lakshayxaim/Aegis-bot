import { create } from 'zustand'
import { api } from './api'

export interface Thread { id: string; title: string; created: number; updated: number; source?: string }
export interface Item { id: string; thread_id: string; seq: number; role: 'user' | 'assistant' | 'tool'; text: string; data: any; ts: number }
export interface Toast { id: number; title: string; message: string; tone?: 'info' | 'error' }
export type View = 'chat' | 'library' | 'planner' | 'memory' | 'activity' | 'settings'
// The main window and the overlay are separate pages hearing the same events. Each acts only on its own dictation.
export const PAGE = window.location.hash.startsWith('#/spotlight') ? 'overlay' : 'main'

export interface SpeechEngine { id: string; label: string; detail: string; size_mb: number; installed: boolean }
export interface SpeechState {
  ready: boolean
  active: string | null
  chosen: string
  engines: SpeechEngine[]
  download: { id: string; percent: number; status: string } | null
}

export interface ModelState {
  reachable: boolean
  server: string
  ollama_installed: boolean
  model: string | null
  loaded: boolean
  gpu_percent: number | null
  force_cpu: boolean
  note: string
  installed: { name: string; size: number; capabilities: string[] }[]
  recommended: string
}

interface State {
  connected: boolean
  threads: Thread[]
  currentId: string | null
  items: Record<string, Item[]>
  drafts: Record<string, string>
  statusText: Record<string, string>
  runs: Record<string, string>
  runStarted: Record<string, number>
  view: View
  settings: Record<string, any>
  model: ModelState | null
  net: { offline: boolean; blocked_total: number } | null
  folders: Record<string, string>
  toasts: Toast[]
  voice: { state: string; level: number; partial: string; owner: string | null }
  speech: SpeechState | null
  recording: { kind: string; seconds: number | null; path: string } | null
  pull: Record<string, { status: string; completed?: number; total?: number }>
  indexProgress: string
  revision: Record<string, number>
  voiceResult: { text: string; seq: number; confident: boolean } | null
  hotkeys: { spotlight: string | null; voice: string | null } | null

  setView: (view: View) => void
  loadState: () => Promise<void>
  loadThreads: (query?: string) => Promise<void>
  openThread: (id: string) => Promise<void>
  newThread: () => void
  send: (text: string, threadId?: string | null) => Promise<string>
  stop: (threadId: string) => Promise<void>
  decide: (itemId: string, decision: 'once' | 'task' | 'always' | 'deny') => Promise<void>
  undo: (undoId: string) => Promise<void>
  deleteThread: (id: string) => Promise<void>
  toast: (title: string, message: string, tone?: 'info' | 'error') => void
  dismissToast: (id: number) => void
  saveSettings: (patch: Record<string, any>) => Promise<void>
  startVoice: () => Promise<void>
  stopVoice: () => Promise<void>
  cancelVoice: () => Promise<void>
  loadSpeech: () => Promise<void>
  handleEvent: (event: any) => void
  setConnected: (connected: boolean) => void
}

let toastId = 0
let voiceSeq = 0

function upsert(list: Item[] | undefined, item: Item): Item[] {
  const current = list ?? []
  const index = current.findIndex(entry => entry.id === item.id)
  if (index >= 0) {
    const next = current.slice()
    next[index] = item
    return next
  }
  return [...current, item].sort((a, b) => a.seq - b.seq)
}

export const useStore = create<State>((set, get) => ({
  connected: false,
  threads: [],
  currentId: null,
  items: {},
  drafts: {},
  statusText: {},
  runs: {},
  runStarted: {},
  view: 'chat',
  settings: {},
  model: null,
  net: null,
  folders: {},
  toasts: [],
  voice: { state: 'idle', level: 0, partial: '', owner: null },
  speech: null,
  recording: null,
  pull: {},
  indexProgress: '',
  revision: {},
  voiceResult: null,
  hotkeys: null,

  setView: view => set({ view }),
  setConnected: connected => set({ connected }),

  loadState: async () => {
    const state = await api('GET', '/api/state')
    const runs: Record<string, string> = {}
    const runStarted: Record<string, number> = {}
    for (const run of state.active_runs) {
      runs[run.thread_id] = run.id
      runStarted[run.thread_id] = run.started
    }
    set({ runStarted, settings: state.settings, model: state.model, net: state.net, folders: state.folders, recording: state.recording, hotkeys: state.hotkeys, speech: state.speech, runs })
  },

  loadThreads: async (query = '') => {
    const { threads } = await api('GET', `/api/threads?q=${encodeURIComponent(query)}`)
    set({ threads })
  },

  openThread: async id => {
    set({ currentId: id, view: 'chat' })
    const { items, running } = await api('GET', `/api/threads/${id}`)
    set(state => {
      const runs = { ...state.runs }
      const runStarted = { ...state.runStarted }
      if (running.length) {
        runs[id] = running[0].id
        runStarted[id] = running[0].started
      } else delete runs[id]
      return { items: { ...state.items, [id]: items }, runs, runStarted }
    })
  },

  newThread: () => set({ currentId: null, view: 'chat' }),

  send: async (text, threadId) => {
    let id = threadId ?? get().currentId
    if (!id) {
      const thread: Thread = await api('POST', '/api/threads', {})
      id = thread.id
      set(state => ({ threads: [thread, ...state.threads], currentId: thread.id, items: { ...state.items, [thread.id]: [] } }))
    }
    const target = id
    const { run_id } = await api('POST', `/api/threads/${target}/messages`, { text })
    set(state => ({ runs: { ...state.runs, [target]: run_id } }))
    return target
  },

  stop: async threadId => {
    const run = get().runs[threadId]
    if (run) await api('POST', `/api/runs/${run}/cancel`, {})
  },

  decide: async (itemId, decision) => {
    try {
      await api('POST', `/api/approvals/${itemId}`, { decision })
    } catch (error) {
      get().toast('Could not send your answer', (error as Error).message, 'error')
    }
  },

  undo: async undoId => {
    try {
      const { message } = await api('POST', `/api/undo/${undoId}`, {})
      get().toast('Undone', message)
    } catch (error) {
      get().toast('Could not undo', (error as Error).message, 'error')
    }
  },

  deleteThread: async id => {
    try {
      await api('DELETE', `/api/threads/${id}`)
    } catch (error) {
      get().toast('Could not delete', (error as Error).message, 'error')
    }
  },

  toast: (title, message, tone = 'info') => {
    const id = ++toastId
    set(state => ({ toasts: [...state.toasts.slice(-3), { id, title, message, tone }] }))
    window.setTimeout(() => get().dismissToast(id), tone === 'error' ? 9000 : 6500)
  },

  dismissToast: id => set(state => ({ toasts: state.toasts.filter(toast => toast.id !== id) })),

  saveSettings: async patch => {
    try {
      const settings = await api('PATCH', '/api/settings', patch)
      set({ settings })
    } catch (error) {
      get().toast('Could not save', (error as Error).message, 'error')
    }
  },

  startVoice: async () => {
    try {
      set(state => ({ voice: { ...state.voice, state: 'listening', owner: PAGE, partial: '' } }))
      await api('POST', '/api/voice/start', { owner: PAGE })
    } catch (error) {
      set(state => ({ voice: { ...state.voice, state: 'idle', owner: null } }))
      get().toast('Voice is unavailable', (error as Error).message, 'error')
    }
  },

  stopVoice: async () => {
    await api('POST', '/api/voice/stop', {}).catch(() => undefined)
  },

  cancelVoice: async () => {
    await api('POST', '/api/voice/cancel', {}).catch(() => undefined)
  },

  loadSpeech: async () => {
    set({ speech: await api('GET', '/api/speech') })
  },

  handleEvent: event => {
    const bump = (key: string) => set(state => ({ revision: { ...state.revision, [key]: (state.revision[key] ?? 0) + 1 } }))
    const threadId: string | undefined = event.thread_id
    switch (event.type) {
      case 'hello': {
        void get().loadState()
        const current = get().currentId
        if (current) void get().openThread(current)
        break
      }
      case 'item_added':
      case 'tool_started':
      case 'tool_finished':
      case 'approval_required': {
        const item: Item = event.item
        set(state => {
          const drafts = { ...state.drafts }
          const statusText = { ...state.statusText }
          if (item.role === 'assistant') {
            delete drafts[item.thread_id]
            delete statusText[item.thread_id]
          }
          return { items: { ...state.items, [item.thread_id]: upsert(state.items[item.thread_id], item) }, drafts, statusText }
        })
        break
      }
      case 'tool_progress': {
        set(state => {
          const list = state.items[event.thread_id]
          if (!list) return {}
          return {
            items: {
              ...state.items,
              [event.thread_id]: list.map(item => (item.id === event.item_id ? { ...item, data: { ...item.data, progress: event.text, status: event.status ?? item.data.status, approval: event.status ? null : item.data.approval } } : item)),
            },
          }
        })
        break
      }
      case 'token':
        set(state => {
          const statusText = { ...state.statusText }
          delete statusText[event.thread_id]
          return { drafts: { ...state.drafts, [event.thread_id]: (state.drafts[event.thread_id] ?? '') + event.text }, statusText }
        })
        break
      case 'draft_clear':
        set(state => {
          const drafts = { ...state.drafts }
          delete drafts[event.thread_id]
          return { drafts }
        })
        break
      case 'status':
        set(state => ({ statusText: { ...state.statusText, [event.thread_id]: event.text } }))
        break
      case 'run_started':
        set(state => ({ runs: { ...state.runs, [event.thread_id]: event.run_id }, runStarted: { ...state.runStarted, [event.thread_id]: event.started ?? Date.now() / 1000 } }))
        break
      case 'run_finished':
        set(state => {
          const runs = { ...state.runs }
          const statusText = { ...state.statusText }
          const drafts = { ...state.drafts }
          if (threadId) {
            delete runs[threadId]
            delete statusText[threadId]
            delete drafts[threadId]
          }
          return { runs, statusText, drafts }
        })
        void get().loadThreads()
        break
      case 'thread_updated':
        set(state => ({ threads: state.threads.map(thread => (thread.id === event.thread_id ? { ...thread, title: event.title } : thread)) }))
        break
      case 'thread_created':
        set(state => ({ threads: [event.thread, ...state.threads] }))
        break
      case 'thread_deleted':
        set(state => {
          const items = { ...state.items }
          delete items[event.thread_id]
          return { threads: state.threads.filter(thread => thread.id !== event.thread_id), items, currentId: state.currentId === event.thread_id ? null : state.currentId }
        })
        break
      case 'notification':
        get().toast(event.title, event.message)
        break
      case 'voice_state':
        set(state => ({
          voice: {
            state: event.state,
            owner: event.state === 'idle' ? null : event.owner ?? state.voice.owner,
            level: event.state === 'listening' ? state.voice.level : 0,
            partial: event.state === 'idle' ? '' : state.voice.partial,
          },
        }))
        break
      case 'voice_level':
        set(state => ({ voice: { ...state.voice, level: event.level } }))
        break
      case 'voice_partial':
        if (event.owner === PAGE) set(state => ({ voice: { ...state.voice, partial: event.text } }))
        break
      case 'voice_result':
        if (event.owner !== PAGE) break
        if (event.text) set({ voiceResult: { text: event.text, seq: ++voiceSeq, confident: Boolean(event.confident) } })
        else if (event.message) get().toast('Voice', event.message)
        break
      case 'speech_changed':
        void get().loadSpeech()
        break
      case 'speech_download':
        set(state => (state.speech ? { speech: { ...state.speech, download: event.done ? null : { id: event.id, percent: event.percent, status: event.status } } } : {}))
        if (event.error) get().toast('Speech model download failed', event.status, 'error')
        if (event.done) void get().loadSpeech()
        break
      case 'recording':
        set({ recording: event.active ? { kind: event.kind, seconds: event.seconds, path: event.path } : null })
        break
      case 'model_status':
        void get().loadState()
        break
      case 'settings_changed':
        set({ settings: event.settings })
        void get().loadState()
        break
      case 'model_pull':
        set(state => {
          const pull = { ...state.pull }
          if (event.done) delete pull[event.model]
          else pull[event.model] = { status: event.status, completed: event.completed, total: event.total }
          return { pull }
        })
        if (event.error) get().toast('Model download failed', event.status, 'error')
        break
      case 'index_progress':
        set({ indexProgress: event.active ? event.text : '' })
        if (!event.active) {
          get().toast('Documents', event.text)
          bump('documents')
        }
        break
      case 'reminders_changed':
        bump('reminders')
        break
      case 'todos_changed':
        bump('todos')
        break
      case 'library_changed':
        bump('library')
        break
      default:
        break
    }
  },
}))

/** This page's dictation, ignoring one running in the other window. */
export function useMyVoice() {
  const voice = useStore(state => state.voice)
  const mine = voice.owner === PAGE
  return {
    listening: mine && voice.state === 'listening',
    transcribing: mine && voice.state === 'transcribing',
    partial: mine ? voice.partial : '',
    level: mine ? voice.level : 0,
  }
}
