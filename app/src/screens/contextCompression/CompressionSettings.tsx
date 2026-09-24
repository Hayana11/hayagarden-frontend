import { useState } from 'react'
import {
  type CompressionSettingsConfig,
  type ContinuitySettingsView,
} from '../../lib/contextCompressionSettings'
import './CompressionSettings.css'

const LENGTH_OPTIONS = [8000, 12000, 16000, 20000]
const TURN_OPTIONS = [10, 20, 30, 40]

const PRODUCT_PROVIDER_NAMES = ['claude', 'gpt', 'deepseek']

function isRelayProvider(provider: { id: string; label: string }) {
  const id = provider.id.toLowerCase()
  const label = provider.label.replace(/\s+/g, '').toLowerCase()
  return id === 'api_relay' || id.includes('relay') || label.includes('relay')
}

function productProviders(providers: ContinuitySettingsView['providers']) {
  const visible = providers.filter(provider => !isRelayProvider(provider))
  return PRODUCT_PROVIDER_NAMES.map(name => visible.find(provider => (provider.id + ' ' + provider.label).toLowerCase().includes(name))).filter((provider): provider is ContinuitySettingsView['providers'][number] => !!provider)
}

function PinkStar() {
  return (
    <span className="cc-settings-star" aria-hidden="true">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 3.4 13.55 10.1 20.4 12 13.55 13.9 12 20.6 10.45 13.9 3.6 12 10.45 10.1Z" />
      </svg>
    </span>
  )
}

