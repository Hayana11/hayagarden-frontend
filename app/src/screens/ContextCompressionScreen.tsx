import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../components/PageHeader'
import {
  fetchContinuityBlockDetail,
  fetchContinuityBlocks,
  fetchContinuityCurrent,
  type ContinuityBlock,
  type ContinuityBlockDetailResponse,
  type ContinuityCurrentResponse,
  type ContinuityMessage,
} from '../lib/contextCompression'
import {
  fetchContinuitySettings,
  saveContinuitySettings,
  type ContinuitySettingsView,
} from '../lib/contextCompressionSettings'
import { ROUTES } from '../navigation'
import { CompressionSettings } from './contextCompression/CompressionSettings'
import './ContextCompressionScreen.css'

const SHANGHAI_TIME = 'Asia/Shanghai'
const weekdays = ['日', '一', '二', '三', '四', '五', '六']

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function formatLogicalSize(value: number | null) {
  if (value === null) return '—'
  if (Math.abs(value) >= 1000) return (value / 1000).toFixed(1) + 'k'
  return formatNumber(value)
}

function timeLabel(value: string | null) {
  if (!value) return '—'
  const localMatch = /^\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/.exec(value)
  if (localMatch && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) return localMatch[1]
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: SHANGHAI_TIME,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function dayFromTimestamp(value: string | null) {
  if (!value) return ''
  const localMatch = /^(\d{4}-\d{2}-\d{2})[ T]/.exec(value)
  if (localMatch && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) return localMatch[1]
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: SHANGHAI_TIME,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date)
  const valueOf = (type: string) => parts.find(part => part.type === type)?.value || ''
  return valueOf('year') + '-' + valueOf('month') + '-' + valueOf('day')
}

function dayLabel(day: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (!match) return day
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  return match[2] + '/' + match[3] + ' 周' + weekdays[date.getDay()]
}

function formatStats(block: ContinuityBlock) {
  const compressed = block.compressed_size ?? block.compressed_char_count
  const parts = [block.completed_turn_count + ' 轮']
  if (block.original_char_count !== null) parts.push(block.original_char_count + ' 字')
  parts.push(formatLogicalSize(block.logical_size) + (compressed === null ? '' : ' → ' + formatLogicalSize(compressed)))
  return parts.join(' · ')
}

function currentStats(current: ContinuityCurrentResponse) {
  return [current.source_count + ' 条', current.original_char_count + ' 字', formatLogicalSize(current.logical_size)].join(' · ')
}

function nextChatLabel(current: ContinuityCurrentResponse) {
  return '约再聊 ' + Math.max(0, current.turn_target - current.completed_turn_count) + ' 轮'
}

function engineLabel(settings: ContinuitySettingsView) {
  const modelId = settings.activeConfig.model
  for (const provider of settings.providers) {
    const model = provider.models.find(item => item.id === modelId)
    if (model) return model.label
  }
  return modelId
}

function blockDay(block: ContinuityBlock) {
  return block.local_day || dayFromTimestamp(block.start_at)
}

function Icon({ name }: { name: 'calendar' | 'settings' | 'close' | 'leaf' | 'star' | 'edit' }) {
  const paths = {
    calendar: <><rect x="4" y="5" width="16" height="16" rx="3" /><path d="M8 3v4m8-4v4M4 10h16m-11 4h2m3 0h2m-7 3h2" /></>,
    settings: <><path d="m9 4 1-2h4l1 2 3 2 2 1v4l-2 1-1 3v3l-3 2-2-1-3 1-3-2v-3l-2-2V9l2-1 1-3Z" /><circle cx="12" cy="11" r="3" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    leaf: <><path d="M19 4c-10-2-16 5-12 11s14 1 12-11ZM5 21 15 9" /></>,
    star: <><path d="M12 3.4 13.55 10.1 20.4 12 13.55 13.9 12 20.6 10.45 13.9 3.6 12 10.45 10.1Z" fill="currentColor" stroke="none" /><path d="M18.35 5.7 18.85 7.7 20.9 8.2 18.85 8.7 18.35 10.7 17.85 8.7 15.8 8.2 17.85 7.7Z" fill="currentColor" stroke="none" /></>,
    edit: <><path d="m5 15-1 5 5-1L20 8l-4-4L5 15Zm9-9 4 4" /></>,
  }
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}

function messageRole(role: string) {
  if (role === 'user') return '我'
  if (role === 'assistant') return '陪伴'
  if (role === 'system') return '系统'
  return role || '消息'
}

