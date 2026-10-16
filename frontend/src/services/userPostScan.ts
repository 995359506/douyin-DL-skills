import api, { type ApiResponse } from '@/api'

export const USER_SCAN_STORAGE_KEY = 'douyin-user-post-scan-v1'

export type ScanStatus = 'idle' | 'pending' | 'scanning' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'expired'
export type WorkKind = 'video' | 'image'
export type DownloadHistory = 'all' | 'downloaded' | 'not_downloaded'

export interface UserProfile {
  sec_user_id: string
  nickname: string
  avatar_url?: string
  signature?: string
  work_count?: number | null
}

export interface UserWork {
  aweme_id: string
  title: string
  published_at: string
  kind: WorkKind
  duration_seconds?: number | null
  cover_url?: string | null
  downloaded: boolean
}

export interface WorkFilter {
  keyword: string
  date_from?: string
  date_to?: string
  kind: 'all' | WorkKind
  history: DownloadHistory
}

export interface UserPostScan {
  scan_id: string
  scan_token?: string
  status: ScanStatus
  profile: UserProfile
  found_count: number
  total_hint?: number | null
  pages_scanned: number
  cursor?: string | null
  elapsed_ms: number
  expires_at?: string | null
  warning?: string | null
  error?: {
    code?: string
    stage?: string
    stage_label?: string
    status_code?: number | null
    message: string
    action?: string
    retryable: boolean
  } | null
}

export interface WorkPage {
  items: UserWork[]
  cursor?: string | null
  next_cursor?: string | null
  matched_count: number
  total_count: number
}

export type ScanSelection =
  | { mode: 'explicit'; aweme_ids: string[] }
  | {
      mode: 'all_filtered'
      filter: WorkFilter
      excluded_aweme_ids: string[]
      included_aweme_ids: string[]
    }

export interface DownloadScanRequest {
  scan_id: string
  selection: ScanSelection
  options: {
    download_path?: string
  }
}

export interface DistillRun {
  run_id: string
  status: string
  options?: { stage_policy?: 'preview' | 'full' }
  nickname: string
  total_count: number
  completed_count: number
  degraded_count: number
  skipped_count: number
  failed_count: number
  cancelled_count?: number
  pending_count?: number
  output_path?: string | null
  synthesis_model?: string
  error?: { code?: string; reason?: string; action?: string } | null
  created_at?: string
  updated_at?: string
}

export interface DistillDetail { run: DistillRun; items: Array<Record<string, any>>; counts: Record<string, number> }

export interface UserPostScanService {
  verifyProfile(userUrl: string): Promise<UserProfile>
  startScan(userUrl: string): Promise<UserPostScan>
  getScan(scanId: string): Promise<UserPostScan>
  listWorks(scanId: string, filter: WorkFilter, cursor?: string | null, limit?: number): Promise<WorkPage>
  cancelScan(scanId: string): Promise<UserPostScan>
  createDownload(request: DownloadScanRequest): Promise<{ task_id: string; message: string }>
  createDistill(request: { scanId: string; selection: ScanSelection; forceReanalyze?: boolean; downloadPath?: string; stagePolicy?: 'preview' | 'full' }): Promise<{ run_id: string; run: DistillRun; video_count: number; image_text_count: number; estimated_model_calls: number }>
  getDistill(runId: string, includeText?: boolean): Promise<DistillDetail>
  listDistill(limit?: number, offset?: number): Promise<{ items: DistillRun[]; limit: number; offset: number }>
  cancelDistill(runId: string): Promise<DistillRun>
  resumeDistill(runId: string, awemeIds?: string[], stagePolicy?: 'full'): Promise<DistillRun>
  getAiDiagnostics(): Promise<Record<string, any>>
}

const unwrap = <T>(response: ApiResponse): T => {
  if (!response.success || response.data === undefined) throw new Error(response.message || '请求失败')
  return response.data as T
}

const scanTokens = new Map<string, string>()
const authConfig = (scanId: string) => {
  const token = scanTokens.get(scanId)
  return token ? { headers: { 'X-Scan-Token': token } } : undefined
}

