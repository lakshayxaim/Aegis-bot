import { ArrowUp, Check, Mic, Square, X } from 'lucide-react'
import { ClipboardEvent, KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useMyVoice, useStore } from '../store'

export function VoiceBars({ level }: { level: number }) {
  const bars = 21
  return (
    <div className="voice-bars" aria-hidden="true">
      {Array.from({ length: bars }, (_, index) => {
        const distance = Math.abs(index - (bars - 1) / 2) / ((bars - 1) / 2)
        const height = 3 + (1 - distance * distance) * level * 22 * (0.72 + 0.28 * Math.sin(index * 2.1))
        return <span key={index} style={{ height: `${Math.max(3, height)}px` }} />
      })}
    </div>
  )
}

function Hints({ items }: { items: [string, string][] }) {
  return (
    <div className="quick-hints" aria-hidden="true">
      {items.map(([key, label]) => <span key={key}><kbd>{key}</kbd>{label}</span>)}
    </div>
  )
}

export function Composer({ threadId, onSend, placeholder, autoFocus, compact }: { threadId: string | null; onSend: (text: string) => void; placeholder?: string; autoFocus?: boolean; compact?: boolean }) {
  const [text, setText] = useState('')
  const [unsure, setUnsure] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  const running = useStore(state => (threadId ? Boolean(state.runs[threadId]) : false))
  const stop = useStore(state => state.stop)
  const startVoice = useStore(state => state.startVoice)
  const stopVoice = useStore(state => state.stopVoice)
  const cancelVoice = useStore(state => state.cancelVoice)
  const autoSend = useStore(state => state.settings.voice_autosend) ?? true
  const voiceResult = useStore(state => state.voiceResult)
  const handled = useRef(voiceResult?.seq ?? 0)
  const { listening, transcribing, partial, level } = useMyVoice()
  // Typing takes over the moment it starts, before the microphone has finished closing.
  const voiceActive = (listening || transcribing) && !text

  useLayoutEffect(() => {
    const element = area.current
    if (!element) return
    element.style.height = 'auto'
    // Empty, it is always one line; the placeholder must not affect the measurement.
    if (text) element.style.height = `${Math.min(element.scrollHeight, 200)}px`
    element.style.overflowY = text && element.scrollHeight > 200 ? 'auto' : 'hidden'
  }, [text])

  useEffect(() => {
    if (autoFocus) area.current?.focus()
  }, [autoFocus, threadId])

  // The keyboard shortcuts live on the text box, so it keeps focus after the buttons are clicked.
  useEffect(() => {
    area.current?.focus()
  }, [voiceActive])

  // Clearly heard speech goes straight through. Anything doubtful waits here to be read first,
  // because a TV in the room should not be able to give Aegis commands.
  useEffect(() => {
    if (voiceResult && voiceResult.seq !== handled.current) {
      handled.current = voiceResult.seq
      if (voiceResult.confident && autoSend && !running) {
        onSend(voiceResult.text)
      } else {
        setText(voiceResult.text)
        setUnsure(!voiceResult.confident)
        area.current?.focus()
      }
    }
  }, [voiceResult, onSend, autoSend, running])

  const submit = () => {
    const value = text.trim()
    if (!value || running) return
    setText('')
    setUnsure(false)
    onSend(value)
  }

  const onChange = (value: string) => {
    setText(value)
    setUnsure(false)
    if (value && (listening || transcribing)) void cancelVoice()
  }

  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return
    const plain = !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
    // A held global hotkey keeps repeating into this box, so repeats and modifier chords are ignored.
    if (event.key === ' ' && plain && !event.repeat && !text) {
      event.preventDefault()
      if (listening) void stopVoice()
      else if (!transcribing) void startVoice()
      return
    }
    if (event.key === ' ' && voiceActive) {
      event.preventDefault()
      return
    }
    if (event.key === 'Escape' && voiceActive && !compact) {
      event.preventDefault()
      void cancelVoice()
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      if (listening && !text) void stopVoice()
      else submit()
    }
  }

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    // Pasting into an empty box while listening means the user would rather type.
    if (voiceActive && event.clipboardData.getData('text')) void cancelVoice()
  }

  const hints: [string, string][] = voiceActive
    ? listening ? [['Space', 'done'], ['Esc', compact ? 'close' : 'cancel']] : [['Esc', compact ? 'close' : 'cancel']]
    : text ? [['Enter', 'send'], ['Shift+Enter', 'new line'], ['Esc', 'close']] : [['Space', 'talk'], ['Enter', 'send'], ['Esc', 'close']]

  return (
    <>
      <div className={`composer ${voiceActive ? 'listening' : ''} ${compact ? 'compact' : ''}`}>
        {voiceActive && (
          <div className="voice-panel" role="status" aria-live="polite">
            {listening ? <VoiceBars level={level} /> : <span className="pulse" aria-hidden="true" />}
            <span className={`voice-label ${partial ? 'heard' : ''}`}>{partial || (listening ? 'Listening… talk, or just start typing' : 'Working out what you said…')}</span>
          </div>
        )}
        <textarea
          ref={area}
          rows={1}
          value={text}
          onChange={event => onChange(event.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
          placeholder={placeholder ?? 'Ask Aegis to do something, or press Space to talk…'}
          aria-label="Message to Aegis"
        />
        {voiceActive ? (
          <>
            <button className="icon-btn" onClick={() => void cancelVoice()} aria-label="Cancel dictation" title="Cancel (Esc)"><X size={17} /></button>
            {listening && <button className="send-btn" onClick={() => void stopVoice()} aria-label="Finish dictation" title="Finish (Space)"><Check size={17} /></button>}
          </>
        ) : (
          <>
            <button className="icon-btn" onClick={() => void startVoice()} aria-label="Speak instead of typing" title="Speak (Space)"><Mic size={17} /></button>
            {running ? (
              <button className="send-btn stop" onClick={() => threadId && void stop(threadId)} aria-label="Stop" title="Stop"><Square size={13} fill="currentColor" /></button>
            ) : (
              <button className="send-btn" onClick={submit} disabled={!text.trim()} aria-label="Send" title="Send (Enter)"><ArrowUp size={17} /></button>
            )}
          </>
        )}
      </div>
      {unsure && <div className="composer-note" role="status">I'm not sure I heard that right. Fix anything that's wrong, then press Enter.</div>}
      {compact && !unsure && <Hints items={hints} />}
    </>
  )
}
