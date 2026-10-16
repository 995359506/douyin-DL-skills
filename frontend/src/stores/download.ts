import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { douyinApi, type ApiResponse, type CreateTaskParams } from '@/api'
import { wsService, type WebSocketStatus } from '@/services/websocket'
import type { DownloadTaskHistoryAttempt } from '@/utils/downloadTaskHistory'

// 任务类型定义
export interface DownloadTask {
  id: string
  type: 'single' | 'mix' | 'mix_user' | 'post'
  status: 'pending' | 'queued' | 'downloading' | 'paused' | 'completed' | 'failed' | 'cancelled'
  summary_status?: 'none' | 'running' | 'completed' | 'failed'
  video_name: string
  file_name: string
  original_desc: string
  video_info: {
    aweme_id?: string
    author?: string
    nickname?: string
    cover?: string | null
    create_time?: string
    mix_id?: string
    sec_user_id?: string
  }
  url?: string
  mix_id?: string
  mix_name?: string
  download_path?: string
  progress: {
    current: number
    total: number
    percentage: number
    current_video_name?: string
  }
  result: {
    success: boolean
    downloaded: number
    skipped: number
    failed: number
    message: string
    completed_at?: string
    has_more?: boolean
    batch_info?: {
      processed: number
      limit: number
      message: string
    }
    failed_items?: Array<{
      aweme_id: string
      name: string
      reason: string
    }>
    history_attempts?: DownloadTaskHistoryAttempt[]
    history_count?: number
    history_failed_count?: number
  }
  created_at: string
  download_options?: {
    max_counts?: number | null
    interval?: string
  }
  started_at?: string
  completed_at?: string
  error?: {
    stage: string
    code: string
    reason: string
    action: string
  }
  retry_count: number
  /**
   * When the API collapses several attempts for the same aweme into one
   * canonical row, it keeps the non-canonical attempts here for transparency.
   * The list UI must not derive this by de-duplicating the current page: doing
   * that would make the server-provided pagination total incorrect.
   */
  history_attempts?: DownloadTaskHistoryAttempt[]
  history_count?: number
  history_failed_count?: number
  /** 批量任务中的逐条作品明细。 */
  videos?: Array<{
    aweme_id: string
    desc?: string
    nickname?: string
    sec_user_id?: string
    create_time?: string
    status?: string
    file_path?: string | null
    file_size?: number | null
    error_message?: string | null
    download_time?: string | null
    video_play_addr?: string | null
    cover?: string | null
    duration?: string | null
    summary_status?: 'none' | 'running' | 'completed' | 'failed'
  }>
  /** 仅供任务画廊使用的批量明细标记。 */
  is_batch_video?: boolean
  parent_task_id?: string
}

export type { DownloadTaskHistoryAttempt } from '@/utils/downloadTaskHistory'

