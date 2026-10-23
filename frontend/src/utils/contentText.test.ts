import { describe, expect, it } from 'vitest'
import { canSeek, summaryTimelineTime, textParts, timeSeconds } from './contentText'
describe('内容原文与定位', () => {
  it('保留原文并合并重叠高亮，内容不会解释成 HTML', () => {
    const text = '<img>中文表达'
    const parts = textParts(text, [[5, 7], [6, 9], [-5, 2]])
    expect(parts.map(p => p.text).join('')).toBe(text)
    expect(parts.filter(p => p.hit).map(p => p.text)).toEqual(['<i', '中文表达'])
  })
  it('支持零点、时间字符串、小时和范围，并拒绝错误', () => {
    expect(timeSeconds(0)).toBe(0); expect(timeSeconds('01:02:03')).toBe(3723)
    expect(timeSeconds('约 02:10—02:30')).toBe(130)
    expect(timeSeconds('1:90')).toBeNull(); expect(timeSeconds(-1)).toBeNull()
    expect(timeSeconds(Infinity)).toBeNull()
  })
  it('不跳转历史不可靠分段时间和超出片长的时间', () => {
    expect(canSeek(0, 'model_estimated', 20)).toBe(true)
    expect(canSeek(21, 'model_estimated', 20)).toBe(false)
    expect(canSeek(1, 'legacy_chunk_uncertain', 20)).toBe(false)
  })
  it('高亮与摘录使用浏览器 UTF-16 偏移，表情之后不偏移', () => {
    const text = '🎬一句中文金句'
    expect(textParts(text, [[2, 6]]).filter(p => p.hit).map(p => p.text)).toEqual(['一句中文'])
    expect(text.slice(2, 6)).toBe('一句中文')
  })
  it('旧分段总结不启用估计偏移，新的全片依据仅映射一次', () => {
    const old = summaryTimelineTime({ time: 0, content: '开头' }, 0, { processingMode: 'chunked' })
    expect(old.seconds).toBe(0); expect(canSeek(old.seconds, old.basis, 120)).toBe(false)
    const mapped = summaryTimelineTime({ time_seconds: 65, time_basis: 'absolute_model_estimated', content: '第二段' }, 1, { processingMode: 'chunked' })
    expect(mapped.seconds).toBe(65); expect(canSeek(mapped.seconds, mapped.basis, 120)).toBe(true)
  })
  it('遵从后端对来源时间的禁用，不能以旧原值绕过', () => {
    const value = summaryTimelineTime({ time: 25, content: '错误' }, 0, {}, [{ source_type: 'summary', field_path: 'timeline.0.content', text: '错误', time_seconds: null, time_basis: 'unknown' }])
    expect(value.seconds).toBeNull(); expect(canSeek(value.seconds, value.basis, 60)).toBe(false)
  })
})