function OriginalChat({ messages }: { messages: ContinuityMessage[] }) {
  if (!messages.length) return <p className="cc-empty-copy">这一段没有可显示的聊天记录。</p>
  return <div className="cc-chat">{messages.map((message, index) => {
    const user = message.role === 'user'
    return <div className={'cc-message ' + (user ? 'cc-message--user' : '')} key={message.id || message.source_ref || String(index)}>
      <div className="cc-speaker">{messageRole(message.role)} <span>{timeLabel(message.created_at)}</span></div>
      <p>{message.content}</p>
    </div>
  })}</div>
}

function Sheet({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  const [expanded, setExpanded] = useState(false)
  const sheet = useRef<HTMLDivElement>(null)
  const drag = useRef<number | null>(null)
  const suppressHandleClick = useRef(false)

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const root = sheet.current
    root?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
      if (event.key === 'Tab' && root) {
        const items = Array.from(root.querySelectorAll<HTMLElement>('button:not(:disabled),[tabindex="0"]')).filter(item => item.offsetParent !== null)
        const first = items[0]
        const last = items[items.length - 1]
        if (!first) {
          event.preventDefault()
        } else if (event.shiftKey && (document.activeElement === first || document.activeElement === root)) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('keydown', key)
      previous?.focus()
    }
  }, [onClose])

  return <div className="cc-overlay" onClick={event => { if (event.target === event.currentTarget) onClose() }}>
    <div ref={sheet} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} className={'cc-sheet ' + (expanded ? 'cc-sheet--expanded' : '')}>
      <button type="button" className="cc-handle" aria-label={expanded ? '收起详情' : '展开详情'} onClick={() => {
        if (suppressHandleClick.current) {
          suppressHandleClick.current = false
          return
        }
        setExpanded(value => !value)
      }} onPointerDown={event => {
        suppressHandleClick.current = false
        drag.current = event.clientY
        event.currentTarget.setPointerCapture(event.pointerId)
      }} onPointerUp={event => {
        if (drag.current === null) return
        const distance = event.clientY - drag.current
        drag.current = null
        if (distance > 65) {
          event.preventDefault()
          onClose()
        } else if (distance < -35) {
          event.preventDefault()
          suppressHandleClick.current = true
          setExpanded(true)
        }
      }} onPointerCancel={() => { drag.current = null }}><span /></button>
      {children}
    </div>
  </div>
}

function Popover({ anchor, kind, width, label, onClose, children }: {
  anchor: HTMLElement | null
  kind: 'calendar' | 'settings'
  width: number
  label: string
  onClose: () => void
  children: ReactNode
}) {
  const box = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ top: 0, left: 8, width, ready: false })

  useLayoutEffect(() => {
    function place() {
      const element = box.current
      if (!anchor || !element) return
      const rect = anchor.getBoundingClientRect()
      const gutter = 8
      const nextWidth = Math.min(width, Math.max(200, window.innerWidth - gutter * 2))
      const left = Math.max(gutter, Math.min(rect.right - nextWidth, window.innerWidth - gutter - nextWidth))
      const below = rect.bottom + 8
      const above = rect.top - element.offsetHeight - 8
      const top = below + element.offsetHeight <= window.innerHeight - gutter ? below : Math.max(gutter, above)
      setPos({ top, left, width: nextWidth, ready: true })
    }
    place()
    window.addEventListener('resize', place)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    if (box.current) observer?.observe(box.current)
    return () => {
      window.removeEventListener('resize', place)
      observer?.disconnect()
    }
  }, [anchor, width, children])

  useEffect(() => {
    const root = box.current
    const close = (event: Event) => {
      const node = event.target as Node
      if (!root?.contains(node) && !anchor?.contains(node)) onClose()
    }
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('mousedown', close)
    document.addEventListener('touchstart', close)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('touchstart', close)
      document.removeEventListener('keydown', key)
    }
  }, [anchor, onClose])

  return createPortal(<div ref={box} role="dialog" aria-label={label} tabIndex={-1} className={'cc-popover cc-popover--' + kind} style={{ top: pos.top + 'px', left: pos.left + 'px', width: pos.width + 'px', visibility: pos.ready ? 'visible' : 'hidden' } as CSSProperties}>{children}</div>, document.body)
}

function statusLabel(block: ContinuityBlock) {
  if (block.status === 'failed') return '压缩失败'
  if (block.status === 'compressing') return '压缩中'
  if (block.status === 'unavailable') return '数据不可用'
  return ''
}

