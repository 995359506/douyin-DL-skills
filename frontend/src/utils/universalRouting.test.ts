import { describe, expect, it } from 'vitest'
import {
  classifyUniversalInput,
  classifyUniversalUrl,
  hasConfirmedMultiVideoMix,
  hasUserProfileShareIntent
} from './universalRouting'

describe('universal entry routing', () => {
  it('keeps v.douyin.com short links as a conservative candidate', () => {
    expect(classifyUniversalUrl('https://v.douyin.com/U2YDJMBT6vM/')).toBe('uncertain')
  })

  it('uses profile share wording to route an otherwise uncertain short link to profile verification', () => {
    const shareText = '长按复制此条消息，打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/jYqZIlR0TIA/\u00a0'
    expect(hasUserProfileShareIntent(shareText)).toBe(true)
    expect(classifyUniversalInput('https://v.douyin.com/jYqZIlR0TIA/', shareText)).toBe('user')
  })

  it('does not infer a profile from ordinary video share wording or override explicit video URLs', () => {
    const videoShare = '复制此链接，打开Dou音搜索，直接观看视频！'
    expect(hasUserProfileShareIntent(videoShare)).toBe(false)
    expect(classifyUniversalInput('https://v.douyin.com/U2YDJMBT6vM/', videoShare)).toBe('uncertain')
    expect(classifyUniversalInput('https://www.douyin.com/video/7420000012345678901', '查看TA的更多作品')).toBe('video')
  })

  it('preserves explicit user and collection URL routing', () => {
    expect(classifyUniversalUrl('https://www.douyin.com/user/MS4wLjABAAAA_mock')).toBe('user')
    expect(classifyUniversalUrl('https://www.douyin.com/collection/123456789')).toBe('mix')
    expect(classifyUniversalUrl('https://www.douyin.com/video/7420000012345678901')).toBe('video')
  })

  it('requires a mix id and an explicit count greater than one', () => {
    expect(hasConfirmedMultiVideoMix({ mix_id: '12345', total_videos: 2 })).toBe(true)
    expect(hasConfirmedMultiVideoMix({ mix_id: '12345', total_videos: '2' })).toBe(true)
    expect(hasConfirmedMultiVideoMix({ mix_id: '12345', total_videos: 1 })).toBe(false)
    expect(hasConfirmedMultiVideoMix({ mix_id: '12345', total_videos: '查看详情' })).toBe(false)
    expect(hasConfirmedMultiVideoMix({ mix_id: '', total_videos: 10 })).toBe(false)
  })
})
