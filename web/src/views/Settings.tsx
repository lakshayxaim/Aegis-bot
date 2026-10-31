import { AlertCircle, Check, Cpu, Download, Lock, Mic, ShieldCheck, Unlock, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { VoiceBars } from '../components/Composer'
import { formatBytes } from '../format'
import { useMyVoice, useStore } from '../store'
import { Page } from './Pages'

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return <button role="switch" aria-checked={checked} aria-label={label} className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><span /></button>
}

function Row({ title, detail, children }: { title: string; detail?: string; children: React.ReactNode }) {
  return (
    <div className="setting-row">
      <div className="setting-text">
        <div className="setting-title">{title}</div>
        {detail && <div className="setting-detail">{detail}</div>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  )
}

const RISK_LABEL: Record<string, string> = { read: 'Just looks', write: 'Makes changes', danger: 'Always asks first' }

function VoiceSection() {
  const settings = useStore(state => state.settings)
  const speech = useStore(state => state.speech)
  const save = useStore(state => state.saveSettings)
  const toast = useStore(state => state.toast)
  const startVoice = useStore(state => state.startVoice)
  const stopVoice = useStore(state => state.stopVoice)
  const voiceResult = useStore(state => state.voiceResult)
  const { listening, transcribing, partial, level } = useMyVoice()
  const [inputs, setInputs] = useState<string[]>([])
  const [systemDefault, setSystemDefault] = useState('')
  const [heard, setHeard] = useState<{ text: string; confident: boolean } | null>(null)
  const handled = useRef(voiceResult?.seq ?? 0)

  useEffect(() => {
    void api('GET', '/api/voice/devices').then(data => {
      setInputs(data.inputs)
      setSystemDefault(data.default ?? '')
    }).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (voiceResult && voiceResult.seq !== handled.current) {
      handled.current = voiceResult.seq
      setHeard({ text: voiceResult.text, confident: voiceResult.confident })
    }
  }, [voiceResult])

  const select = (id: string) => void api('POST', '/api/speech/select', { id }).catch(error => toast('Could not switch', error.message, 'error'))
  const download = (id: string) => void api('POST', '/api/speech/download', { id }).catch(error => toast('Could not start the download', error.message, 'error'))
  const fetching = speech?.download

  return (
    <section className="settings-section">
      <h2 className="section-title">Voice</h2>
      {(speech?.engines ?? []).map(engine => (
        <Row key={engine.id} title={engine.label} detail={engine.detail}>
          {engine.id === speech?.active ? <span className="pill">In use</span>
            : engine.installed ? <button className="btn" onClick={() => select(engine.id)}>Use this</button>
            : <button className="btn" disabled={Boolean(fetching)} onClick={() => download(engine.id)}><Download size={14} aria-hidden="true" />Download {formatBytes(engine.size_mb * 1e6)}</button>}
        </Row>
      ))}
      {fetching && (
        <div className="progress" role="progressbar" aria-valuenow={fetching.percent} aria-valuemin={0} aria-valuemax={100} aria-label="Speech model download">
          <div className="progress-bar"><span style={{ width: `${Math.max(fetching.percent, 2)}%` }} /></div>
          <div className="progress-text">{fetching.status}</div>
        </div>
      )}
      <Row title="Microphone" detail="Earbuds and headsets usually hear you far better than a laptop's built-in microphone.">
        <select value={settings.voice_device ?? ''} onChange={event => void save({ voice_device: event.target.value })} aria-label="Microphone">
          <option value="">Windows default{systemDefault ? ` (${systemDefault})` : ''}</option>
          {inputs.map(name => <option key={name} value={name}>{name}</option>)}
        </select>
      </Row>
      <Row title="Try it" detail={heard ? undefined : 'Say something to see what Aegis hears. Nothing is sent.'}>
        <div className="voice-try">
          {listening && <VoiceBars level={level} />}
          <button className="btn" disabled={transcribing} onClick={() => { setHeard(null); void (listening ? stopVoice() : startVoice()) }}><Mic size={14} aria-hidden="true" />{listening ? 'Finish' : transcribing ? 'Working…' : 'Start'}</button>
        </div>
      </Row>
      {(partial || heard) && <div className={`voice-heard ${heard && !heard.confident ? 'unsure' : ''}`} role="status">{heard ? heard.text : partial}{heard && !heard.confident && <span> (not confident, so this would wait for you to check it)</span>}</div>}
      <Row title="Listen as soon as the overlay opens" detail="The microphone turns on with the overlay and turns off when it closes or you stop talking. Start typing and it switches off straight away.">
        <Toggle checked={Boolean(settings.listen_on_open)} label="Listen as soon as the overlay opens" onChange={value => void save({ listen_on_open: value })} />
      </Row>
      <Row title="Send when you stop talking" detail="Clearly heard requests go straight through. Turn this off to read each one and press Enter yourself.">
        <Toggle checked={Boolean(settings.voice_autosend)} label="Send when you stop talking" onChange={value => void save({ voice_autosend: value })} />
      </Row>
      <Row title="Read replies aloud" detail="Uses the voice built into Windows.">
        <Toggle checked={Boolean(settings.speak_replies)} label="Read replies aloud" onChange={value => void save({ speak_replies: value })} />
      </Row>
    </section>
  )
}

export function Settings() {
  const settings = useStore(state => state.settings)
  const model = useStore(state => state.model)
  const net = useStore(state => state.net)
  const pull = useStore(state => state.pull)
  const save = useStore(state => state.saveSettings)
  const toast = useStore(state => state.toast)
  const [doctor, setDoctor] = useState<any[]>([])
  const [tools, setTools] = useState<any[]>([])
  const [custom, setCustom] = useState('')
  const [name, setName] = useState('')

  useEffect(() => {
    void api('GET', '/api/doctor').then(data => setDoctor(data.checks)).catch(() => undefined)
    void api('GET', '/api/capabilities').then(data => setTools(data.tools)).catch(() => undefined)
  }, [model?.model, settings.offline])

  useEffect(() => setName(settings.user_name ?? ''), [settings.user_name])

  const selectModel = (value: string) => void api('POST', '/api/models/select', { model: value }).catch(error => toast('Could not switch models', error.message, 'error'))
  const pullModel = () => {
    const value = custom.trim()
    if (!value) return
    setCustom('')
    void api('POST', '/api/models/pull', { model: value }).catch(error => toast('Could not start the download', error.message, 'error'))
  }

  const groups: Record<string, any[]> = {}
  for (const tool of tools) (groups[tool.group] ??= []).push(tool)
  const always: string[] = settings.always_allow ?? []

  return (
    <Page title="Settings">
      <section className="settings-section">
        <h2 className="section-title">Privacy</h2>
        <Row title="Offline mode" detail={settings.offline ? `On. Aegis cannot reach the internet${net?.blocked_total ? `, and has blocked ${net.blocked_total} connection attempt${net.blocked_total === 1 ? '' : 's'} since it started` : ''}, so it can't read websites or use them for you.` : 'Off. The AI still runs only on this PC. Aegis uses the internet like you would, for the pages and searches a task needs.'}>
          <div className="with-icon">{settings.offline ? <Lock size={14} aria-hidden="true" /> : <Unlock size={14} aria-hidden="true" />}<Toggle checked={Boolean(settings.offline)} label="Offline mode" onChange={value => void save({ offline: value })} /></div>
        </Row>
        <Row title="Actions that no longer ask" detail={always.length ? 'These run without asking you first. Remove one to be asked again. Sending messages and running code always ask.' : 'None. Aegis asks before anything risky. Sending messages and running code always ask.'}>
          <div className="chips">
            {always.map(tool => <button key={tool} className="chip" aria-label={`Ask again before ${tool}`} onClick={() => void save({ always_allow: always.filter(item => item !== tool) })}><span className="mono">{tool}</span><X size={12} aria-hidden="true" /></button>)}
          </div>
        </Row>
      </section>

      <section className="settings-section">
        <h2 className="section-title">Model</h2>
        {model?.note && <div className="banner warn" role="status"><AlertCircle size={16} aria-hidden="true" />{model.note}</div>}
        <Row title="Language model" detail={model?.loaded ? `Loaded${model.gpu_percent != null ? `, ${model.gpu_percent}% on the GPU` : ''}.` : 'Loads the first time you ask for something.'}>
          <select value={model?.model ?? ''} onChange={event => selectModel(event.target.value)} aria-label="Language model">
            {(model?.installed ?? []).filter(item => item.capabilities.includes('tools')).map(item => <option key={item.name} value={item.name}>{item.name} ({formatBytes(item.size)})</option>)}
          </select>
        </Row>
        <Row title="Use the GPU" detail="Runs the model on the Arc graphics chip, which is several times faster than the CPU here. Turn off only if you see graphics problems.">
          <div className="with-icon"><Cpu size={14} aria-hidden="true" /><Toggle checked={Boolean(settings.use_gpu)} label="Use the GPU" onChange={value => void save({ use_gpu: value })} /></div>
        </Row>
        <Row title="Download another model" detail="Any tool-capable model from the Ollama library. This uses the internet, through Ollama rather than Aegis itself.">
          <div className="inline-form tight">
            <input value={custom} onChange={event => setCustom(event.target.value)} placeholder="e.g. qwen3.5:9b" aria-label="Model to download" />
            <button className="btn" disabled={!custom.trim()} onClick={pullModel}><Download size={14} aria-hidden="true" />Get</button>
          </div>
        </Row>
        {Object.entries(pull).map(([key, progress]) => (
          <div className="progress" key={key} role="status">
            <div className="progress-bar"><span style={{ width: `${progress.total ? Math.round(((progress.completed ?? 0) / progress.total) * 100) : 4}%` }} /></div>
            <div className="progress-text">{key}: {progress.status}</div>
          </div>
        ))}
      </section>

      <section className="settings-section">
        <h2 className="section-title">You</h2>
        <Row title="Overlay see-through" detail="How much of what's behind the overlay shows through. Lower is more see-through.">
          <input type="range" min={45} max={100} step={1} value={settings.overlay_opacity ?? 92} onChange={event => void save({ overlay_opacity: Number(event.target.value) })} aria-label="Overlay opacity" />
        </Row>
        <Row title="Your name" detail="How Aegis addresses you.">
          <input className="short" value={name} onChange={event => setName(event.target.value)} onBlur={() => name.trim() && name !== settings.user_name && void save({ user_name: name.trim() })} aria-label="Your name" />
        </Row>
        <Row title="Appearance">
          <div className="segmented" role="radiogroup" aria-label="Appearance">
            {(['system', 'dark', 'light'] as const).map(theme => <button key={theme} role="radio" aria-checked={settings.theme === theme} className={settings.theme === theme ? 'active' : ''} onClick={() => void save({ theme })}>{theme[0].toUpperCase() + theme.slice(1)}</button>)}
          </div>
        </Row>
      </section>

      <VoiceSection />

      <section className="settings-section">
        <h2 className="section-title">This PC</h2>
        <ul className="health">
          {doctor.map(check => (
            <li key={check.name} className={check.ok ? 'ok' : check.optional ? 'warn' : 'err'}>
              {check.ok ? <Check size={14} aria-label="Working" /> : <AlertCircle size={14} aria-label="Needs attention" />}
              <div>
                <div className="health-name">{check.name}</div>
                <div className="health-detail">{check.detail}{!check.ok && check.fix ? ` ${check.fix}` : ''}</div>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="settings-section">
        <h2 className="section-title">What Aegis can do</h2>
        <p className="page-intro small"><ShieldCheck size={14} aria-hidden="true" /> {tools.length} abilities. You never need these names; just say what you want.</p>
        <div className="capabilities">
          {Object.entries(groups).map(([group, entries]) => (
            <div key={group} className="cap-group">
              <h3 className="section-sub">{group}</h3>
              <ul>
                {entries.map(tool => (
                  <li key={tool.name} className={tool.enabled ? '' : 'disabled'} title={tool.description}>
                    <span className="mono">{tool.name}</span>
                    <span className={`risk ${tool.risk}`}>{tool.enabled ? RISK_LABEL[tool.risk] : 'Off while offline'}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </section>
    </Page>
  )
}
