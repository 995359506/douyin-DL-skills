/**
 * 从分享文案中提取合法的抖音链接。
 * 只接受 http/https 与 douyin.com 本身或其真实子域。
 */
export const extractDouyinUrls = (text: string): string[] => {
  const matches = text.match(/https?:\/\/[^\s]+/gi) || []
  return matches
    .map((value) => value.replace(/[，。！？；、）》\]}>”'"`]+$/g, ''))
    .filter((value) => {
      try {
        const url = new URL(value)
        return (
          (url.protocol === 'http:' || url.protocol === 'https:') &&
          (url.hostname === 'douyin.com' || url.hostname.endsWith('.douyin.com'))
        )
      } catch {
        return false
      }
    })
}
