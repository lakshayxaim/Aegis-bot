import { AlertTriangle, Check, ChevronRight, CircleDashed, Copy, X } from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { formatDuration } from '../format'
import { Item, useStore } from '../store'
import { QuestionCard } from './Ask'
import { Markdown } from './Markdown'
import { FileCard, Receipt } from './Receipt'

function Outcome({ item }: { item: Item }) {
  const toast = useStore(state => state.toast)
  const { status, actions = 0, verified = 0, failed = 0, duration } = item.data
  const copy = () => void navigator.clipboard.writeText(item.text).then(() => toast('Copied', 'The reply is on your clipboard.')).catch(() => toast('Could not copy', 'The clipboard is unavailable here.', 'error'))
  let badge = null
  if (status === 'failed' || status === 'error') badge = <span className="outcome err"><X size={13} aria-hidden="true" />Did not finish</span>
  else if (status === 'partial') badge = <span className="outcome warn"><AlertTriangle size={13} aria-hidden="true" />{failed} step{failed === 1 ? '' : 's'} failed</span>
  else if (status === 'unverified') badge = <span className="outcome warn"><CircleDashed size={13} aria-hidden="true" />No action was taken</span>
  else if (actions > 0) {
    const proven = verified === actions
    badge = <span className={`outcome ${proven ? 'ok' : 'warn'}`}>{proven ? <Check size={13} aria-hidden="true" /> : <CircleDashed size={13} aria-hidden="true" />}{proven ? `${actions} action${actions === 1 ? '' : 's'} verified` : `${verified} of ${actions} verified`}</span>
  }
  if (status === 'cancelled') return null
  return (
    <div className="outcome-row">
      {badge}
      {duration != null && <span className="outcome-time">{formatDuration(duration * 1000)}</span>}
      <button className="icon-btn subtle" onClick={copy} aria-label="Copy reply" title="Copy reply"><Copy size={13} /></button>
    </div>
  )
}

/** While working, every step is visible. Once finished, they fold into one line, and only what was made stays out. */
function Ledger({ items }: { items: Item[] }) {
  const [open, setOpen] = useState(false)
  const shown = items.filter(item => !['ask_user', 'more_tools'].includes(item.data?.name))
  if (!shown.length) return null
  const settled = shown.every(item => ['ok', 'unverified'].includes(item.data?.status))
  if (shown.length < 3 || !settled || open) {
    return (
      <>
        {open && <button className="link-btn fold" onClick={() => setOpen(false)}><ChevronRight size={13} className="chev open" aria-hidden="true" />Hide steps</button>}
        <ol className="ledger">{shown.map(item => <Receipt key={item.id} item={item} />)}</ol>
      </>
    )
  }
  const files = shown.flatMap(item => (item.data?.undone ? [] : item.data?.files ?? []))
  return (
    <>
      <button className="link-btn fold" onClick={() => setOpen(true)}><ChevronRight size={13} className="chev" aria-hidden="true" />{shown.length} steps</button>
      {files.length > 0 && <div className="receipt-files">{files.map((file: any) => <FileCard key={file.id} file={file} />)}</div>}
    </>
  )
}

type Block = { kind: 'user' | 'assistant'; item: Item } | { kind: 'tools'; items: Item[] }

export function Timeline({ threadId, onAnswer }: { threadId: string; onAnswer?: (text: string) => void }) {
  const items = useStore(state => state.items[threadId]) ?? []
  const draft = useStore(state => state.drafts[threadId])
  const statusText = useStore(state => state.statusText[threadId])
  const running = useStore(state => Boolean(state.runs[threadId]))
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  const blocks = useMemo(() => {
    const out: Block[] = []
    for (const item of items) {
      if (item.role === 'tool') {
        const last = out[out.length - 1]
        if (last?.kind === 'tools') last.items.push(item)
        else out.push({ kind: 'tools', items: [item] })
      } else {
        out.push({ kind: item.role, item })
      }
    }
    return out
  }, [items])

  const onScroll = () => {
    const element = scroller.current
    if (element) pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120
  }

  useLayoutEffect(() => {
    const element = scroller.current
    if (element && pinned.current) element.scrollTop = element.scrollHeight
  }, [blocks, draft, statusText])

  useEffect(() => {
    pinned.current = true
    const element = scroller.current
    if (element) element.scrollTop = element.scrollHeight
  }, [threadId])

  const lastAssistantId = [...items].reverse().find(item => item.role === 'assistant')?.id
  const toolBusy = items.some(item => item.role === 'tool' && ['running', 'awaiting_approval'].includes(item.data.status))
  const toolTexts = new Set(items.filter(item => item.role === 'tool').map(item => item.text.trim()))

  return (
    <div className="timeline" ref={scroller} onScroll={onScroll}>
      <div className="timeline-inner" aria-live="polite">
        {blocks.map(block => {
          if (block.kind === 'tools') return <Ledger key={block.items[0].id} items={block.items} />
          if (block.kind === 'user') return <div className="turn-user" key={block.item.id}><div className="user-bubble">{block.item.text}</div></div>
          if (block.item.data.status === 'question') return <QuestionCard key={block.item.id} item={block.item} active={block.item.id === items[items.length - 1]?.id && !running} onAnswer={text => onAnswer?.(text)} />
          const failed = ['failed', 'error'].includes(block.item.data.status)
          // When the receipt already says it all, the reply would only repeat it.
          const repeats = toolTexts.has(block.item.text.trim())
          return (
            <div className={`turn-assistant ${failed ? 'is-error' : ''}`} key={block.item.id}>
              {!repeats && <Markdown text={block.item.text} />}
              {block.item.id === lastAssistantId ? <Outcome item={block.item} /> : <div className="outcome-row quiet"><Outcome item={block.item} /></div>}
            </div>
          )
        })}
        {draft && <div className="turn-assistant streaming"><Markdown text={draft} /><span className="caret" aria-hidden="true" /></div>}
        {running && !draft && !toolBusy && (
          <div className="thinking" role="status"><span className="pulse" aria-hidden="true" />{statusText || 'Working…'}</div>
        )}
      </div>
    </div>
  )
}
