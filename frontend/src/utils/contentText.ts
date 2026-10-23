/** 使用文本节点高亮，偏移基于原文，绝不把来源内容解释为 HTML。 */
export function textParts(text: string, spans: number[][] = []) {
  const ranges = spans.filter(s => s.length >= 2 && Number.isFinite(s[0]) && Number.isFinite(s[1]) && s[1] > s[0])
    .map(s => [Math.max(0, s[0]), Math.min(text.length, s[1])]).sort((a, b) => a[0] - b[0])
  const merged: number[][] = []
  for (const range of ranges) {
    const previous = merged[merged.length - 1]
    if (previous && range[0] <= previous[1]) previous[1] = Math.max(previous[1], range[1])
    else merged.push(range)
  }
  const parts: Array<{ text: string; hit: boolean }> = []; let cursor = 0
  for (const [start, end] of merged) {
    if (end <= start) continue
    if (start > cursor) parts.push({ text: text.slice(cursor, start), hit: false })
    parts.push({ text: text.slice(start, end), hit: true }); cursor = end
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false })
  return parts
}

export function timeSeconds(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) && value >= 0 ? value : null
  if (typeof value !== 'string' || !value.trim()) return null
  const first = value.trim().replace(/^约\s*/, '').split(/\s*(?:—|–|~|至|到|-)\s*/)[0]
  if (/^\d+(?:\.\d+)?$/.test(first)) return Number(first)
  const pieces = first.split(':')
  if (pieces.length < 2 || pieces.length > 3 || pieces.some(p => !/^\d+(?:\.\d+)?$/.test(p))) return null
  if (pieces.slice(1).some(p => Number(p) >= 60)) return null
  return pieces.reduce((sum, p) => sum * 60 + Number(p), 0)
}
export function timeLabel(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '无时间信息'
  const seconds = Math.floor(value); const hour = Math.floor(seconds / 3600)
  return `${hour ? `${hour}:` : ''}${String(Math.floor(seconds / 60) % 60).padStart(hour ? 2 : 1, '0')}:${String(seconds % 60).padStart(2, '0')}`
}
export function canSeek(time: unknown, basis?: string, duration?: number | null) {
  const seconds = timeSeconds(time)
  if (seconds == null || /unknown|uncertain|legacy_chunk|unreliable/.test(basis || '')) return false
  return duration == null || !Number.isFinite(duration) || seconds <= duration
}

/** 旧总结以保守依据补齐时间；后端已禁用的分段时间不能在前端重新启用。 */
export function summaryTimelineTime(item: unknown, index: number, options: { processingMode?: string | null; timeBasis?: string }, sources: Array<{ source_type: string; field_path?: string; text: string; time_seconds?: number | null; time_basis?: string }> = []) {
  if (!item || typeof item !== 'object') return { seconds: null, basis: 'none' }
  const entry = item as Record<string, unknown>
  const values = [entry.content, entry.visual, entry.action, entry.audio, entry.significance].filter(value => typeof value === 'string')
  const source = sources.find(value => value.source_type === 'summary' && value.field_path?.startsWith(`timeline.${index}.`) && values.includes(value.text))
  if (source) return { seconds: timeSeconds(source.time_seconds), basis: source.time_basis || 'unknown' }
  const basis = typeof entry.time_basis === 'string' ? entry.time_basis : options.timeBasis || (/chunked|segment/.test(options.processingMode || '') ? 'unknown_segment_basis' : 'model_estimated')
  return { seconds: timeSeconds(entry.time_seconds ?? entry.time), basis }
}
