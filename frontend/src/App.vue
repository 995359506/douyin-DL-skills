<template>
  <div class="app-shell">
    <header class="site-header">
      <div class="header-inner">
        <a class="brand" href="#top" aria-label="回到赛博蒸馏器首页">
          <span class="brand-mark" aria-hidden="true"><CaretRight /></span>
          <span><span class="brand-name">D·L WORKBENCH</span><span class="brand-caption">赛博蒸馏器 / 本地内容工作台</span></span>
        </a>
        <div class="header-meta" aria-label="工作台状态">
          <span class="header-kicker">LOCAL / PRIVATE</span>
          <span class="cookie-chip" :class="cookieState.className"><span class="status-dot" aria-hidden="true"></span>{{ cookieState.label }}</span>
          <div class="header-cookie-actions">
            <el-button link size="small" class="header-action" aria-label="检查 Cookie 状态" :loading="cookieChecking" @click="handleCheckCookieStatus"><el-icon aria-hidden="true"><Refresh /></el-icon><span class="header-action-label">检查</span></el-button>
            <el-button size="small" class="header-login-action" :aria-label="cookieRefresh.refreshing ? '等待扫码登录' : '扫码登录'" :loading="cookieStarting" :disabled="cookieRefresh.refreshing" @click="handleRefreshCookie"><el-icon aria-hidden="true"><Unlock /></el-icon><span class="header-action-label">{{ cookieRefresh.refreshing ? '等待扫码' : '扫码登录' }}</span></el-button>
          </div>
        </div>
      </div>
    </header>

    <main id="top" class="main-content">
      <section class="hero-section" aria-labelledby="hero-title">
        <img class="hero-image" src="/images/douyin-distill-hero.jpg" alt="" aria-hidden="true" />
        <div class="hero-copy">
          <p class="eyebrow">01 / DISTILL THE SIGNAL</p>
          <h1 id="hero-title">把视频，<em>炼成可复用的表达。</em></h1>
          <p class="hero-description">
            <span class="hero-description-full">单条下载或主页批量扫描；在本地提取口播、总结要点、梳理内容，把视频沉淀成可复用的知识与表达。原片和结果都留在本机。</span>
            <span class="hero-description-compact">下载视频、扫描主页、提取口播与要点；素材和结果都留在本机，随时复用。</span>
          </p>
        </div>
        <div class="hero-signal" aria-label="内容蒸馏流程"><span>采集</span><i aria-hidden="true"></i><span>蒸馏</span><i aria-hidden="true"></i><span>复用</span></div>
      </section>

      <el-alert v-if="!cookieStatus.valid" class="login-gate" type="warning" title="下载功能已锁定" description="请先点击顶部“扫码登录”打开抖音登录窗口；扫码完成后登录状态会自动刷新。" show-icon :closable="false" />
      <el-alert v-if="cookieRefresh.refreshing" class="login-gate" type="info" title="等待扫码" :description="cookieRefresh.message || '请在新打开的抖音浏览器窗口完成扫码；此页面会自动更新状态。'" show-icon :closable="false" />
      <el-alert v-else-if="cookieRefresh.status === 'failed' || cookieRefresh.status === 'timeout'" class="login-gate" type="error" title="扫码登录未完成" :description="cookieRefreshErrorText" show-icon :closable="false" />

      <section class="workspace-grid" aria-label="下载入口">
        <article class="capture-panel primary-panel universal-panel">
          <div class="panel-heading">
            <div><span class="panel-index">A</span><h2>万能入口</h2></div>
            <span class="panel-note">视频 / 主页 / 合集</span>
          </div>
          <p class="panel-intro">粘贴一段抖音分享内容，系统会自动识别资源类型并给出下一步操作。</p>
          <div class="capture-form">
            <label for="video-source">分享文案或链接</label>
            <DouyinShareInput input-id="video-source" v-model="videoShareText" kind="any" placeholder="粘贴抖音分享文案、视频、主页或合集链接" action-label="识别并继续" :loading="universalBusy" :disabled="!cookieStatus.valid" @submit="handleUniversalSubmit" />
            <div class="field-helper">支持完整分享文案、纯短链、Markdown 链接和中文标点。只接受 http / https 的 douyin.com 合法子域。</div>
          </div>
          <div v-if="universalBusy" class="universal-state is-loading" role="status" aria-live="polite">正在判断这是视频、用户主页还是合集……</div>
          <div v-else-if="universalError" class="universal-state is-error" role="alert">{{ universalError }}</div>
          <div v-else-if="universalKind === 'video' && universalUrl" class="universal-result" aria-live="polite">
            <div><strong>视频链接已识别</strong><small>确认后才会创建下载任务</small></div>
            <el-button class="primary-action" type="primary" :loading="loading" @click="confirmSingleDownload">确认下载</el-button>
          </div>
          <div v-else-if="universalKind === 'uncertain' && universalUrl" class="universal-result universal-result--uncertain" aria-live="polite">
            <div>
              <strong>{{ universalUserIntent ? '疑似用户主页，尚未验证' : '先按单视频处理' }}</strong>
              <small>{{ universalUserIntent ? '分享文案提示这是主页；只有后端验证成功后才会进入主页作品解析。' : '这段分享内容已提取出视频链接，暂未确认是包含多条作品的合集。' }}</small>
              <span v-if="universalMixProbeError" class="universal-probe-error" role="status">{{ universalMixProbeError }}</span>
            </div>
            <div class="universal-result-actions">
              <el-button class="primary-action" type="primary" :loading="loading" @click="confirmSingleDownload">按单视频继续</el-button>
              <el-button v-if="universalUserIntent" link type="primary" :loading="universalUserProbeBusy" @click="handleUniversalUserProbe">重试主页验证</el-button>
              <el-button v-else link type="primary" :loading="universalMixProbeBusy" @click="handleUniversalMixProbe">查看合集</el-button>
            </div>
          </div>
          <div v-else-if="universalKind === 'user' && userInfo" class="universal-result" aria-live="polite">
            <div class="entity-preview"><span class="entity-avatar" aria-hidden="true"><User /></span><span><strong>{{ userInfo.nickname }}</strong><small>{{ userInfo.aweme_count || '作品数量未知' }} · 后端已验证</small></span></div>
            <el-button class="primary-action" type="primary" :loading="loading" @click="showUserScanner = true">解析主页作品</el-button>
          </div>
          <div v-else-if="universalKind === 'mix' && mixInfo" class="universal-result mix-result" aria-live="polite">
            <div class="entity-preview"><span class="entity-avatar coral-bg" aria-hidden="true"><Collection /></span><span><strong>{{ mixInfo.mix_name }}</strong><small>{{ mixInfo.total_videos || '数量未知' }} 条视频</small></span></div>
            <el-button class="primary-action" type="primary" :loading="loading" @click="handleDownloadMixWithOptions(mixInfo.mix_id, mixInfo.mix_name)">确认范围并下载</el-button>
          </div>
          <el-alert v-if="currentTask" class="task-feedback" :title="currentTask.message" :type="getAlertType(currentTask)" show-icon closable @close="handleCloseTaskMessage" />
        </article>
      </section>

      <UserPostScanner
        v-if="showUserScanner && userInfo"
        :profile="userInfo"
        :user-url="verifiedUserUrl"
        @close="showUserScanner = false"
        @submitted="handleUserScanSubmitted"
      />

      <DownloadRecords />
    </main>

    <footer class="site-footer"><span>F2 / D·L WORKBENCH</span><a href="https://f2.wiki" target="_blank" rel="noreferrer">查看文档 <el-icon aria-hidden="true"><TopRight /></el-icon></a></footer>
  </div>