export const useDownloadStore = defineStore('download', () => {
  // State
  const loading = ref(false)
  const tasksLoading = ref(false)
  const error = ref('')
  const currentTask = ref<any>(null)
  const cookieStatus = ref({
    exists: false,
    valid: false
  })
  const cookieStarting = ref(false)
  const cookieChecking = ref(false)
  const cookieRefresh = ref<{
    status: 'idle' | 'starting' | 'waiting' | 'succeeded' | 'failed' | 'timeout'
    refreshing: boolean
    message: string
    error: { code?: string; message?: string; action?: string } | null
  }>({ status: 'idle', refreshing: false, message: '', error: null })
  const downloadPath = ref('项目配置的下载目录')
  
  // 任务管理相关 State
  const tasks = ref<DownloadTask[]>([])
  const taskPollingInterval = ref<number | null>(null)
  const taskFilterStatus = ref<string>('')
  const taskFilterType = ref<string>('')
  const taskFilterAuthor = ref<string | null>(null)
  const taskAuthors = ref<Array<{ name: string; count: number }>>([])
  const taskKeyword = ref('')
  const taskSortBy = ref<'created_at' | 'completed_at' | 'status' | 'type' | 'title'>('created_at')
  const taskSortOrder = ref<'asc' | 'desc'>('desc')
  const taskPage = ref(1)
  const taskPageSize = ref(20)
  const taskTotal = ref(0)
  
  // WebSocket相关 State
  const wsStatus = ref<WebSocketStatus>('disconnected')
  const useWebSocket = ref(true) // 是否启用WebSocket（失败时回退到轮询）
  let wsUnsubscribeCallbacks: (() => void)[] = []
  
  // 状态轮询定时器
  let statusPollingTimer: ReturnType<typeof setInterval> | null = null
  let cookieRefreshTimer: ReturnType<typeof setTimeout> | null = null
  let cookieRefreshPolling = false
  let cookieRefreshDeadline = 0
  
  // 告警消息自动清除定时器
  let taskMessageTimer: ReturnType<typeof setTimeout> | null = null
  let wsRefreshTimer: ReturnType<typeof setTimeout> | null = null
  
  // 自动清除任务消息的辅助函数
  const autoClearTaskMessage = (delay: number = 5000) => {
    // 清除之前的定时器
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
    }
    // 设置新的定时器
    taskMessageTimer = window.setTimeout(() => {
      currentTask.value = null
      console.log('[Store] 任务消息已自动清除')
    }, delay)
  }

  // Getters
  const isCookieValid = computed(() => cookieStatus.value.valid)

  // Actions

  const checkServiceStatus = async () => {
    try {
      const response = await douyinApi.getStatus() as any
      if (response?.download_path) downloadPath.value = response.download_path
      return response
    } catch {
      return null
    }
  }
  
  const updateCookieRefresh = (data: any) => {
    if (!data || typeof data.refresh_status !== 'string') return
    cookieRefresh.value = {
      status: data.refresh_status,
      refreshing: Boolean(data.refreshing),
      message: data.refresh_message || '',
      error: data.refresh_error || null
    }
  }

  const stopCookieRefreshPolling = () => {
    cookieRefreshPolling = false
    if (cookieRefreshTimer) {
      clearTimeout(cookieRefreshTimer)
      cookieRefreshTimer = null
    }
  }

  const pollCookieRefresh = async () => {
    if (!cookieRefreshPolling) return
    await checkCookieStatus({ silent: true })
    if (!cookieRefreshPolling) return
    if (!cookieRefresh.value.refreshing) {
      stopCookieRefreshPolling()
      return
    }
    if (Date.now() >= cookieRefreshDeadline) {
      cookieRefresh.value = {
        status: 'timeout',
        refreshing: false,
        message: '等待扫码超时，请重新点击扫码登录',
        error: {
          code: 'COOKIE_REFRESH_TIMEOUT',
          message: '等待扫码超时',
          action: '确认抖音登录窗口仍打开后重新扫码'
        }
      }
      stopCookieRefreshPolling()
      return
    }
    cookieRefreshTimer = window.setTimeout(() => void pollCookieRefresh(), 2000)
  }

  const startCookieRefreshPolling = () => {
    if (cookieRefreshPolling) return
    cookieRefreshPolling = true
    cookieRefreshDeadline = Date.now() + 360000
    cookieRefreshTimer = window.setTimeout(() => void pollCookieRefresh(), 0)
  }

  // 检查Cookie状态
  const checkCookieStatus = async ({ silent = false }: { silent?: boolean } = {}) => {
    if (!silent) cookieChecking.value = true
    try {
      const response = await douyinApi.checkCookieStatus() as unknown as ApiResponse
      if (response.success && response.data) {
        cookieStatus.value = {
          exists: response.data.exists || false,
          valid: response.data.valid || false
        }
        updateCookieRefresh(response.data)
        if (cookieRefresh.value.refreshing) startCookieRefreshPolling()
        else stopCookieRefreshPolling()
      }
      return response
    } catch (err) {
      return null
    } finally {
      if (!silent) cookieChecking.value = false
    }
  }

  // 刷新Cookie
  const refreshCookie = async (headless: boolean = false) => {
    cookieStarting.value = true
    error.value = ''
    stopCookieRefreshPolling()
    
    try {
      const response = await douyinApi.refreshCookie({ headless }) as unknown as ApiResponse
      updateCookieRefresh(response.data)
      await checkCookieStatus({ silent: true })
      if (cookieRefresh.value.refreshing) startCookieRefreshPolling()
      return response
    } catch (err: any) {
      error.value = err.message || '刷新Cookie失败'
      cookieRefresh.value = {
        status: 'failed',
        refreshing: false,
        message: err.message || '无法启动扫码登录',
        error: {
          code: err.code,
          message: err.message || '无法启动扫码登录',
          action: err.action
        }
      }
      throw err
    } finally {
      cookieStarting.value = false
    }
  }

  // 启动状态轮询
  const startStatusPolling = () => {
    if (statusPollingTimer) {
      clearInterval(statusPollingTimer)
    }
    console.log('[Store] Starting cookie status polling...')
    let pollCount = 0
    const maxPolls = 12 // 最多轮询12次（约60秒）
    
    statusPollingTimer = setInterval(async () => {
      pollCount++
      console.log(`[Store] Polling cookie status (${pollCount}/${maxPolls})...`)
      await checkCookieStatus()
      
      // 如果Cookie有效，或达到最大轮询次数，停止轮询
      if (cookieStatus.value.valid || pollCount >= maxPolls) {
        stopStatusPolling()
        console.log('[Store] Cookie status polling stopped. Valid:', cookieStatus.value.valid)
      }
    }, 5000) // Cookie 登录窗口使用低频、限时轮询
  }

  // 停止状态轮询
  const stopStatusPolling = () => {
    if (statusPollingTimer) {
      clearInterval(statusPollingTimer)
      statusPollingTimer = null
      console.log('[Store] Status polling stopped')
    }
  }

  // 下载单个视频
  const downloadVideo = async (url: string, autoSummary = false) => {
    loading.value = true
    error.value = ''
    // 清除之前的消息
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
    }
    currentTask.value = null
    
    try {
      const response = await douyinApi.downloadVideo({ url, auto_summary: autoSummary }) as unknown as ApiResponse
      currentTask.value = response
      
      console.log('[Store] downloadVideo response:', response)
      console.log('[Store] response.data:', response.data)
      console.log('[Store] is_duplicate:', response.data?.is_duplicate)
      
      // 任务创建成功后刷新任务列表（立即更新，不等待WebSocket）
      if (response.success) {
        console.log('[Store] 视频下载任务创建成功，刷新任务列表')
        await fetchTasks()
        
        // 检查是否是重复任务
        if (response.data?.is_duplicate) {
          console.log('[Store] 检测到重复视频任务，延长显示时间')
          // 如果是重复任务，延长显示时间到8秒，让用户看清楚
          autoClearTaskMessage(8000)
        } else {
          console.log('[Store] 新视频任务，正常显示时间')
          autoClearTaskMessage(5000)
        }
      } else {
        autoClearTaskMessage(5000)
      }
      
      return response
    } catch (err: any) {
      error.value = err.message || '下载失败'
      currentTask.value = { success: false, message: err.message || '下载失败' }
      autoClearTaskMessage(5000)
      throw err
    } finally {
      loading.value = false
    }
  }

  // 获取合集信息
  const getMixInfo = async (url: string) => {
    loading.value = true
    error.value = ''
    try {
      const response = await douyinApi.getMixInfo({ url }) as unknown as ApiResponse
      return response
    } catch (err: any) {
      error.value = err.message || '获取合集信息失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  // 后端真实用户资料预检
  const getUserInfo = async (userUrl: string) => {
    loading.value = true
    error.value = ''
    try {
      return await douyinApi.getUserInfo({ user_url: userUrl }) as unknown as ApiResponse
    } catch (err: any) {
      error.value = err.message || '获取用户信息失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  // 下载合集
  const downloadMix = async (mixId: string, mixName: string = '', maxCounts?: number | null, interval: string = 'all') => {
    loading.value = true
    error.value = ''
    // 清除之前的消息
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
    }
    currentTask.value = null
    
    try {
      const response = await douyinApi.downloadMix({ 
        mix_id: mixId, 
        mix_name: mixName,
        max_counts: maxCounts,
        interval: interval
      }) as unknown as ApiResponse
      currentTask.value = response
      
      console.log('[Store] downloadMix response:', response)
      console.log('[Store] response.data:', response.data)
      console.log('[Store] is_duplicate:', response.data?.is_duplicate)
      
      // 任务创建成功后刷新任务列表（立即更新，不等待WebSocket）
      if (response.success) {
        console.log('[Store] 合集下载任务创建成功，刷新任务列表')
        await fetchTasks()
        
        // 检查是否是重复任务
        if (response.data?.is_duplicate) {
          console.log('[Store] 检测到重复合集任务，延长显示时间')
          // 如果是重复任务，延长显示时间到8秒
          autoClearTaskMessage(8000)
        } else {
          console.log('[Store] 新合集任务，正常显示时间')
          autoClearTaskMessage(5000)
        }
      } else {
        autoClearTaskMessage(5000)
      }
      
      return response
    } catch (err: any) {
      error.value = err.message || '下载合集失败'
      currentTask.value = { success: false, message: err.message || '下载合集失败' }
      autoClearTaskMessage(5000)
      throw err
    } finally {
      loading.value = false
    }
  }

  // 下载用户主页作品
  const downloadUserPosts = async (
    userUrl: string,
    maxCounts?: number | null,
    interval: string = 'all'
  ) => {
    loading.value = true
    error.value = ''
    // 清除之前的消息
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
    }
    currentTask.value = null

    try {
      const response = await douyinApi.downloadUserPosts({
        user_url: userUrl,
        max_counts: maxCounts,
        interval: interval,
      }) as unknown as ApiResponse
      currentTask.value = response

      console.log('[Store] downloadUserPosts response:', response)

      // 任务创建成功后刷新任务列表
      if (response.success) {
        console.log('[Store] 用户作品下载任务创建成功，刷新任务列表')
        await fetchTasks()

        // 检查是否是重复任务
        if (response.data?.is_duplicate) {
          console.log('[Store] 检测到重复用户作品任务，延长显示时间')
          autoClearTaskMessage(8000)
        } else {
          console.log('[Store] 新用户作品任务，正常显示时间')
          autoClearTaskMessage(5000)
        }
      } else {
        autoClearTaskMessage(5000)
      }

      return response
    } catch (err: any) {
      error.value = err.message || '下载用户作品失败'
      currentTask.value = { success: false, message: err.message || '下载用户作品失败' }
      autoClearTaskMessage(5000)
      throw err
    } finally {
      loading.value = false
    }
  }

  // ========== 任务管理 Actions ==========

  // 获取任务列表（支持分页）
  const fetchTasks = async (options?: { 
    status?: string; 
    type?: string; 
    author?: string | null;
    keyword?: string;
    sortBy?: typeof taskSortBy.value;
    sortOrder?: typeof taskSortOrder.value;
    page?: number;
    pageSize?: number;
    silent?: boolean;
  }) => {
    const silent = options?.silent ?? false
    if (!silent) tasksLoading.value = true
    if (!silent) error.value = ''
    
    try {
      const page = options?.page ?? taskPage.value
      const pageSize = options?.pageSize ?? taskPageSize.value
      const offset = (page - 1) * pageSize
      
      const response = await douyinApi.getTasks({
        status: options?.status ?? taskFilterStatus.value,
        type: options?.type ?? taskFilterType.value,
        author: (options?.author ?? taskFilterAuthor.value) ?? undefined,
        keyword: options?.keyword ?? (taskKeyword.value.trim() || undefined),
        sort_by: options?.sortBy ?? taskSortBy.value,
        sort_order: options?.sortOrder ?? taskSortOrder.value,
        limit: pageSize,
        offset: offset
      }) as unknown as ApiResponse
      
      console.log('[Store] fetchTasks response:', response)
      if (response.success && response.data) {
        tasks.value = response.data.tasks || []
        taskTotal.value = response.data.total || 0
        taskAuthors.value = response.data.authors || []
        taskPage.value = page
        taskPageSize.value = pageSize
        console.log(`[Store] 更新任务列表: ${tasks.value.length} 个任务, 总计: ${taskTotal.value}`)
        // 打印第一个任务的video_name检查
        if (tasks.value.length > 0) {
          console.log('[Store] 第一个任务video_name:', tasks.value[0].video_name?.substring(0, 80))
          console.log('[Store] 第一个任务完整数据:', tasks.value[0])
        }
      } else {
        // API 返回成功标志为 false
        error.value = response.message || '获取任务列表失败'
        console.error('[Store] API 返回失败:', response.message)
      }
      return response
    } catch (err: any) {
      error.value = err.message || '获取任务列表失败'
      console.error('[Store] 获取任务列表失败:', err)
      return null
    } finally {
      if (!silent) tasksLoading.value = false
    }
  }
  
  // 跳转到指定页
  const goToPage = async (page: number) => {
    await fetchTasks({ page })
  }
  
  // 改变每页数量
  const changePageSize = async (pageSize: number) => {
    taskPageSize.value = pageSize
    taskPage.value = 1
    await fetchTasks({ page: 1, pageSize })
  }

  const setTaskFilters = async (filters: {
    status?: string
    type?: string
    author?: string | null
    keyword?: string
    sortBy?: typeof taskSortBy.value
    sortOrder?: typeof taskSortOrder.value
  }) => {
    if (filters.status !== undefined) taskFilterStatus.value = filters.status
    if (filters.type !== undefined) taskFilterType.value = filters.type
    if (filters.author !== undefined) taskFilterAuthor.value = filters.author
    if (filters.keyword !== undefined) taskKeyword.value = filters.keyword
    if (filters.sortBy !== undefined) taskSortBy.value = filters.sortBy
    if (filters.sortOrder !== undefined) taskSortOrder.value = filters.sortOrder
    taskPage.value = 1
    await fetchTasks({ page: 1 })
  }

  // 创建下载任务
  const createTask = async (params: CreateTaskParams) => {
    loading.value = true
    error.value = ''
    // 清除之前的消息
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
    }
    currentTask.value = null
    
    try {
      const response = await douyinApi.createTask(params) as unknown as ApiResponse
      currentTask.value = response
      
      console.log('[Store] createTask response:', response)
      
      if (response.success) {
        // 刷新任务列表
        await fetchTasks()
        
        // 检查是否是重复任务
        if (response.data?.is_duplicate) {
          console.log('[Store] 检测到重复任务，延长显示时间')
          autoClearTaskMessage(8000) // 重复任务显示8秒
        } else {
          console.log('[Store] 新任务，正常显示时间')
          autoClearTaskMessage(5000) // 新任务显示5秒
        }
      } else {
        autoClearTaskMessage(5000)
      }
      
      return response
    } catch (err: any) {
      error.value = err.message || '创建任务失败'
      currentTask.value = { success: false, message: err.message || '创建任务失败' }
      autoClearTaskMessage(5000)
      throw err
    } finally {
      loading.value = false
    }
  }

  // 重试任务
  const retryTask = async (taskId: string) => {
    try {
      const response = await douyinApi.retryTask(taskId) as unknown as ApiResponse
      if (response.success) {
        await fetchTasks()
      }
      return response
    } catch (err: any) {
      error.value = err.message || '重试任务失败'
      throw err
    }
  }

  const continueTask = async (taskId: string) => {
    try {
      const response = await douyinApi.continueTask(taskId) as unknown as ApiResponse
      if (response.success) await fetchTasks({ silent: true })
      return response
    } catch (err: any) {
      error.value = err.message || '继续任务失败'
      throw err
    }
  }

  const cancelTask = async (taskId: string) => {
    try {
      const response = await douyinApi.cancelTask(taskId) as unknown as ApiResponse
      if (response.success) await fetchTasks({ silent: true })
      return response
    } catch (err: any) {
      error.value = err.message || '取消任务失败'
      throw err
    }
  }

  // 删除任务
  const deleteTask = async (taskId: string, deleteFiles = false) => {
    try {
      const response = await douyinApi.deleteTask(taskId, deleteFiles) as unknown as ApiResponse
      if (response.success) {
        tasks.value = tasks.value.filter(t => t.id !== taskId)
      }
      return response
    } catch (err: any) {
      error.value = err.message || '删除任务失败'
      throw err
    }
  }

  // 启动任务列表轮询
  const startTaskPolling = () => {
    if (taskPollingInterval.value) {
      clearInterval(taskPollingInterval.value)
    }
    console.log('[Store] Starting task list polling...')
    
    taskPollingInterval.value = window.setInterval(async () => {
      await fetchTasks({ status: taskFilterStatus.value || undefined, silent: true })
    }, 10000) // 轮询仅作为 WebSocket 降级兜底
  }

  // 停止任务列表轮询
  const stopTaskPolling = () => {
    if (taskPollingInterval.value) {
      clearInterval(taskPollingInterval.value)
      taskPollingInterval.value = null
      console.log('[Store] Task list polling stopped')
    }
  }

  // ========== WebSocket 相关方法 ==========

  const refreshVisiblePageSoon = () => {
    if (wsRefreshTimer) clearTimeout(wsRefreshTimer)
    wsRefreshTimer = window.setTimeout(() => {
      fetchTasks({
        status: taskFilterStatus.value || undefined,
        page: taskPage.value,
        pageSize: taskPageSize.value,
        silent: true
      })
      wsRefreshTimer = null
    }, 500)
  }

  // 启动WebSocket连接
  const startWebSocket = () => {
    if (!useWebSocket.value) {
      console.log('[Store] WebSocket已禁用，使用轮询模式')
      startTaskPolling()
      return
    }

    console.log('[Store] 启动WebSocket连接...')

    // 注册状态变更回调
    const unsubscribeStatus = wsService.onStatusChange((status) => {
      wsStatus.value = status
      console.log('[Store] WebSocket状态:', status)

      // 如果连接断开且启用了WebSocket，回退到轮询
      if (status === 'disconnected' || status === 'error') {
        if (useWebSocket.value && !taskPollingInterval.value) {
          console.log('[Store] WebSocket断开，回退到轮询模式')
          startTaskPolling()
        }
      }

      // 如果连接成功，停止轮询
      if (status === 'connected' && taskPollingInterval.value) {
        console.log('[Store] WebSocket连接成功，停止轮询')
        stopTaskPolling()
      }
    })

    // WebSocket init only signals a sync; HTTP owns pagination and filters.
    const unsubscribeInit = wsService.onInit((data) => {
      console.log('[Store] WebSocket初始化数据:', data)
      if (data?.sync_required) refreshVisiblePageSoon()
    })

    // 注册任务更新回调
    const unsubscribeTaskUpdate = wsService.onTaskUpdate((task) => {
      console.log('[Store] WebSocket任务更新:', task.id, task.status)
      // Optimistically update visible rows, then debounce a filtered page sync.
      const index = tasks.value.findIndex(t => t.id === task.id)
      if (index !== -1) {
        tasks.value[index] = task
      }
      refreshVisiblePageSoon()
    })

    // 保存注销函数
    wsUnsubscribeCallbacks = [unsubscribeStatus, unsubscribeInit, unsubscribeTaskUpdate]

    // 连接WebSocket
    wsService.connect()
  }

  // 停止WebSocket连接
  const stopWebSocket = () => {
    console.log('[Store] 停止WebSocket连接...')

    // 注销所有回调
    wsUnsubscribeCallbacks.forEach(unsubscribe => unsubscribe())
    wsUnsubscribeCallbacks = []

    // 断开WebSocket
    wsService.disconnect()

    // 停止轮询（如果有）
    stopTaskPolling()
    if (wsRefreshTimer) {
      clearTimeout(wsRefreshTimer)
      wsRefreshTimer = null
    }
  }

  // 手动清除任务消息
  const clearTaskMessage = () => {
    if (taskMessageTimer) {
      clearTimeout(taskMessageTimer)
      taskMessageTimer = null
    }
    currentTask.value = null
    console.log('[Store] 任务消息已手动清除')
  }

  // 重新连接WebSocket（手动刷新时使用）
  const reconnectWebSocket = () => {
    console.log('[Store] 重新连接WebSocket...')
    stopWebSocket()
    // 短暂延迟后重新连接
    setTimeout(() => {
      startWebSocket()
    }, 100)
  }

  // 切换WebSocket/轮询模式
  const toggleWebSocketMode = (enabled: boolean) => {
    useWebSocket.value = enabled
    if (enabled) {
      stopTaskPolling()
      startWebSocket()
    } else {
      stopWebSocket()
      startTaskPolling()
    }
  }

  return {
    // State
    loading,
    tasksLoading,
    error,
    currentTask,
    cookieStatus,
    cookieStarting,
    cookieChecking,
    cookieRefresh,
    downloadPath,
    tasks,
    taskFilterStatus,
    taskFilterType,
    taskFilterAuthor,
    taskAuthors,
    taskKeyword,
    taskSortBy,
    taskSortOrder,
    taskPage,
    taskPageSize,
    taskTotal,
    wsStatus,
    useWebSocket,
    // Getters
    isCookieValid,
    // Actions
    checkCookieStatus,
    refreshCookie,
    startStatusPolling,
    stopStatusPolling,
    downloadVideo,
    getMixInfo,
    getUserInfo,
    downloadMix,
    downloadUserPosts,
    fetchTasks,
    setTaskFilters,
    goToPage,
    changePageSize,
    createTask,
    retryTask,
    continueTask,
    cancelTask,
    deleteTask,
    startTaskPolling,
    stopTaskPolling,
    startWebSocket,
    stopWebSocket,
    reconnectWebSocket,
    toggleWebSocketMode,
    clearTaskMessage,
    checkServiceStatus
  }
})
