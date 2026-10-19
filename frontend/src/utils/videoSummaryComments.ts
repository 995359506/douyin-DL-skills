export interface CommentIdea {
  style: '搞笑有梗' | '抒情共鸣' | '金句'
  text: string
}

export function normalizeCommentIdeas(value: unknown): CommentIdea[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(idea => {
    if (!idea || typeof idea !== 'object') return []
    const { style, text } = idea
    if (style !== '搞笑有梗' && style !== '抒情共鸣' && style !== '金句') return []
    if (typeof text !== 'string' || !text.trim()) return []
    return [{ style, text: text.trim() }]
  })
}

export function commentIdeasForCopy(ideas: CommentIdea[]): string {
  return ideas.length ? `神评论灵感\n${ideas.map(idea => `【${idea.style}】${idea.text}`).join('\n')}` : ''
}
