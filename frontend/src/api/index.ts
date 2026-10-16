import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,  // 2分钟超时，适应大合集视频获取
  headers: {
    'Content-Type': 'application/json'
  }
})

// 响应拦截器
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || error.response?.data?.message || error.message || '请求失败'
    error.message = message
    error.code = detail?.code || error.code
    error.action = detail?.action
    return Promise.reject(error)
  }
)

export interface ApiResponse {
  success: boolean
  data?: any
  message: string
}

export interface DownloadVideoParams {
  url: string
  auto_summary?: boolean
}

export interface DownloadMixParams {
  mix_id: string
  mix_name?: string
  max_counts?: number | null  // None表示全量下载
  interval?: string  // 日期区间，默认'all'
}

export interface RefreshCookieParams {
  headless?: boolean
}

export interface DownloadUserPostsParams {
  user_url: string
  max_counts?: number | null
  interval?: string
}

export interface UserInfoParams {
  user_url: string
}

export interface CreateTaskParams {
  type: 'single' | 'mix' | 'mix_user'
  url?: string
  mix_id?: string
  mix_name?: string
  user_url?: string
}

export interface GetTasksParams {
  status?: string
  type?: string
  author?: string
  limit?: number
  offset?: number
  keyword?: string
  sort_by?: 'created_at' | 'completed_at' | 'status' | 'type' | 'title'
  sort_order?: 'asc' | 'desc'
}

export const douyinApi = {
  // 获取服务状态
  getStatus: () => api.get('/status'),
  getAiDiagnostics: () => api.get('/ai/diagnostics'),

  // 下载单个视频（创建任务）
  downloadVideo: (params: DownloadVideoParams) =>
    api.post('/download', params),

  // 获取合集信息
  getMixInfo: (params: DownloadVideoParams) =>
    api.post('/mix-info', params),

  // 后端真实用户资料预检
  getUserInfo: (params: UserInfoParams) =>
    api.post('/user-info', params),

  // 下载合集（创建任务）
  downloadMix: (params: DownloadMixParams) =>
    api.post('/download-mix', params),

  // 刷新Cookie
  refreshCookie: (params?: RefreshCookieParams) =>
    api.post('/cookie/refresh', params || {}, { timeout: 15000 }),

  // 检查Cookie状态
  checkCookieStatus: () => api.get('/cookie/status'),

  // ========== 任务管理 API ==========

  // 获取任务列表
  getTasks: (params?: GetTasksParams) =>
    api.get('/tasks', { params }),

  // 获取单个任务详情
  getTask: (taskId: string) =>
    api.get(`/tasks/${taskId}`),

  // 创建任务
  createTask: (params: CreateTaskParams) =>
    api.post('/tasks', params),

  // 重试任务
  retryTask: (taskId: string) =>
    api.post(`/tasks/${taskId}/retry`),

  // 继续未完成项（保留已下载文件）
  continueTask: (taskId: string) =>
    api.post(`/tasks/${taskId}/continue`),

  // 删除任务
  deleteTask: (taskId: string, deleteFiles = false) =>
    api.delete(`/tasks/${taskId}`, { params: { delete_files: deleteFiles } }),

  // 取消任务
  cancelTask: (taskId: string) =>
    api.post(`/tasks/${taskId}/cancel`),

  openTaskFolder: (taskId: string) =>
    api.post(`/tasks/${taskId}/open-folder`),

  // 下载用户主页作品
  downloadUserPosts: (params: DownloadUserPostsParams) =>
    api.post('/download-user-posts', params),

  createTranscript: (taskId: string, force = false) =>
    api.post(`/tasks/${taskId}/transcript`, { force }),

  getTranscript: (taskId: string) =>
    api.get(`/tasks/${taskId}/transcript`),

  cancelTranscript: (jobId: string) =>
    api.post(`/transcripts/${jobId}/cancel`),

  retryTranscript: (jobId: string) =>
    api.post(`/transcripts/${jobId}/retry`),

  createVideoSummary: (taskId: string, force = false) =>
    api.post(`/tasks/${taskId}/summary`, { force }),

  createAuthorSummaryBatch: (author: string) =>
    api.post('/summary-batches', { author }),

  previewAuthorSummaryBatch: (author: string) =>
    api.post('/summary-batches/preview', { author }),

  getAuthorSummaryBatch: (author: string) =>
    api.get('/summary-batches', { params: { author } }),

  createAuthorTranscriptBatch: (author: string) =>
    api.post('/transcript-batches', { author }),

  previewAuthorTranscriptBatch: (author: string) =>
    api.post('/transcript-batches/preview', { author }),

  getAuthorTranscriptBatch: (author: string) =>
    api.get('/transcript-batches', { params: { author } }),

  getVideoSummary: (taskId: string) =>
    api.get(`/tasks/${taskId}/summary`),

  cancelVideoSummary: (jobId: string) =>
    api.post(`/summaries/${jobId}/cancel`),

  retryVideoSummary: (jobId: string) =>
    api.post(`/summaries/${jobId}/retry`),
}

export default api
