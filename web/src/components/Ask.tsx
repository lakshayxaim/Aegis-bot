import { ChevronRight, CircleHelp, Code2, MessageCircleQuestion, Send, ShieldCheck, Undo2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Item, useStore } from '../store'
import { Markdown } from './Markdown'

const KIND = {
  change: { icon: ShieldCheck, yes: 'Allow' },
  send: { icon: Send, yes: 'Send' },
  run: { icon: Code2, yes: 'Run it' },
} as const

/** Enter and Esc answer whichever card is showing, except while the user is typing something else. */
function useAnswerKeys(onYes: () => void, onNo: () => void, enabled: boolean) {
  const handlers = useRef({ onYes, onNo })
  handlers.current = { onYes, onNo }
  useEffect(() => {
    if (!enabled) return
    // A key already on its way when the card appeared must not count as the answer.
    const armed = window.setTimeout(() => {
      window.addEventListener('keydown', listener, true)
    }, 350)
    const listener = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.tagName === 'TEXTAREA' && (target as HTMLTextAreaElement).value) return
      if (target?.tagName === 'BUTTON' && event.key === 'Enter') return
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        event.stopImmediatePropagation()
        handlers.current.onYes()
      } else if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        handlers.current.onNo()
      }
    }
    return () => {
      window.clearTimeout(armed)
      window.removeEventListener('keydown', listener, true)
    }
  }, [enabled])
}

export function ApprovalCard({ item }: { item: Item }) {
  const decide = useStore(state => state.decide)
  const approval = item.data.approval
  const kind = KIND[(approval.kind as keyof typeof KIND) ?? 'change'] ?? KIND.change
  const [sent, setSent] = useState(false)
  // What goes out to other people is always on show. Long code stays folded until asked for.
  const [open, setOpen] = useState(approval.kind === 'send' || (approval.code ?? '').length < 240)
  const answer = (decision: 'once' | 'task' | 'always' | 'deny') => {
    if (sent) return
    setSent(true)
    void decide(item.id, decision)
  }
  useAnswerKeys(() => answer('once'), () => answer('deny'), !sent)
  const Icon = kind.icon
  return (
    <div className={`ask ask-${approval.kind ?? 'change'}`} role="alertdialog" aria-label={approval.title}>
      <div className="ask-head">
        <Icon size={15} aria-hidden="true" />
        <span className="ask-title">{approval.title}</span>
      </div>
      {approval.detail && <p className="ask-detail">{approval.detail}</p>}
      {approval.code && (open
        ? <pre className={`code-block ask-code ${approval.kind === 'send' ? 'quote' : ''}`}><code>{approval.code}</code></pre>
        : <button className="link-btn" onClick={() => setOpen(true)}><ChevronRight size={13} aria-hidden="true" />Show what it will do</button>)}
      {approval.after_web && <p className="ask-detail warn">This request has read web pages, which could try to mislead Aegis. Check that this is what you wanted.</p>}
      <div className="ask-actions">
        <button className="btn primary" disabled={sent} onClick={() => answer('once')}>{kind.yes}<kbd>Enter</kbd></button>
        <button className="btn" disabled={sent} onClick={() => answer('deny')}>Don't<kbd>Esc</kbd></button>
        <span className="ask-more">
          {approval.can_task !== false && <button className="link-btn" disabled={sent} onClick={() => answer('task')}>Yes for this whole task</button>}
          {approval.can_remember !== false && <button className="link-btn" disabled={sent} onClick={() => answer('always')}>Always</button>}
        </span>
      </div>
      {approval.undoable && <div className="ask-note"><Undo2 size={12} aria-hidden="true" />You can undo this afterwards.</div>}
    </div>
  )
}

export function QuestionCard({ item, onAnswer, active }: { item: Item; onAnswer: (text: string) => void; active: boolean }) {
  const options: string[] = item.data.options ?? []
  useEffect(() => {
    if (!active || !options.length) return
    const listener = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.tagName === 'TEXTAREA' && (target as HTMLTextAreaElement).value) return
      const index = Number(event.key) - 1
      if (Number.isInteger(index) && options[index]) {
        event.preventDefault()
        onAnswer(options[index])
      }
    }
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [active, options, onAnswer])
  const Icon = active ? MessageCircleQuestion : CircleHelp
  return (
    <div className={`ask ask-question ${active ? '' : 'answered'}`} role={active ? 'alert' : undefined}>
      <div className="ask-head">
        <Icon size={15} aria-hidden="true" />
        <span className="ask-label">{active ? 'Aegis needs an answer' : 'Aegis asked'}</span>
      </div>
      <Markdown text={item.text} />
      {active && options.length > 0 && (
        <div className="ask-options">
          {options.map((option, index) => (
            <button className="option" key={option} onClick={() => onAnswer(option)}>
              <kbd>{index + 1}</kbd>{option}
            </button>
          ))}
        </div>
      )}
      {active && <div className="ask-note">Type your answer, or press Space to say it.</div>}
    </div>
  )
}