export function CompressionSettings({ value, onSave, onClose }: {
  value: ContinuitySettingsView
  onSave: (next: CompressionSettingsConfig) => Promise<ContinuitySettingsView>
  onClose: () => void
}) {
  const [settings, setSettings] = useState(value)
  const [draft, setDraft] = useState<CompressionSettingsConfig>({ ...value.editableConfig })
  const [customLength, setCustomLength] = useState(LENGTH_OPTIONS.indexOf(value.editableConfig.length) < 0)
  const [customTurns, setCustomTurns] = useState(TURN_OPTIONS.indexOf(value.editableConfig.turns) < 0)
  const [lengthText, setLengthText] = useState(String(value.editableConfig.length / 1000))
  const [turnsText, setTurnsText] = useState(String(value.editableConfig.turns))
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [saving, setSaving] = useState(false)
  const [providerHintId, setProviderHintId] = useState<string | null>(null)

  const visibleProviders = productProviders(settings.providers)
  const selectedProvider = settings.providers.find(provider => provider.id === draft.provider)
  const selectedModel = selectedProvider?.models.find(model => model.id === draft.model)

  function chooseProvider(providerId: string) {
    const provider = settings.providers.find(item => item.id === providerId)
    const firstModel = provider?.models.find(model => model.enabled)
    if (!provider?.enabled || !firstModel) return
    setDraft(previous => ({ ...previous, provider: provider.id, model: firstModel.id }))
    setProviderHintId(null)
    setError('')
    setSuccess('')
  }

  async function save() {
    const length = customLength ? Number(lengthText) * 1000 : draft.length
    const turns = customTurns ? Number(turnsText) : draft.turns
    if (!Number.isFinite(length) || length < 1000 || length > 1000000 || !Number.isInteger(length)
      || !Number.isFinite(turns) || turns < 1 || turns > 1000 || !Number.isInteger(turns)) {
      setError('请填写 1–1000k 的长度和 1–1000 的整数轮数')
      return
    }
    if (!selectedProvider?.enabled || !selectedModel?.enabled) {
      setError(selectedModel?.disabledReason || selectedProvider?.disabledReason || '当前 provider / model 不可用')
      return
    }
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const saved = await onSave({ ...draft, length, turns })
      setSettings(saved)
      setDraft({ ...saved.editableConfig })
      setCustomLength(LENGTH_OPTIONS.indexOf(saved.editableConfig.length) < 0)
      setCustomTurns(TURN_OPTIONS.indexOf(saved.editableConfig.turns) < 0)
      setLengthText(String(saved.editableConfig.length / 1000))
      setTurnsText(String(saved.editableConfig.turns))
      setSuccess('已保存，将从下一个新压缩块生效')
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  function restoreSettings() {
    setDraft({ ...settings.defaults, prompt: draft.prompt })
    setCustomLength(LENGTH_OPTIONS.indexOf(settings.defaults.length) < 0)
    setCustomTurns(TURN_OPTIONS.indexOf(settings.defaults.turns) < 0)
    setLengthText(String(settings.defaults.length / 1000))
    setTurnsText(String(settings.defaults.turns))
    setError('')
    setSuccess('')
  }

  function restorePrompt() {
    setDraft(previous => ({ ...previous, prompt: settings.defaults.prompt }))
    setError('')
    setSuccess('')
  }

  return (
    <section className="cc-settings" aria-label="压缩设置">
      <header className="cc-settings-header">
        <h2>压缩设置</h2>
        <button className="cc-settings-close" type="button" onClick={onClose} aria-label="关闭设置">×</button>
      </header>
      <div className="cc-settings-scroll">
        <fieldset className="cc-settings-group">
          <legend><PinkStar />压缩 provider</legend>
          <div className="cc-settings-options cc-settings-brands">
            {visibleProviders.map(provider => {
              const enabled = provider.enabled && provider.models.some(model => model.enabled)
              return <span className="cc-settings-brand" key={provider.id}>
                <button type="button" aria-disabled={!enabled} aria-pressed={draft.provider === provider.id} onMouseEnter={() => { if (!enabled) setProviderHintId(provider.id) }} onMouseLeave={() => setProviderHintId(null)} onFocus={() => { if (!enabled) setProviderHintId(provider.id) }} onBlur={() => setProviderHintId(null)} onClick={() => {
                  if (!enabled) { setProviderHintId(provider.id); return }
                  chooseProvider(provider.id)
                }}>{provider.label}</button>
                {!enabled && providerHintId === provider.id && <span className="cc-settings-hint" role="status">暂未接入压缩任务</span>}
              </span>
            })}
          </div>
          <label className="cc-settings-sr" htmlFor="cc-settings-model">具体模型</label>
          <select id="cc-settings-model" value={draft.model} disabled={!selectedProvider?.enabled} onChange={event => {
            setDraft(previous => ({ ...previous, model: event.target.value }))
            setError('')
            setSuccess('')
          }}>
            {(selectedProvider?.models || []).map(model => <option key={model.id} value={model.id} disabled={!model.enabled}>{model.label}{model.enabled ? '' : '（不可用）'}</option>)}
          </select>
        </fieldset>
        <fieldset className="cc-settings-group">
          <legend><PinkStar />压缩长度</legend>
          <div className="cc-settings-options">
            {LENGTH_OPTIONS.map(length => <button key={length} type="button" aria-pressed={!customLength && draft.length === length} onClick={() => {
              setDraft(previous => ({ ...previous, length }))
              setCustomLength(false)
              setError('')
              setSuccess('')
            }}>{length / 1000}k</button>)}
            <button type="button" aria-pressed={customLength} onClick={() => { setCustomLength(true); setSuccess('') }}>自定义</button>
          </div>
          {customLength && <label className="cc-settings-custom">长度（k）<input type="number" inputMode="decimal" min="1" max="1000" step="0.1" value={lengthText} onChange={event => { setLengthText(event.target.value); setSuccess('') }} /></label>}
        </fieldset>
        <fieldset className="cc-settings-group">
          <legend><PinkStar />压缩轮数</legend>
          <div className="cc-settings-options">
            {TURN_OPTIONS.map(turns => <button key={turns} type="button" aria-pressed={!customTurns && draft.turns === turns} onClick={() => {
              setDraft(previous => ({ ...previous, turns }))
              setCustomTurns(false)
              setError('')
              setSuccess('')
            }}>{turns}轮</button>)}
            <button type="button" aria-pressed={customTurns} onClick={() => { setCustomTurns(true); setSuccess('') }}>自定义</button>
          </div>
          {customTurns && <label className="cc-settings-custom">轮数<input type="number" inputMode="numeric" min="1" max="1000" step="1" value={turnsText} onChange={event => { setTurnsText(event.target.value); setSuccess('') }} /></label>}
          <p className="cc-settings-rule">满足任意一个条件，就开始压缩</p>
        </fieldset>
        <button type="button" className="cc-settings-reset" onClick={restoreSettings}>恢复默认设置</button>
        <p className="cc-settings-note">恢复 provider、模型、长度与轮数；保留你的自定义提示词。</p>
        <div className="cc-settings-group">
          <div className="cc-settings-star-rule" aria-hidden="true"><PinkStar /></div>
          <div className="cc-settings-prompt-heading"><h3>压缩提示词</h3><span>{draft.prompt === settings.defaults.prompt ? '默认提示词' : '自定义提示词'}</span></div>
          <label className="cc-settings-sr" htmlFor="cc-settings-prompt">压缩提示词</label>
          <textarea id="cc-settings-prompt" className="cc-settings-prompt" value={draft.prompt} onChange={event => {
            setDraft(previous => ({ ...previous, prompt: event.target.value }))
            setError('')
            setSuccess('')
          }} />
          <button type="button" className="cc-settings-prompt-restore" onClick={restorePrompt}>恢复默认提示词</button>
        </div>

        {error && <p className="cc-settings-error" role="alert">{error}</p>}
        {success && <p className="cc-settings-success" role="status">{success}</p>}
      </div>
      <footer className="cc-settings-footer"><button type="button" className="cc-settings-primary" disabled={saving || !draft.prompt.trim()} onClick={save}>{saving ? '保存中…' : '保存设置'}</button></footer>
    </section>
  )
}
