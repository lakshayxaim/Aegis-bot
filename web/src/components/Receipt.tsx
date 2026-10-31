import { AlertTriangle, Ban, Check, ChevronRight, CircleDashed, File, FileImage, FileSpreadsheet, FileText, FileVideo, FolderOpen, Loader2, Music, Undo2, X } from 'lucide-react'
import { useState } from 'react'
import { api, rawUrl } from '../api'
import { ApprovalCard } from './Ask'
import { formatBytes, formatDuration, shortPath } from '../format'
import { Item, useStore } from '../store'

const FILE_ICONS: Record<string, typeof File> = { image: FileImage, video: FileVideo, audio: Music, pdf: FileText, document: FileText, text: FileText, spreadsheet: FileSpreadsheet }

function StatusGlyph({ status }: { status: string }) {
  switch (status) {
    case 'running':
      return <Loader2 className="glyph spin" size={15} aria-label="Running" />
    case 'ok':
      return <Check className="glyph ok" size={15} aria-label="Verified" />
    case 'unverified':
      return <CircleDashed className="glyph warn" size={15} aria-label="Sent, but not provable" />
    case 'awaiting_approval':
      return <AlertTriangle className="glyph warn" size={15} aria-label="Waiting for your answer" />
    case 'denied':
    case 'cancelled':
      return <Ban className="glyph muted" size={15} aria-label="Did not run" />
    default:
      return <X className="glyph err" size={15} aria-label="Failed" />
  }
}

export function FileCard({ file }: { file: { path: string; name: string; kind: string; size: number } }) {
  const toast = useStore(state => state.toast)
  const folders = useStore(state => state.folders)
  const Icon = FILE_ICONS[file.kind] ?? File
  const act = async (path: string) => {
    try {
      await api('POST', path, { path: file.path })
    } catch (error) {
      toast('Could not open it', (error as Error).message, 'error')
    }
  }
  return (
    <div className={`file-card ${file.kind === 'image' ? 'has-image' : ''}`}>
      {file.kind === 'image' && (
        <button className="file-thumb" onClick={() => void act('/api/files/open')} aria-label={`Open ${file.name}`}>
          <img src={rawUrl(file.path)} alt={file.name} loading="lazy" />
        </button>
      )}
      <div className="file-row">
        <Icon size={16} className="file-icon" aria-hidden="true" />
        <div className="file-meta">
          <div className="file-name" title={file.path}>{file.name}</div>
          <div className="file-sub" title={file.path}>{formatBytes(file.size)} · {shortPath(file.path, folders)}</div>
        </div>
        <button className="btn ghost small" onClick={() => void act('/api/files/open')}>Open</button>
        <button className="btn ghost small icon" onClick={() => void act('/api/files/reveal')} aria-label="Show in folder" title="Show in folder"><FolderOpen size={14} /></button>
      </div>
    </div>
  )
}

export function Receipt({ item }: { item: Item }) {
  const undo = useStore(state => state.undo)
  const [open, setOpen] = useState(false)
  const data = item.data
  const status: string = data.status ?? 'running'
  const label = status === 'running' ? data.progress || data.doing || item.text : item.text
  const args = Object.entries(data.args ?? {}).filter(([, value]) => value !== '' && value != null)
  const hasDetail = status !== 'running' && status !== 'awaiting_approval' && (args.length > 0 || data.verified || data.data_preview || data.hint)
  const files: any[] = data.files ?? []
  return (
    <li className={`receipt status-${status}`}>
      <div className="receipt-line">
        <span className="receipt-glyph"><StatusGlyph status={status} /></span>
        {hasDetail ? (
          <button className="receipt-label as-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>
            <span className="receipt-text">{label}</span>
            <ChevronRight size={13} className={`chev ${open ? 'open' : ''}`} aria-hidden="true" />
          </button>
        ) : (
          <span className="receipt-label"><span className="receipt-text">{label}</span></span>
        )}
        {data.undo_id && status === 'ok' && !data.undone && (
          <button className="btn ghost small undo" onClick={() => void undo(data.undo_id)}><Undo2 size={13} aria-hidden="true" />Undo</button>
        )}
        {data.undone && <span className="pill muted">Undone</span>}
        {data.duration_ms != null && <span className="receipt-time">{formatDuration(data.duration_ms)}</span>}
      </div>
      {status === 'unverified' && <div className="receipt-note">Sent, but there is no way to confirm it took effect.</div>}
      {status === 'awaiting_approval' && data.approval && <ApprovalCard item={item} />}
      {files.length > 0 && !data.undone && <div className="receipt-files">{files.map(file => <FileCard key={file.id} file={file} />)}</div>}
      {open && hasDetail && (
        <div className="receipt-detail">
          {data.verified && <div className="proof"><Check size={13} aria-hidden="true" /><span>{data.verified}</span></div>}
          {args.length > 0 && (
            <dl className="kv">
              <dt>tool</dt><dd>{data.name}</dd>
              {args.map(([key, value]) => (
                <div className="kv-row" key={key}><dt>{key}</dt><dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd></div>
              ))}
            </dl>
          )}
          {data.data_preview && <pre className="code-block small"><code>{data.data_preview}</code></pre>}
        </div>
      )}
    </li>
  )
}
