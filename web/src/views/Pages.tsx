import { Check, FileText, FolderPlus, Plus, ShieldCheck, ShieldAlert, Trash2 } from 'lucide-react'
import { FormEvent, ReactNode, useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { FileCard } from '../components/Receipt'
import { formatBytes, shortPath, when } from '../format'
import { useStore } from '../store'

export function Page({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  return (
    <div className="page">
      <div className="page-inner">
        <h1 className="page-title">{title}</h1>
        {intro && <p className="page-intro">{intro}</p>}
        {children}
      </div>
    </div>
  )
}

function useLoad<T>(path: string, revisionKey: string, fallback: T): [T, () => Promise<void>] {
  const [value, setValue] = useState<T>(fallback)
  const toast = useStore(state => state.toast)
  const revision = useStore(state => state.revision[revisionKey] ?? 0)
  const load = useCallback(async () => {
    try {
      setValue(await api<T>('GET', path))
    } catch (error) {
      toast('Could not load this page', (error as Error).message, 'error')
    }
  }, [path, toast])
  useEffect(() => {
    void load()
  }, [load, revision])
  return [value, load]
}

export function Library() {
  const [data] = useLoad<{ artifacts: any[] }>('/api/library', 'library', { artifacts: [] })
  const [filter, setFilter] = useState('all')
  const kinds = [['all', 'Everything'], ['image', 'Images'], ['document', 'Documents'], ['video', 'Video'], ['audio', 'Audio']] as const
  const shown = data.artifacts.filter(item => item.exists).filter(item => filter === 'all' || item.kind === filter || (filter === 'document' && ['pdf', 'text', 'spreadsheet', 'presentation', 'document'].includes(item.kind)))
  return (
    <Page title="Library" intro="Every file Aegis has made for you, wherever it was saved.">
      <div className="segmented" role="tablist" aria-label="Filter by type">
        {kinds.map(([key, label]) => <button key={key} role="tab" aria-selected={filter === key} className={filter === key ? 'active' : ''} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      {shown.length === 0 ? <div className="empty">Nothing here yet. Screenshots, charts, documents and recordings will collect here as you ask for them.</div> : <div className="library-grid">{shown.map(item => <FileCard key={item.id} file={item} />)}</div>}
    </Page>
  )
}

export function Planner() {
  const [todos, loadTodos] = useLoad<{ todos: any[] }>('/api/todos', 'todos', { todos: [] })
  const [reminders] = useLoad<{ reminders: any[] }>('/api/reminders', 'reminders', { reminders: [] })
  const [text, setText] = useState('')
  const toast = useStore(state => state.toast)
  const add = async (event: FormEvent) => {
    event.preventDefault()
    const value = text.trim()
    if (!value) return
    setText('')
    await api('POST', '/api/todos', { text: value }).catch(error => toast('Could not add that', error.message, 'error'))
    void loadTodos()
  }
  const upcoming = reminders.reminders.filter(item => item.status === 'pending')
  const past = reminders.reminders.filter(item => item.status !== 'pending').slice(0, 12)
  return (
    <Page title="Planner" intro="To-dos and reminders. Ask for them in a sentence, or manage them here.">
      <div className="split">
        <section>
          <h2 className="section-title">To-do</h2>
          <form className="inline-form" onSubmit={event => void add(event)}>
            <input value={text} onChange={event => setText(event.target.value)} placeholder="Add a to-do" aria-label="New to-do" />
            <button className="btn" disabled={!text.trim()}><Plus size={14} aria-hidden="true" />Add</button>
          </form>
          {todos.todos.length === 0 ? <div className="empty">Nothing to do. Try saying "add call the dentist to my list".</div> : (
            <ul className="rows">
              {todos.todos.map(todo => (
                <li className={`row todo ${todo.done ? 'done' : ''}`} key={todo.id}>
                  <button className={`check ${todo.done ? 'on' : ''}`} role="checkbox" aria-checked={Boolean(todo.done)} aria-label={todo.text} onClick={() => void api('POST', `/api/todos/${todo.id}/toggle`, {})}>{todo.done ? <Check size={12} /> : null}</button>
                  <span className="row-main">{todo.text}</span>
                  <button className="icon-btn subtle" aria-label={`Delete ${todo.text}`} onClick={() => void api('DELETE', `/api/todos/${todo.id}`)}><Trash2 size={14} /></button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section>
          <h2 className="section-title">Reminders</h2>
          {upcoming.length === 0 ? <div className="empty">Nothing coming up. Try "remind me at 5pm to call Sam".</div> : (
            <ul className="rows">
              {upcoming.map(item => (
                <li className="row" key={item.id}>
                  <div className="row-main">
                    <div>{item.message}</div>
                    <div className="row-sub">{when(item.due)}{item.repeat_seconds ? `, then every ${Math.round(item.repeat_seconds / 60)} min` : ''}{item.kind === 'run' ? ' · Aegis will do this itself' : ''}</div>
                  </div>
                  <button className="btn ghost small" onClick={() => void api('DELETE', `/api/reminders/${item.id}`)}>Cancel</button>
                </li>
              ))}
            </ul>
          )}
          {past.length > 0 && (
            <>
              <h3 className="section-sub">Earlier</h3>
              <ul className="rows quiet">
                {past.map(item => (
                  <li className="row" key={item.id}>
                    <div className="row-main">
                      <div>{item.message}</div>
                      <div className="row-sub">{item.status} · {when(item.fired_at ?? item.due)}</div>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
    </Page>
  )
}

export function Memory() {
  const [facts, loadFacts] = useLoad<{ facts: any[] }>('/api/memory', 'memory', { facts: [] })
  const [docs, loadDocs] = useLoad<{ folders: any[]; indexing: boolean }>('/api/documents', 'documents', { folders: [], indexing: false })
  const [fact, setFact] = useState('')
  const [folder, setFolder] = useState('')
  const toast = useStore(state => state.toast)
  const indexProgress = useStore(state => state.indexProgress)
  const folders = useStore(state => state.folders)
  const addFact = async (event: FormEvent) => {
    event.preventDefault()
    const value = fact.trim()
    if (!value) return
    setFact('')
    await api('POST', '/api/memory', { fact: value }).catch(error => toast('Could not save that', error.message, 'error'))
    void loadFacts()
  }
  const addFolder = async (event: FormEvent) => {
    event.preventDefault()
    const value = folder.trim()
    if (!value) return
    try {
      await api('POST', '/api/documents', { path: value })
      setFolder('')
      void loadDocs()
    } catch (error) {
      toast('Could not add that folder', (error as Error).message, 'error')
    }
  }
  return (
    <Page title="Memory" intro="What Aegis knows about you and your files. All of it stays in one file on this PC.">
      <div className="split">
        <section>
          <h2 className="section-title">Things you told me</h2>
          <form className="inline-form" onSubmit={event => void addFact(event)}>
            <input value={fact} onChange={event => setFact(event.target.value)} placeholder="Add something to remember" aria-label="New memory" />
            <button className="btn" disabled={!fact.trim()}><Plus size={14} aria-hidden="true" />Add</button>
          </form>
          {facts.facts.length === 0 ? <div className="empty">Nothing yet. Try saying "remember that my favourite editor is VS Code".</div> : (
            <ul className="rows">
              {facts.facts.map(item => (
                <li className="row" key={item.id}>
                  <span className="row-main">{item.fact}</span>
                  <button className="icon-btn subtle" aria-label={`Forget: ${item.fact}`} onClick={() => void api('DELETE', `/api/memory/${item.id}`).then(loadFacts)}><Trash2 size={14} /></button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section>
          <h2 className="section-title">Documents I can search</h2>
          <form className="inline-form" onSubmit={event => void addFolder(event)}>
            <input value={folder} onChange={event => setFolder(event.target.value)} placeholder="Folder, e.g. documents or D:\Notes" aria-label="Folder to index" />
            <button className="btn" disabled={!folder.trim()}><FolderPlus size={14} aria-hidden="true" />Add</button>
          </form>
          {indexProgress && <div className="index-progress" role="status"><span className="pulse" aria-hidden="true" />{indexProgress}</div>}
          {docs.folders.length === 0 ? <div className="empty">Add a folder and you can ask questions about what is inside its PDFs, Word files and notes. Answers cite the file they came from.</div> : (
            <ul className="rows">
              {docs.folders.map(item => (
                <li className="row" key={item.folder}>
                  <FileText size={16} className="row-icon" aria-hidden="true" />
                  <div className="row-main">
                    <div className="mono-line" title={item.folder}>{shortPath(item.folder, folders)}</div>
                    <div className="row-sub">{item.files} files · {formatBytes(item.bytes)}</div>
                  </div>
                  <button className="icon-btn subtle" aria-label={`Remove ${item.folder}`} onClick={() => void api('DELETE', `/api/documents?path=${encodeURIComponent(item.folder)}`).then(loadDocs)}><Trash2 size={14} /></button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Page>
  )
}

const STATUS_LABEL: Record<string, string> = { ok: 'Verified', unverified: 'Not provable', error: 'Failed', denied: 'Declined', cancelled: 'Cancelled' }

export function Activity() {
  const [data] = useLoad<{ entries: any[]; chain_valid: boolean }>('/api/activity?limit=300', 'library', { entries: [], chain_valid: true })
  return (
    <Page title="Activity" intro="Every action Aegis has taken on this PC. Each entry is sealed to the one before it, so edits to this log can be detected.">
      <div className={`banner ${data.chain_valid ? 'ok' : 'err'}`} role="status">
        {data.chain_valid ? <ShieldCheck size={16} aria-hidden="true" /> : <ShieldAlert size={16} aria-hidden="true" />}
        {data.chain_valid ? `The log is intact: ${data.entries.length} most recent entries shown.` : 'This log has been altered outside Aegis.'}
      </div>
      {data.entries.length === 0 ? <div className="empty">Nothing has happened yet.</div> : (
        <table className="activity">
          <thead><tr><th scope="col">When</th><th scope="col">Action</th><th scope="col">Result</th><th scope="col">What happened</th></tr></thead>
          <tbody>
            {data.entries.map(entry => (
              <tr key={entry.id}>
                <td className="nowrap">{when(entry.ts)}</td>
                <td className="mono">{entry.tool}</td>
                <td className={`status-${entry.status} nowrap`}>{STATUS_LABEL[entry.status] ?? entry.status}</td>
                <td className="summary">{entry.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  )
}

