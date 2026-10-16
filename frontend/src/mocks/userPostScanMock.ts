import type {
  DownloadScanRequest,
  DistillRun,
  DistillDetail,
  UserPostScan,
  UserPostScanService,
  UserProfile,
  UserWork,
  WorkFilter,
  WorkPage,
} from '@/services/userPostScan'
import { workMatchesFilter } from '@/utils/workSelection'

const countFromScenario = (scenario: string): number => {
  if (scenario === '0') return 0
  if (scenario === '10') return 10
  return 1000
}

const createWork = (index: number): UserWork => ({
  aweme_id: `mock-aweme-${String(index + 1).padStart(5, '0')}`,
  title: index === 2
    ? '这是一条用于验证极端布局的超长作品标题：海风、落日、没有写完的夏天，以及镜头之外仍在发生的故事'
    : `${index % 4 === 0 ? '图文记录' : '片场短片'} · 第 ${index + 1} 号素材`,
  published_at: new Date(Date.UTC(2026, 7, 30) - index * 86_400_000).toISOString(),
  kind: index % 4 === 0 ? 'image' : 'video',
  duration_seconds: index % 4 === 0 ? null : 12 + (index % 173),
  cover_url: null,
  downloaded: index % 5 === 0,
})

export const createMockUserPostScanService = (scenario: string): UserPostScanService => {
  const total = countFromScenario(scenario)
  const allWorks = Array.from({ length: total }, (_, index) => createWork(index))
  const profile: UserProfile = {
    sec_user_id: 'mock-user-cinematic-01',
    nickname: scenario === '0' ? '空镜收藏家' : '片场观察员',
    signature: '开发环境扫描预览，不访问真实抖音。',
    work_count: total,
  }
  let startedAt = Date.now()
  let cancelled = scenario === 'cancelled'
  let mockRun: DistillRun | null = null

  const snapshot = (): UserPostScan => {
    const elapsed = Date.now() - startedAt
    if (scenario === 'expired') return { scan_id: 'mock-scan-expired', status: 'expired', profile, found_count: 0, total_hint: total, pages_scanned: 0, elapsed_ms: elapsed, error: { code: 'SCAN_EXPIRED', message: '扫描结果已过期，请重新解析。', retryable: true } }
    if (cancelled) return { scan_id: `mock-scan-${scenario}`, status: 'cancelled', profile, found_count: Math.min(total, 18), total_hint: total, pages_scanned: 1, elapsed_ms: elapsed }
    if (scenario === 'failed' && elapsed > 700) return { scan_id: 'mock-scan-failed', status: 'failed', profile, found_count: Math.min(total, 8), total_hint: total, pages_scanned: 1, elapsed_ms: elapsed, error: { code: 'SCAN_UPSTREAM_TIMEOUT', message: '作品列表服务暂时无响应。', retryable: true } }
    const ratio = Math.min(1, elapsed / 1500)
    const found = Math.min(total, Math.max(total ? 1 : 0, Math.floor(total * ratio)))
    const complete = ratio >= 1
    return {
      scan_id: `mock-scan-${scenario}`,
      status: complete ? (scenario === 'partial' ? 'partial' : 'completed') : 'scanning',
      profile,
      found_count: complete && scenario === 'partial' ? Math.max(0, total - 7) : found,
      total_hint: total,
      pages_scanned: Math.ceil(found / 20),
      cursor: complete ? null : `cursor-${Math.ceil(found / 20)}`,
      elapsed_ms: elapsed,
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      warning: complete && scenario === 'partial' ? '最后一页读取失败，已保留前面成功解析的作品。' : null,
    }
  }

  return {
    async verifyProfile() { return profile },
    async startScan() { startedAt = Date.now(); cancelled = false; return snapshot() },
    async getScan() { return snapshot() },
    async listWorks(_scanId: string, filter: WorkFilter, cursor = null, limit = 24): Promise<WorkPage> {
      const scan = snapshot()
      const available = allWorks.slice(0, scan.status === 'scanning' ? scan.found_count : scan.found_count)
      const filtered = available.filter(work => workMatchesFilter(work, filter))
      const offset = Number(cursor || 0)
      return {
        items: filtered.slice(offset, offset + limit),
        cursor: String(offset),
        next_cursor: offset + limit < filtered.length ? String(offset + limit) : null,
        matched_count: filtered.length,
        total_count: scan.found_count,
      }
    },
    async cancelScan() { cancelled = true; return snapshot() },
    async createDownload(_request: DownloadScanRequest) { return { task_id: 'mock-task-user-selection', message: 'Mock 下载任务已创建（未下载媒体）' } },
    async createDistill(_request) {
      mockRun = { run_id: 'mock-distill-run', status: 'queued', nickname: profile.nickname, total_count: 3, completed_count: 0, degraded_count: 0, skipped_count: 1, failed_count: 0, synthesis_model: 'gpt-5.6-luna' }
      return { run_id: mockRun.run_id, run: mockRun, video_count: 2, image_text_count: 1, estimated_model_calls: 6 }
    },
    async getDistill(_runId): Promise<DistillDetail> {
      if (mockRun && mockRun.status === 'queued') mockRun = { ...mockRun, status: 'completed', completed_count: 2 }
      return { run: mockRun || { run_id: 'mock-distill-run', status: 'failed', nickname: profile.nickname, total_count: 0, completed_count: 0, degraded_count: 0, skipped_count: 0, failed_count: 0 }, items: [], counts: { total: 3, completed: 2, degraded: 0, skipped: 1, failed: 0 } }
    },
    async listDistill() { return { items: mockRun ? [mockRun] : [], limit: 10, offset: 0 } },
    async cancelDistill(_runId) { mockRun = mockRun ? { ...mockRun, status: 'cancelled' } : null; return mockRun! },
    async resumeDistill(_runId) { mockRun = mockRun ? { ...mockRun, status: 'running' } : null; return mockRun! },
    async getAiDiagnostics() { return { ffmpeg: { ok: true }, transcript: { api_key_configured: true, dashscope_api_key_configured: true }, summary: { api_key_configured: true } } },
  }
}