</template>
<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { ElCheckbox, ElMessage, ElMessageBox } from 'element-plus'
import { storeToRefs } from 'pinia'
import { useDownloadStore } from '@/stores/download'
import DownloadRecords from '@/components/DownloadRecords.vue'
import DouyinShareInput from '@/components/DouyinShareInput.vue'
import UserPostScanner from '@/components/UserPostScanner.vue'
import { classifyUniversalInput, classifyUniversalUrl, hasConfirmedMultiVideoMix } from '@/utils/universalRouting'

const store = useDownloadStore()

// 使用storeToRefs解构响应式状态
const { currentTask, cookieStarting, cookieChecking, cookieRefresh } = storeToRefs(store)

// 使用方法直接解构（方法不需要响应性）
const { 
  checkCookieStatus,
  refreshCookie,
  downloadVideo,
  getMixInfo,
  getUserInfo,
  downloadMix,
  clearTaskMessage,
  checkServiceStatus
} = store

// 计算属性
const loading = computed(() => store.loading)
const cookieStatus = computed(() => store.cookieStatus)
const downloadPath = computed(() => store.downloadPath)
const cookieState = computed(() => cookieStatus.value.valid
  ? (cookieRefresh.value.refreshing ? { label: '等待扫码', className: 'is-warning' } : { label: '登录有效', className: 'is-valid' })
  : cookieRefresh.value.refreshing ? { label: '等待扫码', className: 'is-warning' }
    : cookieRefresh.value.status === 'failed' || cookieRefresh.value.status === 'timeout' ? { label: '扫码失败', className: 'is-warning' }
      : cookieStatus.value.exists ? { label: '需要刷新', className: 'is-warning' } : { label: '未登录', className: 'is-empty' })
