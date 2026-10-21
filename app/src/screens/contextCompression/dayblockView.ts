import type { ContinuityBlock, DayBlockSummary } from '../../lib/contextCompression'

export function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

export function formatLogicalSize(value: number | null) {
  if (value === null) return '—'
  if (Math.abs(value) >= 1000) return (value / 1000).toFixed(1) + 'k'
  return formatNumber(value)
}

export function dayBlockStatusLabel(dayblock: DayBlockSummary) {
  if (dayblock.status === 'ready') return ''
  if (dayblock.status === 'rejected') return dayblock.reason === 'refusal_text' ? '未采用 · 拒绝稿' : '未采用'
  if (dayblock.status === 'failed') return '生成失败'
  if (dayblock.status === 'processing') return '生成中'
  if (dayblock.status === 'stale') return '已过期'
  return '未生成'
}

export function dayBlockStatusTone(dayblock: DayBlockSummary) {
  if (dayblock.status === 'processing') return 'compressing'
  if (dayblock.status === 'rejected' || dayblock.status === 'failed') return 'failed'
  return 'unavailable'
}

export function dayBlockReasonCopy(dayblock: DayBlockSummary) {
  if (dayblock.reason === 'refusal_text') return '模型这一天拒绝了写日块'
  if (dayblock.reason === 'body_too_short') return '日块正文过短'
  if (dayblock.reason === 'body_hash_mismatch') return '日块正文校验失败'
  return dayblock.reason || ''
}

/** 日块 · 7 个压缩块 · 110 轮 · 1.6k — size is the DayBlock's own token estimate. */
export function dayBlockStats(dayblock: DayBlockSummary, items: ContinuityBlock[]) {
  const turns = dayblock.completed_turn_count ?? items.reduce((sum, block) => sum + block.completed_turn_count, 0)
  const parts = ['日块', items.length + ' 个压缩块', formatNumber(turns) + ' 轮']
  if (dayblock.output_token_estimate !== null) parts.push(formatLogicalSize(dayblock.output_token_estimate))
  return parts.join(' · ')
}
