import { describe, expect, it } from 'vitest'
import { normalizeCommentIdeas, commentIdeasForCopy } from './videoSummaryComments'

describe('神评论灵感', () => {
  it('兼容旧总结缺失的字段和异常形状', () => {
    for (const value of [undefined, null, '', {}, { text: '文案' }]) {
      expect(normalizeCommentIdeas(value)).toEqual([])
    }
    expect(commentIdeasForCopy([])).toBe('')
  })

  it('只保留风格明确且正文有效的灵感，不把对象渲染成文案', () => {
    const original = { style: '搞笑有梗', text: '  生活给我出题，我给生活捧哏。  ' }
    const input = [
      null,
      '字符串',
      { style: '其他', text: '不支持的风格' },
      { style: '金句', text: { content: '错误形状' } },
      { style: '金句', text: '  ' },
      original,
      { style: '抒情共鸣', text: '原来想念，是记忆替我们亮着灯。' },
      { style: '金句', text: '把普通日子过成自己的答案。' },
    ]
    const ideas = normalizeCommentIdeas(input)

    expect(ideas).toEqual([
      { style: '搞笑有梗', text: '生活给我出题，我给生活捧哏。' },
      { style: '抒情共鸣', text: '原来想念，是记忆替我们亮着灯。' },
      { style: '金句', text: '把普通日子过成自己的答案。' },
    ])
    expect(original.text).toBe('  生活给我出题，我给生活捧哏。  ')
    expect(commentIdeasForCopy(ideas)).toBe('神评论灵感\n【搞笑有梗】生活给我出题，我给生活捧哏。\n【抒情共鸣】原来想念，是记忆替我们亮着灯。\n【金句】把普通日子过成自己的答案。')
  })
})
