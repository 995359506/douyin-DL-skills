/**
 * WebSocket服务模块
 * 
 * 提供实时任务状态更新，支持自动重连和降级到轮询
 */

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

export interface WebSocketMessage {
  type: 'init' | 'task_update' | 'pong' | 'error'
  data?: any
}

export type TaskUpdateCallback = (task: any) => void
export type StatusChangeCallback = (status: WebSocketStatus) => void

class WebSocketService {
  private ws: WebSocket | null = null
  private url: string = ''
  private reconnectAttempts: number = 0
  private reconnectDelay: number = 3000
  private heartbeatInterval: number = 30000
  private heartbeatTimer: number | null = null
  private reconnectTimer: number | null = null
  private status: WebSocketStatus = 'disconnected'
  private shouldReconnect = true
  private lastError = ''
  
  // 回调函数
  private onTaskUpdateCallbacks: TaskUpdateCallback[] = []
  private onStatusChangeCallbacks: StatusChangeCallback[] = []
  private onInitCallbacks: ((data: any) => void)[] = []

  /**
   * 初始化WebSocket连接
   */
  connect(url?: string) {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      console.log('[WebSocket] 已经连接')
      return
    }

    this.shouldReconnect = true

    // 构建WebSocket URL
    if (!url) {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      // 开发和生产均连接当前页面 host；Vite 在开发模式代理 /ws。
      url = `${protocol}//${window.location.host}/ws`
    }
    this.url = url

    this.setStatus('connecting')
    console.log(`[WebSocket] 正在连接到: ${url}`)

    try {
      this.ws = new WebSocket(url)
      this.setupEventHandlers()
    } catch (error) {
      console.error('[WebSocket] 连接失败:', error)
      this.setStatus('error')
      this.scheduleReconnect()
    }
  }

  /**
   * 设置事件处理器
   */
  private setupEventHandlers() {
    if (!this.ws) return

    this.ws.onopen = () => {
      console.log('[WebSocket] 连接成功')
      this.lastError = ''
      this.setStatus('connected')
      this.reconnectAttempts = 0
      this.startHeartbeat()
    }

    this.ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data)
        this.handleMessage(message)
      } catch (error) {
        console.error('[WebSocket] 解析消息失败:', error)
      }
    }

    this.ws.onclose = async (event) => {
      console.log('[WebSocket] 连接关闭:', event.code, event.reason)
      this.lastError = event.reason || ''
      this.setStatus('disconnected')
      this.stopHeartbeat()
      if (!event.reason && this.shouldReconnect) {
        try {
          const response = await fetch('/api/connection/status')
          const payload = await response.json()
          const allowed: string[] = payload?.data?.allowed_local_origins || []
          if (allowed.length && !allowed.includes(window.location.origin)) {
            this.lastError = 'Origin 与监听端口不一致'
            this.setStatus('error')
          }
        } catch {
          // 网络断开时保留轮询状态。
        }
      }
      
      // 非主动关闭，尝试重连
      if (this.shouldReconnect) {
        this.scheduleReconnect()
      }
    }

    this.ws.onerror = (error) => {
      console.error('[WebSocket] 连接错误:', error)
      this.setStatus('error')
    }
  }

  /**
   * 处理收到的消息
   */
  private handleMessage(message: WebSocketMessage) {
    console.log('[WebSocket] 收到消息:', message.type)

    switch (message.type) {
      case 'init':
        // 初始化只发送同步信号；任务列表仍由带分页/筛选的 HTTP 请求提供。
        this.onInitCallbacks.forEach(callback => {
          try {
            callback(message.data)
          } catch (error) {
            console.error('[WebSocket] init回调执行失败:', error)
          }
        })
        break

      case 'task_update':
        // 任务状态更新
        this.onTaskUpdateCallbacks.forEach(callback => {
          try {
            callback(message.data)
          } catch (error) {
            console.error('[WebSocket] task_update回调执行失败:', error)
          }
        })
        break

      case 'pong':
        // 心跳响应
        console.log('[WebSocket] 心跳响应')
        break

      case 'error':
        console.error('[WebSocket] 服务器错误:', message.data)
        break

      default:
        console.warn('[WebSocket] 未知消息类型:', message.type)
    }
  }

  /**
   * 发送消息
   */
  send(message: object) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message))
    } else {
      console.warn('[WebSocket] 未连接，无法发送消息')
    }
  }

  /**
   * 发送心跳
   */
  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = window.setInterval(() => {
      this.send({ type: 'ping' })
    }, this.heartbeatInterval)
  }

  /**
   * 停止心跳
   */
  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  /**
   * 计划重连
   */
  private scheduleReconnect() {
    if (!this.shouldReconnect || this.reconnectTimer) return

    this.reconnectAttempts++
    const delay = Math.min(this.reconnectDelay * Math.max(1, this.reconnectAttempts), 30000)
    
    console.log(`[WebSocket] ${delay}ms后尝试第${this.reconnectAttempts}次重连...`)
    
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect(this.url)
    }, delay)
  }

  /**
   * 设置状态
   */
  private setStatus(status: WebSocketStatus) {
    if (this.status !== status) {
      this.status = status
      this.onStatusChangeCallbacks.forEach(callback => {
        try {
          callback(status)
        } catch (error) {
          console.error('[WebSocket] 状态回调执行失败:', error)
        }
      })
    }
  }

  /**
   * 获取当前状态
   */
  getStatus(): WebSocketStatus {
    return this.status
  }

  getLastError(): string {
    return this.lastError
  }

  /**
   * 是否已连接
   */
  isConnected(): boolean {
    return this.status === 'connected'
  }

  /**
   * 注册任务更新回调
   */
  onTaskUpdate(callback: TaskUpdateCallback) {
    this.onTaskUpdateCallbacks.push(callback)
    
    // 返回注销函数
    return () => {
      const index = this.onTaskUpdateCallbacks.indexOf(callback)
      if (index > -1) {
        this.onTaskUpdateCallbacks.splice(index, 1)
      }
    }
  }

  /**
   * 注册状态变更回调
   */
  onStatusChange(callback: StatusChangeCallback) {
    this.onStatusChangeCallbacks.push(callback)
    
    return () => {
      const index = this.onStatusChangeCallbacks.indexOf(callback)
      if (index > -1) {
        this.onStatusChangeCallbacks.splice(index, 1)
      }
    }
  }

  /**
   * 注册初始化回调
   */
  onInit(callback: (data: any) => void) {
    this.onInitCallbacks.push(callback)
    
    return () => {
      const index = this.onInitCallbacks.indexOf(callback)
      if (index > -1) {
        this.onInitCallbacks.splice(index, 1)
      }
    }
  }

  /**
   * 手动请求任务列表
   */
  requestTasks() {
    this.send({ type: 'get_tasks' })
  }

  /**
   * 断开连接
   */
  disconnect() {
    this.shouldReconnect = false
    // 清除重连定时器
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    this.stopHeartbeat()
    
    if (this.ws) {
      // 标记为正常关闭，不触发重连
      this.ws.close(1000, 'Client disconnected')
      this.ws = null
    }
    
    this.setStatus('disconnected')
    console.log('[WebSocket] 已断开连接')
  }
}

// 导出单例
export const wsService = new WebSocketService()
export default wsService