const httpUserPostScanService: UserPostScanService = {
  async verifyProfile(userUrl: string) {
    return unwrap<UserProfile>(await api.post('/user-info', { user_url: userUrl }) as unknown as ApiResponse)
  },
  async startScan(userUrl: string) {
    const scan = unwrap<UserPostScan>(await api.post('/user-post-scans', { user_url: userUrl }) as unknown as ApiResponse)
    if (scan.scan_token) scanTokens.set(scan.scan_id, scan.scan_token)
    return scan
  },
  async getScan(scanId: string) {
    return unwrap<UserPostScan>(await api.get(`/user-post-scans/${scanId}`, authConfig(scanId)) as unknown as ApiResponse)
  },
  async listWorks(scanId: string, filter: WorkFilter, cursor = null, limit = 24) {
    return unwrap<WorkPage>(await api.get(`/user-post-scans/${scanId}/items`, {
      ...authConfig(scanId),
      params: {
        cursor: cursor || undefined,
        limit,
        keyword: filter.keyword || undefined,
        date_from: filter.date_from,
        date_to: filter.date_to,
        kind: filter.kind === 'all' ? undefined : filter.kind,
        history: filter.history === 'all' ? undefined : filter.history,
      },
    }) as unknown as ApiResponse)
  },
  async cancelScan(scanId: string) {
    return unwrap<UserPostScan>(await api.post(`/user-post-scans/${scanId}/cancel`, undefined, authConfig(scanId)) as unknown as ApiResponse)
  },
  async createDownload(request: DownloadScanRequest) {
    const response = await api.post(`/user-post-scans/${request.scan_id}/downloads`, {
      selection_mode: request.selection.mode === 'explicit' ? 'selected' : 'all',
      selected_ids: request.selection.mode === 'explicit' ? request.selection.aweme_ids : [],
      excluded_ids: request.selection.mode === 'all_filtered' ? request.selection.excluded_aweme_ids : [],
      filters: request.selection.mode === 'all_filtered' ? {
        keyword: request.selection.filter.keyword,
        date_from: request.selection.filter.date_from,
        date_to: request.selection.filter.date_to,
        kind: request.selection.filter.kind,
        history: request.selection.filter.history,
      } : {},
      save_path: request.options.download_path,
      confirm: true,
    }, authConfig(request.scan_id))
    return unwrap<{ task_id: string; message: string }>(response as unknown as ApiResponse)
  },
  async createDistill(request) {
    const response = await api.post(`/distill-runs?scan_id=${encodeURIComponent(request.scanId)}`, {
      confirm: true,
      selection: request.selection.mode === 'explicit'
        ? { mode: 'selected', aweme_ids: request.selection.aweme_ids }
        : { mode: 'all_filtered', aweme_ids: request.selection.included_aweme_ids, excluded_aweme_ids: request.selection.excluded_aweme_ids, filters: request.selection.filter },
      force_reanalyze: Boolean(request.forceReanalyze),
      stage_policy: request.stagePolicy || 'full',
      save_path: request.downloadPath || undefined,
    }, authConfig(request.scanId))
    return unwrap(response as unknown as ApiResponse)
  },
  async getDistill(runId, includeText = false) {
    return unwrap<DistillDetail>(await api.get(`/distill-runs/${runId}`, { params: { include_text: includeText } }) as unknown as ApiResponse)
  },
  async listDistill(limit = 10, offset = 0) {
    return unwrap<{ items: DistillRun[]; limit: number; offset: number }>(await api.get('/distill-runs', { params: { limit, offset } }) as unknown as ApiResponse)
  },
  async cancelDistill(runId) {
    return unwrap<DistillRun>(await api.post(`/distill-runs/${runId}/cancel`) as unknown as ApiResponse)
  },
  async resumeDistill(runId, awemeIds = [], stagePolicy) {
    return unwrap<DistillRun>(await api.post(`/distill-runs/${runId}/resume`, { aweme_ids: awemeIds, stage_policy: stagePolicy }) as unknown as ApiResponse)
  },
  async getAiDiagnostics() {
    return unwrap<Record<string, any>>(await api.get('/ai/diagnostics') as unknown as ApiResponse)
  },
}

export const getUserPostScanService = async (): Promise<UserPostScanService> => {
  try {
    const saved = JSON.parse(sessionStorage.getItem(USER_SCAN_STORAGE_KEY) || 'null')
    if (saved?.scan_id && saved?.scan_token) scanTokens.set(saved.scan_id, saved.scan_token)
  } catch {
    // Ignore malformed local scan state; a fresh scan will be started by the UI.
  }
  const preview = import.meta.env.DEV ? new URLSearchParams(window.location.search).get('user-scan-preview') : null
  if (preview) {
    const { createMockUserPostScanService } = await import('@/mocks/userPostScanMock')
    return createMockUserPostScanService(preview)
  }
  return httpUserPostScanService
}