function HistoryCard({ block, onOpen }: { block: ContinuityBlock; onOpen: () => void }) {
  const status = statusLabel(block)
  return <div className="cc-swipe" data-block-id={block.candidate_id}>
    <div className="cc-card">
      <button type="button" className="cc-card-main" aria-label={blockDay(block) + ' ' + timeLabel(block.start_at) + '–' + timeLabel(block.end_at) + ' ' + (status || '查看详情')} onClick={onOpen}>
        <div className="cc-card-copy">
          <div className="cc-time">{blockDay(block).slice(5).replace('-', '/')} <span>{timeLabel(block.start_at)}–{timeLabel(block.end_at)}</span></div>
          <div className="cc-stats">{formatStats(block)}</div>
        </div>
        {status && <span className={'cc-status cc-status--' + block.status}>{status}</span>}
      </button>
      {block.status === 'failed' && <div className="cc-fail-hint">压缩失败 · 原聊天保留<button type="button" className="cc-rerun" disabled>补跑</button></div>}
    </div>
  </div>
}

function HistoryDetail({ value }: { value: ContinuityBlockDetailResponse }) {
  const block = value.block
  const summary = value.chunk?.body
  return <>
    {block.status === 'complete' && <section className="cc-summary">
      <div className="cc-summary-heading">
        <span className="cc-summary-star" aria-hidden="true"><Icon name="star" /></span>
        <h3>压缩总结</h3>
        <button className="cc-icon-button" type="button" aria-label="修改总结（只读）" disabled><Icon name="edit" /></button>
      </div>
      <div className="cc-summary-glass"><p className="cc-summary-text">{summary || '暂无可读取的压缩总结。'}</p></div>
    </section>}
    {block.status === 'failed' && <div className="cc-retry"><p>这一段暂时没有压缩成功，原聊天仍完整保留。</p><button className="cc-secondary" type="button" disabled>重新压缩</button>{block.materialization_error && <p className="cc-tech-note">{block.materialization_error}</p>}</div>}
    {block.status === 'compressing' && <p className="cc-working">压缩中，正在整理这段对话…</p>}
    <h3 className="cc-original-title">完整原聊天记录</h3>
    {!block.materialization_available
      ? <p className="cc-empty-copy">原聊天仍保留，当前无法展示原文。</p>
      : <OriginalChat messages={value.messages} />}
  </>
}

