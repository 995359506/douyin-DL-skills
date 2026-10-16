export type UniversalRouteHint = 'video' | 'user' | 'mix' | 'uncertain'

/**
 * The universal entry is intentionally conservative for short links. A
 * v.douyin.com URL is usually a shared video, but its final resource type is
 * hidden behind a redirect. Keep it as an explicit user choice until the
 * backend confirms a real multi-video collection.
 */
export function classifyUniversalUrl(url: string): UniversalRouteHint {
  try {
    const parsed = new URL(url)
    const host = parsed.hostname.toLowerCase()
    const path = parsed.pathname.toLowerCase()

    if (host === 'v.douyin.com') return 'uncertain'
    if (/^\/user(?:\/|$)/u.test(path)) return 'user'
    if (/\/(?:mix|collection|playlist)(?:\/|$)/u.test(path) || parsed.searchParams.has('mix_id')) return 'mix'
    return 'video'
  } catch {
    return 'uncertain'
  }
}

/** Detect the wording Douyin uses when sharing a user's profile. */
export function hasUserProfileShareIntent(shareText: string): boolean {
  const normalized = shareText
    .normalize('NFKC')
    .replace(/[\s\u200b]+/gu, '')
    .toLowerCase()

  return /(?:查看|访问|进入|打开|前往|看看|浏览)(?:ta|他|她)?(?:的)?(?:抖音)?(?:个人)?(?:主页|更多作品|全部作品)/u.test(normalized)
}

/** Share-copy hints may disambiguate short links, but never override explicit URLs. */
export function classifyUniversalInput(url: string, shareText: string): UniversalRouteHint {
  const urlHint = classifyUniversalUrl(url)
  if (urlHint !== 'uncertain') return urlHint
  return hasUserProfileShareIntent(shareText) ? 'user' : urlHint
}

function readPositiveCount(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string') return null

  const normalized = value.trim()
  if (!/^\d+(?:\.\d+)?$/u.test(normalized)) return null
  const count = Number(normalized)
  return Number.isFinite(count) ? count : null
}

/**
 * A collection is a confirmed route only when both pieces of evidence exist:
 * a collection ID and an explicit item count greater than one. Values such as
 * "查看详情", "数量未知" or an absent count are deliberately not accepted.
 */
export function hasConfirmedMultiVideoMix(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const record = data as Record<string, unknown>
  const mixId = String(record.mix_id || '').trim()
  if (!mixId) return false

  const countKeys = ['total_videos', 'item_count', 'video_count', 'total_count', 'count']
  const count = countKeys
    .map((key) => readPositiveCount(record[key]))
    .find((value): value is number => value !== null)

  return count !== undefined && count > 1
}