const cookieRefreshErrorText = computed(() => [cookieRefresh.value.error?.message, cookieRefresh.value.error?.action].filter(Boolean).join('。') || cookieRefresh.value.message)

// 表单数据
const videoShareText = ref('')
const autoSummary = ref(true)
type UniversalKind = 'video' | 'user' | 'mix' | 'uncertain'
const universalBusy = ref(false)
const universalMixProbeBusy = ref(false)
const universalUserProbeBusy = ref(false)
const universalMixProbeError = ref('')
const universalUserIntent = ref(false)
const universalError = ref('')
const universalKind = ref<UniversalKind | null>(null)
const universalUrl = ref('')
const mixInfo = ref<any>(null)

// 用户作品下载相关
const userInfo = ref<any>(null)
const showUserScanner = ref(false)
const verifiedUserUrl = ref('')

// 初始化
onMounted(() => {
  checkCookieStatus()
  checkServiceStatus()
  currentTask.value = null
})

const ensureCookie = () => {
  if (cookieStatus.value.valid) return true
  ElMessage.warning('请先在 Cookie 管理中完成扫码登录')
  return false
}

const confirmBatchDownload = async (
  label: string,
  maxCounts: number | null,
  interval: string = 'all'
) => {
  const countText = maxCounts ? `最多 ${maxCounts} 个` : '全部（可能耗时较长）'
  const dateText = interval === 'all' ? '全部日期' : interval
  await ElMessageBox.confirm(
    h('div', { style: 'display:grid;gap:8px;line-height:1.5' }, [
      h('strong', label),
      h('div', `数量：${countText}`),
      h('div', `日期：${dateText}`),
      h('div', { style: 'word-break:break-all' }, `保存位置：${downloadPath.value}`)
    ]),
    '确认下载范围',
    {
      confirmButtonText: '确认创建任务',
      cancelButtonText: '返回修改',
      type: maxCounts ? 'info' : 'warning',
      dangerouslyUseHTMLString: false
    }
  )
}

// 检查Cookie状态
const handleCheckCookieStatus = async () => {
  try {
    const response = await checkCookieStatus()
    if (response?.success) {
      const { exists, valid } = response.data || {}
      if (cookieRefresh.value.refreshing) {
        ElMessage.info('扫码登录正在进行，请在抖音浏览器窗口完成扫码')
      } else if (valid) {
        ElMessage.success('登录状态有效')
      } else if (exists) {
        ElMessage.warning('Cookie存在但无效，请刷新')
      } else {
        ElMessage.warning('未登录，请刷新Cookie')
      }
    }
  } catch (error) {
    ElMessage.error('检查状态出错')
  }
}

// 刷新Cookie
const handleRefreshCookie = async () => {
  try {
    await refreshCookie(false)
    if (cookieRefresh.value.status === 'failed') {
      ElMessage.error(cookieRefreshErrorText.value)
    } else {
      ElMessage.info(cookieRefresh.value.message || '登录窗口已启动，请在新窗口扫码；本页面会自动更新状态')
    }
  } catch (error: any) {
    console.error('Refresh error:', error)
    const message = error.action
      ? `${error.message || '扫码登录失败'}。${error.action}`
      : error.message || '扫码登录失败，请检查本地服务'
    ElMessage.error(message)
  }
}

const handleDownloadVideo = async (url: string) => {
  if (!ensureCookie()) return false
  try {
    const response = await downloadVideo(url, autoSummary.value)
    if (!response.success) throw new Error(response.message || '下载任务创建失败')
    ElMessage.success(autoSummary.value ? '下载已开始，完成后将自动生成视频总结' : '下载请求已发送')
    videoShareText.value = ''
    return true
  } catch (error: any) {
    ElMessage.error(error.message || '下载失败')
    return false
  }
}