export function ContextCompressionScreen() {
  const navigate = useNavigate()
  const [blocks, setBlocks] = useState<ContinuityBlock[]>([])
  const [count, setCount] = useState(0)
  const [listState, setListState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [listError, setListError] = useState('')
  const [current, setCurrent] = useState<ContinuityCurrentResponse | null>(null)
  const [currentState, setCurrentState] = useState<'loading' | 'ready' | 'unavailable'>('loading')
  const [currentError, setCurrentError] = useState('')
  const [expandedDays, setExpandedDays] = useState<string[]>([])
  const [selected, setSelected] = useState<string | 'current' | null>(null)
  const [detail, setDetail] = useState<ContinuityBlockDetailResponse | null>(null)
  const [detailState, setDetailState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [detailError, setDetailError] = useState('')
  const [panel, setPanel] = useState<'calendar' | 'settings' | null>(null)
  const [settings, setSettings] = useState<ContinuitySettingsView | null>(null)
  const [settingsState, setSettingsState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [settingsError, setSettingsError] = useState('')
  const [docked, setDocked] = useState(false)
  const timeline = useRef<HTMLDivElement>(null)
  const currentRef = useRef<HTMLDivElement>(null)
  const calendarBtn = useRef<HTMLButtonElement>(null)
  const settingsBtn = useRef<HTMLButtonElement>(null)
  const anchored = useRef(false)

  useEffect(() => {
    const controller = new AbortController()
    fetchContinuityBlocks(controller.signal).then(value => {
      setBlocks(value.blocks.slice().sort((a, b) => (a.start_at || '').localeCompare(b.start_at || '')))
      setCount(value.count)
      setListState('ready')
    }).catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setListError(error instanceof Error ? error.message : '真实压缩数据读取失败')
      setListState('error')
    })
    fetchContinuityCurrent(controller.signal).then(value => {
      if (value.available) {
        setCurrent(value)
        setCurrentState('ready')
      } else {
        setCurrent(null)
        setCurrentError(value.materialization_error || '当前块暂未接入')
        setCurrentState('unavailable')
      }
    }).catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setCurrent(null)
      setCurrentError(error instanceof Error ? error.message : '当前块暂未接入')
      setCurrentState('unavailable')
    })
    fetchContinuitySettings(controller.signal).then(value => {
      setSettings(value)
      setSettingsState('ready')
    }).catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setSettingsError(error instanceof Error ? error.message : '压缩设置读取失败')
      setSettingsState('error')
    })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!selected || selected === 'current') {
      setDetail(null)
      setDetailState(selected === 'current' ? 'ready' : 'idle')
      return
    }
    const controller = new AbortController()
    setDetail(null)
    setDetailError('')
    setDetailState('loading')
    fetchContinuityBlockDetail(selected, controller.signal).then(value => {
      setDetail(value)
      setDetailState('ready')
    }).catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setDetailError(error instanceof Error ? error.message : '详情读取失败')
      setDetailState('error')
    })
    return () => controller.abort()
  }, [selected])

  const grouped = useMemo(() => {
    const groups = new Map<string, ContinuityBlock[]>()
    blocks.forEach(block => {
      const day = blockDay(block)
      const items = groups.get(day) || []
      items.push(block)
      groups.set(day, items)
    })
    return Array.from(groups.entries()).sort(([left], [right]) => left.localeCompare(right))
  }, [blocks])
  const days = grouped.map(([day]) => day)
  const latestDay = days[days.length - 1] || dayFromTimestamp(current?.start_at || null)
  const progress = current ? Math.min(100, Math.max(0, Math.round(Math.max(current.size_progress, current.turn_progress)))) : 0
  const lifecycle = current && settingsState === 'ready' && settings ? currentLifecycle(current, settings) : null
  const currentNextLabel = current && lifecycle?.kind === 'threshold' ? '等待真实封块' : current ? nextChatLabel(current) : ''
  const currentEngine = current && settingsState === 'ready' && settings ? engineLabel(settings) : ''

  useLayoutEffect(() => {
    if (anchored.current || listState === 'loading' || currentState === 'loading') return
    const host = timeline.current
    if (!host) return
    host.scrollTop = host.scrollHeight
    anchored.current = true
  }, [listState, currentState, blocks.length])

  useEffect(() => {
    const host = timeline.current
    const target = currentRef.current
    if (!host || !target || typeof IntersectionObserver === 'undefined') {
      setDocked(false)
      return
    }
    const observer = new IntersectionObserver(entries => setDocked(!entries[0]?.isIntersecting), { root: host, threshold: 0.18 })
    observer.observe(target)
    return () => observer.disconnect()
  }, [currentState])

  function openHistory(candidateId: string) {
    setSelected(candidateId)
  }

  function closeDetail() {
    setSelected(null)
  }

  function jump(day: string) {
    setPanel(null)
    setExpandedDays(value => value.includes(day) ? value : value.concat(day))
    window.setTimeout(() => timeline.current?.querySelector('[data-day="' + day + '"]')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 70)
  }

  const today = dayFromTimestamp(current?.start_at || null)
  const calendarMatch = /^(\d{4})-(\d{2})-/.exec(today || latestDay)
  const calendarYear = calendarMatch ? Number(calendarMatch[1]) : new Date().getFullYear()
  const calendarMonth = calendarMatch ? Number(calendarMatch[2]) : new Date().getMonth() + 1
  const monthLength = new Date(calendarYear, calendarMonth, 0).getDate()
  const mondayOffset = (new Date(calendarYear, calendarMonth - 1, 1).getDay() + 6) % 7

  return <main className="dash-fullscreen-page cc-page">
    <div className="cc-heading">
      <PageHeader title="上下文压缩" onBack={() => navigate(ROUTES.chat)} backLabel="返回" aside={<div className="cc-toolbar">
        <button ref={calendarBtn} type="button" className={'cc-icon-button' + (panel === 'calendar' ? ' cc-icon-button--open' : '')} aria-label="日历" aria-expanded={panel === 'calendar'} aria-haspopup="dialog" onClick={() => setPanel(value => value === 'calendar' ? null : 'calendar')}><Icon name="calendar" /></button>
        <button ref={settingsBtn} type="button" className={'cc-icon-button' + (panel === 'settings' ? ' cc-icon-button--open' : '')} aria-label="设置" aria-expanded={panel === 'settings'} aria-haspopup="dialog" onClick={() => setPanel(value => value === 'settings' ? null : 'settings')}><Icon name="settings" /></button>
      </div>} />
    </div>
    <div className="cc-timeline" ref={timeline} aria-label="压缩时间线">
      <div className="cc-timeline-intro"><span>原聊天会一直保留，随时可以回看</span></div>
      {listState === 'loading' && <div className="cc-data-state" role="status">正在读取真实压缩历史…</div>}
      {listState === 'error' && <div className="cc-data-state cc-data-state--error" role="alert">真实压缩历史读取失败：{listError}</div>}
      {listState === 'ready' && !blocks.length && <div className="cc-data-state">暂时没有可显示的压缩历史。</div>}
      {grouped.map(([day, items], groupIndex) => {
        const old = groupIndex < grouped.length - 3
        const expanded = !old || expandedDays.includes(day)
        const failed = items.filter(block => block.status === 'failed' || block.status === 'unavailable').length
        const unavailableCharacterCount = items.filter(block => block.original_char_count === null).length
        const knownCharacterCount = items.reduce((sum, block) => sum + (block.original_char_count ?? 0), 0)
        const characterSummary = unavailableCharacterCount === items.length ? '' : formatNumber(knownCharacterCount) + ' 字'
        return <section key={day} data-day={day} className="cc-day">
          {old
            ? <button className="cc-day-fold" aria-expanded={expanded} onClick={() => setExpandedDays(value => value.includes(day) ? value.filter(item => item !== day) : value.concat(day))}>
              <div className="cc-day-fold-top"><span>{dayLabel(day)}</span><span aria-hidden="true">{expanded ? '−' : '+'}</span></div>
              <div className="cc-stats">{items.length} 个压缩块 · {formatNumber(items.reduce((sum, block) => sum + block.completed_turn_count, 0))} 轮{characterSummary ? ' · ' + characterSummary : ''}</div>
              {failed > 0 && <div className="cc-failure-note">有 {failed} 个压缩失败</div>}
            </button>
            : <h2 className="cc-day-label">{day === latestDay ? '最近' : dayLabel(day)}<span>{dayLabel(day)}</span><i /></h2>}
          {expanded && <div className="cc-day-blocks">{items.map(block => <HistoryCard key={block.candidate_id} block={block} onOpen={() => openHistory(block.candidate_id)} />)}</div>}
        </section>
      })}
      <section className="cc-day cc-current-section">
        {currentState === 'loading' && <div className="cc-data-state" role="status">正在读取当前块…</div>}
        {currentState === 'unavailable' && <div className="cc-current cc-current--unavailable"><span className="cc-current-caption"><span className="cc-current-dot" />正在聊的这一段</span><strong>当前块暂未接入</strong><p>{currentError}</p><button className="cc-primary cc-early" type="button" disabled><Icon name="leaf" />提前压缩当前块</button></div>}
        {currentState === 'ready' && current && <div ref={currentRef} className="cc-current">
          <button className="cc-current-open" type="button" onClick={() => setSelected('current')}>
            <span className="cc-current-caption"><span className="cc-current-dot" />正在聊的这一段</span>
            <span className="cc-time">{dayFromTimestamp(current.start_at).slice(5).replace('-', '/')} <span>{timeLabel(current.start_at)}–{timeLabel(current.end_at) === '—' ? '现在' : timeLabel(current.end_at)}</span></span>
            <span className="cc-stats">{currentStats(current)}</span>
          </button>
          {lifecycle && <div className={'cc-current-state cc-current-state--' + lifecycle.kind} role="status">
            <strong>{lifecycle.label}</strong>
            <span>{lifecycle.detail}</span>
          </div>}
          <div className="cc-progress-label"><span>距离下次压缩</span><strong>{progress}%</strong></div>
          <div className="cc-progress-track" role="progressbar" aria-label="距离下次压缩" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}><span style={{ width: progress + '%' }} /></div>
          <p className="cc-next">{currentNextLabel}{currentEngine && currentNextLabel.indexOf('约再聊') === 0 ? <span className="cc-engine"> · 当前引擎 {currentEngine}</span> : null}</p>
          <button className="cc-primary cc-early" type="button" disabled><Icon name="leaf" />提前压缩当前块</button>
        </div>}
      </section>
      {listState === 'ready' && <p className="cc-count-note">真实压缩历史共 {count} 块</p>}
    </div>
    {current && docked && !panel && !selected && <button type="button" className="cc-dock" onClick={() => currentRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })}>当前块 · {progress}% · {currentNextLabel}</button>}
    {selected && <Sheet label={selected === 'current' ? '当前聊天记录' : '压缩块详情'} onClose={closeDetail}>
      <div className="cc-sheet-heading"><div><span className="cc-eyebrow">{selected === 'current' && current ? dayFromTimestamp(current.start_at).replace(/-/g, '/') + ' · ' + timeLabel(current.start_at) + '–现在' : detail ? blockDay(detail.block).replace(/-/g, '/') + ' · ' + timeLabel(detail.block.start_at) + '–' + timeLabel(detail.block.end_at) : '正在读取'}</span><h2>{selected === 'current' ? '当前聊天记录' : '这一段对话'}</h2></div><button className="cc-icon-button" type="button" aria-label="关闭详情" onClick={closeDetail}><Icon name="close" /></button></div>
      <div className="cc-detail-scroll">
        {selected === 'current' && current && <><h3 className="cc-original-title">原聊天记录</h3><OriginalChat messages={current.messages} /></>}
        {selected !== 'current' && detailState === 'loading' && <div className="cc-data-state" role="status">正在读取真实块详情…</div>}
        {selected !== 'current' && detailState === 'error' && <div className="cc-data-state cc-data-state--error" role="alert">详情读取失败：{detailError}</div>}
        {selected !== 'current' && detailState === 'ready' && detail && <HistoryDetail value={detail} />}
      </div>
    </Sheet>}
    {panel === 'settings' && <Popover anchor={settingsBtn.current} kind="settings" width={324} label="压缩设置" onClose={() => setPanel(null)}>
      {settingsState === 'loading' && <div className="cc-settings-state" role="status">正在读取真实压缩设置…</div>}
      {settingsState === 'error' && <div className="cc-settings-state cc-settings-state--error" role="alert">设置读取失败：{settingsError}</div>}
      {settingsState === 'ready' && settings && <CompressionSettings value={settings} onClose={() => setPanel(null)} onSave={async next => {
        const saved = await saveContinuitySettings(next)
        setSettings(saved)
        return saved
      }} />}
    </Popover>}
    {panel === 'calendar' && <Popover anchor={calendarBtn.current} kind="calendar" width={312} label="压缩日历" onClose={() => setPanel(null)}>
      <p className="cc-popover-title">{calendarYear} 年 {calendarMonth} 月</p>
      <div className="cc-calendar"><div className="cc-calendar-grid">
        {['一', '二', '三', '四', '五', '六', '日'].map(day => <span className="cc-weekday" key={day}>{day}</span>)}
        {Array.from({ length: mondayOffset }, (_, index) => <span key={'offset-' + index} />)}
        {Array.from({ length: monthLength }, (_, index) => index + 1).map(dayNumber => {
          const day = String(calendarYear) + '-' + String(calendarMonth).padStart(2, '0') + '-' + String(dayNumber).padStart(2, '0')
          const items = blocks.filter(block => blockDay(block) === day)
          const failed = items.some(block => block.status === 'failed' || block.status === 'unavailable')
          const included = items.some(block => Boolean(block.included))
          return <button className={'cc-calendar-day' + (day === today ? ' cc-calendar-today' : '')} key={day} disabled={!items.length} aria-label={calendarMonth + '月' + dayNumber + '日' + (included ? ' 有块在上下文' : '') + (failed ? ' 有压缩失败' : '')} onClick={() => jump(day)}><span>{dayNumber}</span><span className="cc-calendar-dots">{included && <i className="cc-dot-pink" />}{failed && <i className="cc-dot-red" />}</span></button>
        })}
      </div><div className="cc-calendar-legend"><span><i className="cc-dot-pink" />在上下文里</span><span><i className="cc-dot-red" />压缩失败</span></div></div>
    </Popover>}
  </main>
}

type CurrentLifecycle = {
  kind: 'waiting' | 'threshold'
  label: string
  detail: string
}

function currentLifecycle(current: ContinuityCurrentResponse, settings: ContinuitySettingsView): CurrentLifecycle | null {
  if (current.processing_state === 'threshold_reached' || current.threshold_reached) {
    return {
      kind: 'threshold',
      label: '已达到压缩阈值',
      detail: settings.pendingRevision
        ? '等待真实封块完成后切换到 ' + settings.pendingRevision
        : '等待后端处理真实封块',
    }
  }
  if (settings.pendingRevision && current.processing_state === 'waiting') {
    return {
      kind: 'waiting',
      label: 'WAITING · 等待达到阈值',
      detail: '当前块继续使用 ' + (current.settings_revision_id || settings.currentBlockRevision) + '；下一块使用 ' + settings.pendingRevision,
    }
  }
  return null
}
