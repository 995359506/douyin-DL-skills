import { describe, expect, it } from 'vitest'
import { downloadBatchCountdown, downloadBatchNextLabel, downloadBatchProgressValue, downloadBatchSkipLabel, downloadBatchStatusLabel, type DownloadBatch } from './downloadBatchQueue'

const batch: DownloadBatch = {
  batch_id: 'batch-1', author: '作者', profile_id: 'profile-1', status: 'running',
  total: 3, completed: 1, failed: 0, pending: 2, cancelled: 0, interval_seconds: 180,
}

describe('后台下载队列提示', () => {
  it('显示所有后端状态，保留暂停与停止的区别', () => {
    expect(['queued', 'running', 'paused', 'completed', 'cancelled'].map(status => downloadBatchStatusLabel(status as DownloadBatch['status'])))
      .toEqual(['排队中', '下载中', '已暂停', '已完成', '已停止'])
  })

  it('使用后端截止时间逐秒倒计时，不出现负数', () => {
    expect(downloadBatchCountdown({ ...batch, next_allowed_at: 280 }, 100_000, 90_000)).toBe(180)
    expect(downloadBatchNextLabel({ ...batch, next_allowed_at: 280 }, 221_000, 90_000)).toBe('下一条：0 分 59 秒后')
    expect(downloadBatchCountdown({ ...batch, next_allowed_at: 280 }, 300_000, 90_000)).toBe(0)
    expect(downloadBatchNextLabel({ ...batch, next_allowed_at: 280 }, 300_000, 90_000)).toBe('等待后台调度下一条')
  })

  it('兼容服务返回剩余秒数，并从获取时刻递减', () => {
    expect(downloadBatchCountdown({ ...batch, next_in_seconds: 180 }, 110_000, 100_000)).toBe(170)
  })

  it('显示跳过数量，兼容未返回跳过字段的旧批次', () => {
    expect(downloadBatchSkipLabel(batch)).toBe('已下载跳过 0')
    expect(downloadBatchSkipLabel({ ...batch, skipped: 2 })).toBe('已下载跳过 2')
  })

  it('已完成包含本地跳过条目，进度不重复累加', () => {
    expect(downloadBatchProgressValue({ ...batch, total: 5, completed: 3, skipped: 2, failed: 1, cancelled: 1 })).toBe(5)
    expect(downloadBatchProgressValue(batch)).toBe(1)
  })

  it('当前视频与暂停状态不显示误导性下载倒计时', () => {
    expect(downloadBatchNextLabel({ ...batch, current_task_id: 'task-1' }, 0, 0)).toContain('结束后再等待 3 分钟')
    expect(downloadBatchNextLabel({ ...batch, status: 'paused' }, 0, 0)).toContain('当前下载不受影响')
    expect(downloadBatchNextLabel({ ...batch, status: 'cancelled' }, 0, 0)).toContain('已停止')
  })

  it('当前视频使用后端实际间隔，兼容历史九十秒与三分钟批次', () => {
    expect(downloadBatchNextLabel({ ...batch, current_task_id: 'task-1', interval_seconds: 30 }, 0, 0)).toContain('结束后再等待 30 秒')
    expect(downloadBatchNextLabel({ ...batch, current_task_id: 'task-1', interval_seconds: 90 }, 0, 0)).toContain('结束后再等待 90 秒')
    expect(downloadBatchNextLabel({ ...batch, current_task_id: 'task-1', interval_seconds: 180 }, 0, 0)).toContain('结束后再等待 3 分钟')
  })

  it('缺少实际间隔时使用三十秒默认值，不缩短后台已有冷却', () => {
    expect(downloadBatchNextLabel({ ...batch, current_task_id: 'task-1', interval_seconds: 0 }, 0, 0)).toContain('结束后再等待 30 秒')
    expect(downloadBatchCountdown({ ...batch, interval_seconds: 30, next_allowed_at: 280 }, 100_000, 90_000)).toBe(180)
  })
})
