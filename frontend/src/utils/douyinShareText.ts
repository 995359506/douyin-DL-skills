export type DouyinUrlKind = 'video' | 'user' | 'any'
export type DouyinParseStatus = 'empty' | 'none' | 'single' | 'multiple' | 'wrong-type'

export interface DouyinShareParseResult {
  status: DouyinParseStatus
  url?: string
  urls: string[]
  message: string
}

// Stop at Markdown link delimiters as well as prose punctuation. Without this
// guard, `[https://v.douyin.com/x](https://v.douyin.com/x)` can be swallowed as
// one malformed URL and later look like a different resource to the backend.
const URL_PATTERN = /https?:\/\/[^\s<>"'`()[\]\u3000，。！？；：、（）【】《》“”‘’「」『』]+/giu
const ALWAYS_TRAILING_PUNCTUATION = /[，。！？；：、…]$/u
const ASCII_TRAILING_PUNCTUATION = /[.,!;:]$/u
const UNSAFE_SCHEME_PREFIX = /(?:javascript|data|vbscript):[^\s]*$/iu

function trimUnmatchedClosingPair(value: string, open: string, close: string): string {
  let result = value
  while (result.endsWith(close)) {
    const opens = [...result].filter((character) => character === open).length
    const closes = [...result].filter((character) => character === close).length
    if (closes <= opens) break
    result = result.slice(0, -close.length)
  }
  return result
}

/** Remove prose delimiters while leaving the URL path, fragment and query intact. */
export function trimShareUrlPunctuation(value: string): string {
  let result = value.trim()
  let previous = ''

  while (result && result !== previous) {
    previous = result
    result = trimUnmatchedClosingPair(result, '(', ')')
    result = trimUnmatchedClosingPair(result, '[', ']')
    result = trimUnmatchedClosingPair(result, '{', '}')

    if (ALWAYS_TRAILING_PUNCTUATION.test(result)) {
      result = result.slice(0, -1)
      continue
    }
    if (ASCII_TRAILING_PUNCTUATION.test(result)) {
      result = result.slice(0, -1)
      continue
    }
    // A lone trailing question mark is prose punctuation or an empty query.
    if (result.endsWith('?') && result.indexOf('?') === result.length - 1) {
      result = result.slice(0, -1)
    }
  }

  return result
}

function isAllowedDouyinHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/\.$/, '')
  return host === 'douyin.com' || host.endsWith('.douyin.com')
}

function normalizeDouyinUrl(candidate: string): string | null {
  try {
    const parsed = new URL(trimShareUrlPunctuation(candidate))
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
    const normalizedHost = parsed.hostname.toLowerCase().replace(/\.$/, '')
    if (!isAllowedDouyinHost(normalizedHost)) return null
    if (parsed.username || parsed.password) return null
    if (parsed.port && !(
      (parsed.protocol === 'http:' && parsed.port === '80')
      || (parsed.protocol === 'https:' && parsed.port === '443')
    )) return null
    parsed.hostname = normalizedHost
    return parsed.toString()
  } catch {
    return null
  }
}

function matchesKind(url: string, kind: DouyinUrlKind): boolean {
  if (kind === 'any') return true
  const parsed = new URL(url)
  const path = parsed.pathname.toLowerCase()

  // Douyin short links redirect to their final resource, so they are valid in
  // both entry points and the backend performs the final metadata validation.
  if (parsed.hostname.toLowerCase() === 'v.douyin.com') return true
  if (kind === 'user') return /^\/user(?:\/|$)/u.test(path)
  return /^\/(?:video|note)(?:\/|$)/u.test(path) || /^\/share\/(?:video|note)(?:\/|$)/u.test(path)
}

function extractValidDouyinUrls(text: string): string[] {
  const urls: string[] = []
  for (const match of text.matchAll(URL_PATTERN)) {
    const index = match.index ?? 0
    const prefix = text.slice(Math.max(0, index - 64), index)
    if (UNSAFE_SCHEME_PREFIX.test(prefix)) continue

    const normalized = normalizeDouyinUrl(match[0])
    if (normalized && !urls.includes(normalized)) urls.push(normalized)
  }
  return urls
}

/**
 * Parse Douyin share prose without performing network requests.
 *
 * Multiple distinct valid Douyin URLs are rejected before entry-type filtering
 * so the single-resource UI never silently selects the wrong link.
 */
export function parseDouyinShareText(
  input: string,
  kind: DouyinUrlKind = 'any'
): DouyinShareParseResult {
  const text = (input || '').trim()
  if (!text) return { status: 'empty', urls: [], message: '' }

  const urls = extractValidDouyinUrls(text)
  if (urls.length === 0) {
    return {
      status: 'none',
      urls: [],
      message: '没有识别到抖音链接，请重新复制分享内容'
    }
  }
  if (urls.length > 1) {
    return {
      status: 'multiple',
      urls,
      message: `检测到 ${urls.length} 个抖音链接，单次只支持一个，请保留一个后重试`
    }
  }

  const [url] = urls
  if (!matchesKind(url, kind)) {
    return {
      status: 'wrong-type',
      urls,
      message: kind === 'user'
        ? '识别到的不是用户主页链接，请重新复制用户主页分享内容'
        : '识别到的是用户主页链接，请粘贴视频分享内容'
    }
  }

  return {
    status: 'single',
    url,
    urls,
    message: kind === 'user' ? '已识别用户主页链接' : '已识别视频链接'
  }
}
