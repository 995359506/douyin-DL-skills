export type DownloadBatchStatus = 'queued' | 'running' | 'paused' | 'completed' | 'cancelled'

export interface DownloadBatch {
  batch_id: string
  author: string
  profile_id: string
  status: DownloadBatchStatus
  total: number
  completed: number
  skipped?: number
  failed: number
  pending: number
  cancelled: number
  current_aweme_id?: string | null
  current_task_id?: string | null
  next_allowed_at?: number | null
  next_in_seconds?: number | null
  interval_seconds: number
  error?: { code?: string; reason?: string; action?: string } | null
}

export const downloadBatchStatusLabel = (status: DownloadBatchStatus): string => ({
  queued: '排队中', running: '下载中', paused: '已暂停', completed: '已完成', cancelled: '已停止',
})[status] || '状态未知'

export const downloadBatchSkipLabel = (batch: DownloadBatch): string => `已下载跳过 ${batch.skipped ?? 0}`

export const downloadBatchProgressValue = (batch: DownloadBatch): number => batch.completed + batch.failed + batch.cancelled

export const downloadBatchCountdown = (batch: DownloadBatch, now: number, fetchedAt: number): number => {
  const deadline = batch.next_allowed_at
    ? batch.next_allowed_at * 1000
    : fetchedAt + Math.max(0, batch.next_in_seconds || 0) * 1000
  return Math.max(0, Math.ceil((deadline - now) / 1000))
}

export const downloadBatchNextLabel = (batch: DownloadBatch, now: number, fetchedAt: number): string => {
  if (batch.status === 'completed') return '本批次已处理完毕'
  if (batch.status === 'cancelled') return '后续下载已停止；当前下载不受影响'
  if (batch.status === 'paused') return '后续下载已暂停；当前下载不受影响'
  if (batch.current_task_id) {
    const seconds = Math.max(0, Math.ceil(Number(batch.interval_seconds) || 30))
    const interval = seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`
    return `正在处理当前视频；实际下载结束后再等待 ${interval}`
  }
  if (!batch.pending) return '正在收尾'
  const seconds = downloadBatchCountdown(batch, now, fetchedAt)
  return seconds > 0 ? `下一条：${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒后` : '等待后台调度下一条'
}
