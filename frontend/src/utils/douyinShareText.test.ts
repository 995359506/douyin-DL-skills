import { describe, expect, it } from 'vitest'
import { parseDouyinShareText } from './douyinShareText'

const SAMPLE_SHARE_TEXT = String.raw`2.02 OKw:/ :5pm 06/29 H\@i.Ch 阿姑 撒呀 看母鸡干撒呀 # 抽象  https://v.douyin.com/u3hEhoFhbmQ/ 复制此链接，打开Dou音搜索，直接观看视频！`

describe('parseDouyinShareText', () => {
  it('extracts the complete provided share-text sample without requesting it', () => {
    expect(parseDouyinShareText(SAMPLE_SHARE_TEXT, 'video')).toMatchObject({
      status: 'single',
      url: 'https://v.douyin.com/u3hEhoFhbmQ/'
    })
  })

  it.each([
    ['pure short link', 'https://v.douyin.com/AbCd123/', 'https://v.douyin.com/AbCd123/'],
    ['standard video link', 'https://www.douyin.com/video/7420000012345678901', 'https://www.douyin.com/video/7420000012345678901'],
    ['Markdown link', '[直接观看](https://v.douyin.com/Markdown123/)', 'https://v.douyin.com/Markdown123/'],
    ['Chinese punctuation', '链接：https://v.douyin.com/Punctuation1/。', 'https://v.douyin.com/Punctuation1/'],
    ['English punctuation', 'Watch https://v.douyin.com/Punctuation2/!', 'https://v.douyin.com/Punctuation2/'],
    ['line breaks', '第一行\nhttps://v.douyin.com/LineBreak1/\n复制打开抖音', 'https://v.douyin.com/LineBreak1/'],
    ['query parameters', 'https://www.douyin.com/video/7420000012345678901?previous_page=app_code_link&enter_method=share', 'https://www.douyin.com/video/7420000012345678901?previous_page=app_code_link&enter_method=share']
  ])('supports %s', (_name, input, expected) => {
    expect(parseDouyinShareText(input, 'video')).toMatchObject({
      status: 'single',
      url: expected
    })
  })

  it('extracts a URL from a Markdown label and target without retaining ]( delimiters', () => {
    const input = '[https://v.douyin.com/U2YDJMBT6vM/](https://v.douyin.com/U2YDJMBT6vM/) 复制此链接，打开抖音搜索，直接观看视频！'
    expect(parseDouyinShareText(input, 'any')).toMatchObject({
      status: 'single',
      url: 'https://v.douyin.com/U2YDJMBT6vM/'
    })
  })

  it('returns an inline no-link error', () => {
    expect(parseDouyinShareText('这里只有标题、时间和话题，没有链接', 'video')).toEqual({
      status: 'none',
      urls: [],
      message: '没有识别到抖音链接，请重新复制分享内容'
    })
  })

  it('does not silently select one of multiple valid links', () => {
    const result = parseDouyinShareText(
      'https://v.douyin.com/First123/\nhttps://www.douyin.com/video/7420000012345678901',
      'video'
    )
    expect(result.status).toBe('multiple')
    expect(result.urls).toHaveLength(2)
  })

  it.each([
    'https://douyin.com.evil.test/video/12345',
    'https://evildouyin.com/video/12345',
    'javascript:https://v.douyin.com/Unsafe1/',
    'data:text/html,https://v.douyin.com/Unsafe2/',
    'ftp://v.douyin.com/Unsafe3/'
  ])('rejects a disguised domain or unsafe protocol: %s', (input) => {
    expect(parseDouyinShareText(input, 'video').status).toBe('none')
  })

  it('reuses entry-type validation for user profiles and video entries', () => {
    const userUrl = 'https://www.douyin.com/user/MS4wLjABAAAA_mock'
    expect(parseDouyinShareText(`用户主页：${userUrl}`, 'user').status).toBe('single')
    expect(parseDouyinShareText(userUrl, 'video').status).toBe('wrong-type')
    expect(
      parseDouyinShareText('https://www.douyin.com/video/7420000012345678901', 'user').status
    ).toBe('wrong-type')
  })

  it('deduplicates the same normalized URL instead of reporting a false multiple', () => {
    const url = 'https://v.douyin.com/Same123/'
    expect(parseDouyinShareText(`${url} ${url}`, 'video')).toMatchObject({
      status: 'single',
      url
    })
  })
})