const handleUniversalSubmit = async (url: string, shareText: string) => {
  if (!ensureCookie()) return
  universalBusy.value = true
  universalMixProbeBusy.value = false
  universalMixProbeError.value = ''
  universalError.value = ''
  universalKind.value = null
  universalUrl.value = url
  userInfo.value = null
  mixInfo.value = null
  verifiedUserUrl.value = ''
  const urlKind = classifyUniversalUrl(url)
  let kind = classifyUniversalInput(url, shareText)
  const inferredUser = urlKind === 'uncertain' && kind === 'user'
  universalUserIntent.value = inferredUser
  try {
    if (kind === 'user') {
      try {
        await handleGetUserInfo(url)
      } catch (error: any) {
        if (!inferredUser) throw error
        kind = 'uncertain'
        universalMixProbeError.value = `主页线索未通过后端验证：${error?.message || '链接暂时无法确认'}。可重试验证，或手动按单视频继续。`
      }
    } else if (kind === 'mix') {
      const response = await getMixInfo(url)
      if (response.success && hasConfirmedMultiVideoMix(response.data)) {
        mixInfo.value = response.data
      } else {
        kind = 'uncertain'
        universalMixProbeError.value = '后端未确认这是包含 2 条以上作品的合集，请选择单视频继续或再次查看合集。'
      }
    }
    universalKind.value = kind
  } catch (error: any) {
    universalError.value = error?.message || '无法识别这段内容，请重新粘贴'
  } finally {
    universalBusy.value = false
  }
}

const handleUniversalUserProbe = async () => {
  if (!universalUrl.value || !ensureCookie()) return
  universalUserProbeBusy.value = true
  universalMixProbeError.value = ''
  try {
    await handleGetUserInfo(universalUrl.value)
    universalKind.value = 'user'
    universalUserIntent.value = false
  } catch (error: any) {
    userInfo.value = null
    verifiedUserUrl.value = ''
    universalMixProbeError.value = `主页尚未验证：${error?.message || '链接暂时无法确认'}。`
  } finally {
    universalUserProbeBusy.value = false
  }
}

const handleUniversalMixProbe = async () => {
  if (!universalUrl.value || !ensureCookie()) return
  universalMixProbeBusy.value = true
  universalMixProbeError.value = ''
  try {
    const response = await getMixInfo(universalUrl.value)
    if (response.success && hasConfirmedMultiVideoMix(response.data)) {
      mixInfo.value = response.data
      universalKind.value = 'mix'
      return
    }
    universalMixProbeError.value = '后端没有确认这是包含 2 条以上作品的合集，请按单视频继续。'
  } catch (error: any) {
    universalMixProbeError.value = error?.message || '暂时无法确认合集，请按单视频继续。'
  } finally {
    universalMixProbeBusy.value = false
  }
}

const confirmSingleDownload = async () => {
  if (!universalUrl.value) return
  try {
    await ElMessageBox.confirm(
      h('div', { style: 'display:grid;gap:8px;line-height:1.5' }, [
        h('strong', '确认下载这条视频？'),
        h('div', { style: 'word-break:break-all' }, universalUrl.value),
        h('div', `保存位置：${downloadPath.value}`),
        h(ElCheckbox, { modelValue: autoSummary.value, 'onUpdate:modelValue': (value: boolean | string | number) => { autoSummary.value = Boolean(value) } }, () => '下载完成后自动生成视频总结（会调用 AI 接口）'),
      ]),
      '确认下载',
      { confirmButtonText: '确认下载', cancelButtonText: '返回', type: 'info', dangerouslyUseHTMLString: false },
    )
    if (await handleDownloadVideo(universalUrl.value)) {
      universalKind.value = null
      universalUrl.value = ''
    }
  } catch (error: any) {
    if (error !== 'cancel') universalError.value = error?.message || '下载失败'
  }
}

// 下载合集（带选项）
const handleDownloadMixWithOptions = async (mixId: string, mixName: string = '') => {
  if (!ensureCookie()) return
  if (!mixId) {
    ElMessage.warning('请输入合集ID')
    return
  }
  const maxCounts = 20
  try {
    await confirmBatchDownload(`合集：${mixName || mixId}`, maxCounts)
    await downloadMix(mixId, mixName, maxCounts)
    const countMsg = maxCounts ? `（限制${maxCounts}个）` : '（全部）'
    ElMessage.success(`合集下载请求已发送${countMsg}`)
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error.message || '合集下载失败')
  }
}

// 获取用户信息
const handleGetUserInfo = async (url: string) => {
  if (!ensureCookie()) return
  try {
    const response = await getUserInfo(url)
    if (response.success) {
      userInfo.value = response.data
      verifiedUserUrl.value = url
      ElMessage.success('用户资料已由后端验证')
    } else throw new Error(response.message || '获取用户信息失败')
  } catch (error: any) {
    userInfo.value = null
    verifiedUserUrl.value = ''
    throw new Error(error.message || '获取用户信息失败')
  }
}

const handleUserScanSubmitted = () => {
  showUserScanner.value = false
  ElMessage.success('已按所选作品创建下载任务')
}

// 关闭任务消息
const handleCloseTaskMessage = () => {
  clearTaskMessage()
}

// 获取告警类型
const getAlertType = (task: any): string => {
  if (!task.success) return 'error'
  if (task.data?.is_duplicate) return 'warning'
  return 'success'
}
</script>
