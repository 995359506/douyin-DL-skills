<template>
  <section class="scan-workbench" aria-labelledby="scan-title">
    <header class="scan-header">
      <div>
        <p class="scan-kicker">USER ARCHIVE / CONTACT SHEET</p>
        <h2 id="scan-title">{{ profile.nickname }}的作品胶片</h2>
        <p>扫描只读取作品元数据；确认前不会下载任何媒体。</p>
      </div>
      <el-button class="scan-close" aria-label="关闭作品扫描工作台" @click="$emit('close')"><el-icon aria-hidden="true"><Close /></el-icon>关闭</el-button>
    </header>

    <ol class="scan-steps" aria-label="用户作品下载步骤">
      <li class="is-complete"><span>01</span><strong>验证主页</strong><small>已识别</small></li>
      <li :class="stepClass('scan')"><span>02</span><strong>解析作品</strong><small>{{ scanStepLabel }}</small></li>
      <li :class="stepClass('select')"><span>03</span><strong>选择作品</strong><small>{{ selectedTotal }} 个已选</small></li>
      <li :class="stepClass('confirm')"><span>04</span><strong>确认下载</strong><small>不会自动提交</small></li>
    </ol>

    <div v-if="booting" class="scan-loading" aria-live="polite">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="startupError" class="scan-state-panel is-failed" aria-live="assertive">
      <span class="state-icon" aria-hidden="true"><WarningFilled /></span>
      <div>
        <h3>无法启动作品解析</h3>
        <p>{{ startupError.message }}</p>
        <small v-if="startupError.stage_label">失败阶段：{{ startupError.stage_label }}</small>
        <small v-if="startupError.status_code">上游状态码：HTTP {{ startupError.status_code }}</small>
        <small v-if="startupError.action" class="state-action">建议：{{ startupError.action }}</small>
      </div>
      <el-button class="primary-action" type="primary" :loading="starting" @click="startFreshScan">重新解析</el-button>
    </div>

    <div v-else-if="scan && ['pending', 'scanning'].includes(scan.status)" class="scan-progress-panel" aria-live="polite">
      <div class="scan-progress-heading">
        <div><span class="live-dot" aria-hidden="true"></span><strong>{{ scan.status === 'pending' ? '准备解析作品目录' : '正在解析作品目录' }}</strong></div>
        <span>{{ formatElapsed(scan.elapsed_ms) }}</span>
      </div>
      <el-progress :percentage="scanProgress" :stroke-width="10" :show-text="false" />
      <div class="scan-metrics">
        <div><span>已发现</span><strong>{{ formatCount(scan.found_count) }}</strong><small>件作品</small></div>
        <div><span>读取分页</span><strong>{{ scan.pages_scanned }}</strong><small>{{ scan.cursor ? '游标继续中' : '等待游标' }}</small></div>
        <div><span>预计总数</span><strong>{{ scan.total_hint == null ? '—' : formatCount(scan.total_hint) }}</strong><small>仅供参考</small></div>
      </div>
      <p class="scan-time-hint">{{ scan.status === 'pending' ? '扫描任务已排队，正在连接抖音并准备读取主页。' : `正在继续解析：已发现 ${formatCount(scan.found_count)} / ${scan.total_hint == null ? '未知' : formatCount(scan.total_hint)} 条。扫描完成前不要提交下载，结果会自动更新。` }}</p>
      <div v-if="works.items.length" class="scan-preview-strip" aria-label="已解析作品预览">
        <div v-for="work in works.items.slice(0, 4)" :key="work.aweme_id"><span aria-hidden="true"><Picture v-if="work.kind === 'image'" /><VideoCamera v-else /></span><p>{{ work.title }}</p></div>
      </div>
      <el-button class="cancel-scan" :loading="cancelling" @click="cancelScan">取消解析</el-button>
    </div>

    <div v-else-if="scan && ['failed', 'cancelled', 'expired'].includes(scan.status)" class="scan-state-panel" :class="`is-${scan.status}`">
      <span class="state-icon" aria-hidden="true"><WarningFilled /></span>
      <div>
        <h3>{{ terminalTitle }}</h3>
        <p>{{ terminalMessage }}</p>
        <small v-if="scan.error?.stage_label">失败阶段：{{ scan.error.stage_label }}</small>
        <small v-if="scan.error?.status_code">上游状态码：HTTP {{ scan.error.status_code }}</small>
        <small v-if="scan.error?.action" class="state-action">建议：{{ scan.error.action }}</small>
      </div>
      <el-button class="primary-action" type="primary" :loading="starting" @click="startFreshScan">{{ scan.status === 'cancelled' ? '重新解析' : '重试解析' }}</el-button>
    </div>

    <template v-else-if="scan && (scan.status === 'completed' || scan.status === 'partial')">
      <el-alert v-if="scan.status === 'partial'" class="partial-alert" type="warning" :title="scan.error?.stage_label ? `${scan.error.stage_label}失败，已保留部分结果` : '解析部分完成'" :description="partialScanMessage" show-icon />

      <template v-if="!confirmMode && !distillConfirm && !distillRun && !submitted">
        <div class="scan-toolbar" role="search" aria-label="筛选作品">
          <el-input v-model="draftKeyword" class="filter-keyword" clearable placeholder="搜索作品标题" aria-label="搜索作品标题" @keyup.enter="applyFilters"><template #prefix><el-icon aria-hidden="true"><Search /></el-icon></template></el-input>
          <el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" range-separator="至" aria-label="筛选发布日期范围" />
          <el-select v-model="filters.kind" aria-label="筛选作品类型" @change="applyFilters"><el-option label="全部类型" value="all" /><el-option label="视频" value="video" /><el-option label="图文" value="image" /></el-select>
          <el-select v-model="filters.history" aria-label="筛选下载历史" @change="applyFilters"><el-option label="全部状态" value="all" /><el-option label="未下载" value="not_downloaded" /><el-option label="已下载" value="downloaded" /></el-select>
          <el-button class="filter-submit" @click="applyFilters"><el-icon aria-hidden="true"><Filter /></el-icon>应用筛选</el-button>
        </div>

        <div class="selection-toolbar">
          <div class="result-counts"><span>本页 <strong>{{ works.items.length }}</strong></span><span>当前筛选 <strong>{{ formatCount(works.matched_count) }}</strong></span><span>全部解析 <strong>{{ formatCount(works.total_count) }}</strong></span></div>
          <div class="selection-actions"><el-button @click="selectCurrentPage">选择本页</el-button><el-button type="primary" plain :disabled="works.matched_count === 0" :aria-pressed="allFilteredSelected" :title="allFilteredSelected ? '取消当前筛选结果的全选状态' : '全选当前筛选结果（包含未显示的分页）'" @click="toggleAllFiltered">{{ allFilteredSelected ? '取消全选' : '全选' }}</el-button><el-button :disabled="selectedTotal === 0" @click="clearSelection">清空选择</el-button></div>
        </div>

        <div v-if="selectedTotal" class="selection-banner" aria-live="polite">
          <el-icon aria-hidden="true"><CircleCheckFilled /></el-icon>
          <span>{{ selectionDescription }}</span>
          <small>本页已选 {{ selectedOnPage }} / {{ works.items.length }}</small>
        </div>

        <div v-if="loadingWorks" class="work-grid" aria-label="加载作品列表">
          <el-skeleton v-for="index in 8" :key="index" class="work-skeleton" animated><template #template><el-skeleton-item variant="image" class="skeleton-cover" /><el-skeleton-item variant="text" /><el-skeleton-item variant="text" style="width: 55%" /></template></el-skeleton>
        </div>
        <el-empty v-else-if="works.items.length === 0" description="当前筛选下没有作品"><el-button @click="resetFilters">清除筛选条件</el-button></el-empty>
        <div v-else class="work-grid" aria-label="作品选择列表">
          <article
            v-for="work in works.items"
            :key="work.aweme_id"
            class="work-card"
            :class="{ 'is-selected': isSelected(work) }"
            role="checkbox"
            tabindex="0"
            :aria-checked="isSelected(work)"
            :aria-label="`${isSelected(work) ? '取消选择' : '选择'}作品：${work.title}`"
            @click="toggle(work)"
            @keydown.enter.prevent="toggle(work)"
            @keydown.space.prevent="toggle(work)"
          >
            <div class="work-cover" :class="`kind-${work.kind}`">
              <img v-if="work.cover_url" :src="work.cover_url" alt="" loading="lazy" />
              <div v-else class="cover-placeholder"><el-icon aria-hidden="true"><Picture v-if="work.kind === 'image'" /><VideoCamera v-else /></el-icon><span>NO PREVIEW</span></div>
              <span class="work-check" aria-hidden="true"><Check v-if="isSelected(work)" /></span>
              <span class="work-kind">{{ work.kind === 'image' ? '图文' : formatDuration(work.duration_seconds) }}</span>
            </div>
            <div class="work-copy"><h3>{{ work.title }}</h3><div><time :datetime="work.published_at">{{ formatDate(work.published_at) }}</time><span v-if="work.downloaded" class="history-badge">已下载</span><span v-else>未下载</span></div></div>
          </article>
        </div>

        <nav v-if="works.items.length" class="cursor-pagination" aria-label="作品分页">
          <el-button :disabled="pageIndex === 0" @click="previousPage"><el-icon aria-hidden="true"><ArrowLeft /></el-icon>上一页</el-button>
          <span>第 {{ pageIndex + 1 }} 页</span>
          <el-button :disabled="!works.next_cursor" @click="nextPage">下一页<el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button>
        </nav>

        <aside v-if="selectedTotal" class="download-summary" aria-label="已选作品摘要">
          <div><span>已选作品</span><strong>{{ formatCount(selectedTotal) }}</strong><small>预计占用：未知，下载时计算</small></div>
          <div><span>保存位置</span><strong>{{ downloadPath || '后端默认目录' }}</strong><small>确认页可修改</small></div>
          <div class="summary-actions"><el-button class="primary-action" type="primary" @click="confirmMode = true">确认下载选项<el-icon aria-hidden="true"><ArrowRight /></el-icon></el-button><el-button class="distill-action" type="success" @click="distillConfirm = true">开始蒸馏</el-button></div>
        </aside>
      </template>

      <div v-else-if="confirmMode && !submitted" class="confirm-panel">
        <header><button type="button" class="back-button" @click="confirmMode = false"><el-icon aria-hidden="true"><ArrowLeft /></el-icon>返回修改选择</button><div><span>STEP 04</span><h3>确认下载</h3><p>最后核对选择范围与下载策略，提交后才会创建任务。</p></div></header>
        <div class="confirm-grid">
          <section><h4>选择摘要</h4><strong class="confirm-count">{{ formatCount(selectedTotal) }}</strong><p>{{ selectionDescription }}</p><dl><div><dt>预计占用</dt><dd>未知，下载时计算</dd></div><div><dt>扫描编号</dt><dd><code>{{ scan.scan_id }}</code></dd></div></dl></section>
          <section><h4>下载选项</h4><label for="download-path">保存位置</label><el-input id="download-path" v-model="downloadPath" placeholder="留空使用后端默认目录" /><p>下载队列固定同时处理 2 条，避免同时启动过多任务。</p></section>
        </div>
        <el-alert type="info" title="安全提示" description="只下载你明确选择的作品；不会保存 Cookie、不会自动扩大选择范围。" show-icon :closable="false" />
        <footer><el-button @click="confirmMode = false">返回选择</el-button><el-button class="coral-action final-download" type="primary" :loading="submitting" @click="submitDownload">下载已选 {{ formatCount(selectedTotal) }} 个作品</el-button></footer>
      </div>

      <div v-else-if="distillConfirm && !submitted" class="confirm-panel distill-confirm-panel">
        <header><button type="button" class="back-button" @click="distillConfirm = false"><el-icon aria-hidden="true"><ArrowLeft /></el-icon>返回修改选择</button><div><span>STEP 04 / DISTILL</span><h3>确认博主内容蒸馏</h3><p>视频分析口播与画面；图文仅提取已有正文，不下载图片。</p></div></header>
        <div class="confirm-grid"><section><h4>本次范围</h4><strong class="confirm-count">{{ formatCount(selectedTotal) }}</strong><p>{{ selectionDescription }}</p><dl><div><dt>视频</dt><dd>{{ selectedVideoCount }}</dd></div><div><dt>图文正文</dt><dd>{{ selectedImageCount }}（逐条 0 次模型调用）</dd></div><div><dt>低信息量</dt><dd>转写后确认</dd></div><div><dt>预筛模型调用</dt><dd>约 {{ selectedVideoCount * 2 }} 次</dd></div><div><dt>完整蒸馏模型调用</dt><dd>约 {{ estimatedDistillCalls }} 次</dd></div></dl></section><section><h4>运行策略</h4><el-radio-group v-model="stagePolicy" aria-label="蒸馏档位"><el-radio-button value="full">完整蒸馏</el-radio-button><el-radio-button value="preview">先预筛</el-radio-button></el-radio-group><p>默认复用本地文件和已有成功结果；单条失败不会阻断整批。归纳模型固定为 <code>gpt-5.6-luna</code>。</p><el-checkbox v-model="forceReanalyze">强制重新分析（不覆盖历史结果）</el-checkbox><p class="distill-path">产物目录：{{ distillOutputHint }}</p></section></div>
        <el-alert type="info" title="费用与耗时提示" description="实际耗时取决于视频数量、文件大小和模型限流；首次建议先选择 3 条。" show-icon :closable="false" />
        <footer><el-button @click="distillConfirm = false">返回选择</el-button><el-button class="coral-action final-download" type="primary" :loading="distillSubmitting" @click="submitDistill">确认蒸馏 {{ formatCount(selectedTotal) }} 条作品</el-button></footer>
      </div>

      <div v-else-if="distillRun && !submitted" class="distill-progress-panel" aria-live="polite">
        <div class="distill-progress-heading"><div><span class="live-dot" aria-hidden="true"></span><strong>博主内容蒸馏 · {{ distillStatusLabel(distillRun.status) }}</strong></div><span>{{ distillRun.completed_count + distillRun.degraded_count + distillRun.skipped_count + distillRun.failed_count + (distillRun.cancelled_count || 0) }} / {{ distillRun.total_count }}</span></div>
        <el-progress :percentage="distillProgress" :stroke-width="10" />
        <div class="distill-metrics"><span>已完成 {{ distillRun.completed_count }}</span><span>降级 {{ distillRun.degraded_count }}</span><span>未纳入归纳 {{ distillRun.skipped_count }}</span><span>失败 {{ distillRun.failed_count }}</span><span>已取消 {{ distillRun.cancelled_count || 0 }}</span><span>待处理 {{ distillRun.pending_count || 0 }}</span></div>
        <p v-if="distillRun.error" class="state-action">{{ distillRun.error.reason }}{{ distillRun.error.action ? `；建议：${distillRun.error.action}` : '' }}</p>
        <p v-if="distillRun.output_path" class="distill-artifact-hint">总纲和每条视频的独立 Markdown 文件已保存到下载目录；完整包会将它们一起打包。</p>
        <div v-if="distillItems.length" class="distill-item-list" aria-label="逐条蒸馏进度">
          <div v-for="item in distillItems" :key="item.aweme_id" class="distill-item-row">
            <div><strong>{{ String(item.ordinal).padStart(3, '0') }} · {{ item.metadata?.title || item.aweme_id }}</strong><small>{{ distillItemStatusLabel(item) }}</small><small v-if="distillRun.status === 'preview_ready'">{{ formatDuration(item.metadata?.duration_seconds) }} · {{ (item.polished_text || '').length }} 字</small></div>
            <span v-if="item.degrade_reason" class="state-action">{{ item.degrade_reason }}</span>
            <el-checkbox v-if="distillRun.status === 'preview_ready' && item.item_status === 'transcribed'" v-model="previewSelected[item.aweme_id]" :aria-label="`深看 ${item.metadata?.title || item.aweme_id}`" />
            <el-button v-if="item.item_status === 'failed' || item.item_status === 'summary_invalid'" size="small" :loading="distillActionBusy" @click="resumeDistill(item.aweme_id)">重试此项</el-button>
          </div>
        </div>
        <footer><el-button v-if="distillRun.status === 'preview_ready'" type="primary" :disabled="!Object.values(previewSelected).some(Boolean)" :loading="distillActionBusy" @click="deepAnalyzeSelected">深看所选</el-button><el-button v-if="['paused','failed','partial','cancelled'].includes(distillRun.status)" :loading="distillActionBusy" @click="resumeDistill">继续/重试失败项</el-button><el-button v-if="!['completed','partial','failed','cancelled','preview_ready'].includes(distillRun.status)" :loading="distillActionBusy" @click="cancelDistill">取消蒸馏</el-button><el-button v-if="distillRun.output_path" type="primary" @click="openDistillArtifact">查看总纲</el-button><el-button v-if="distillRun.output_path" @click="downloadDistillZip">下载完整包（含逐条文件）</el-button></footer>
      </div>

      <aside v-if="!distillRun && !distillConfirm && !confirmMode && distillHistory.length" class="distill-history" aria-label="蒸馏历史">
        <header><h3>最近蒸馏记录</h3><small>刷新页面后仍可继续已暂停或失败的运行</small></header>
        <button v-for="history in distillHistory" :key="history.run_id" type="button" class="distill-history-row" @click="openHistory(history)">
          <span><strong>{{ history.nickname }} · {{ formatDate(history.created_at || '') }}</strong><small>{{ history.completed_count + history.degraded_count + history.skipped_count + history.failed_count + (history.cancelled_count || 0) }} / {{ history.total_count }} 条</small></span>
          <em>{{ distillStatusLabel(history.status) }}</em>
        </button>
      </aside>

      <div v-else class="submitted-panel" aria-live="polite"><span aria-hidden="true"><CircleCheckFilled /></span><h3>任务已创建</h3><p>{{ submittedMessage }}</p><el-button @click="$emit('close')">返回首页</el-button></div>
    </template>
    <el-drawer v-model="artifactViewerOpen" title="蒸馏总纲" size="min(920px, 96vw)" destroy-on-close>
      <iframe v-if="artifactViewerOpen && artifactViewerUrl" class="artifact-viewer" :src="artifactViewerUrl" title="蒸馏总纲阅读视图" referrerpolicy="no-referrer"></iframe>
    </el-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, Check, CircleCheckFilled, Close, Filter, Picture, Search, VideoCamera, WarningFilled } from '@element-plus/icons-vue'
import { getUserPostScanService, USER_SCAN_STORAGE_KEY, type DistillRun, type UserPostScan, type UserPostScanService, type UserProfile, type UserWork, type WorkFilter, type WorkPage } from '@/services/userPostScan'
import { allFilteredSelection, emptySelection, isAllFilteredSelection, isWorkSelected, selectedCount, toScanSelection, toggleWork, type WorkSelection } from '@/utils/workSelection'

const props = defineProps<{ profile: UserProfile; userUrl: string }>()
const emit = defineEmits<{ close: []; submitted: [taskId: string] }>()
const pageSize = 24

const service = ref<UserPostScanService | null>(null)
const scan = ref<UserPostScan | null>(null)
const works = ref<WorkPage>({ items: [], matched_count: 0, total_count: 0, next_cursor: null })
const booting = ref(true)
const starting = ref(false)
const cancelling = ref(false)
const loadingWorks = ref(false)
const submitting = ref(false)
const submitted = ref(false)
const submittedMessage = ref('')
const startupError = ref<{ code?: string; message: string; action?: string; stage_label?: string; status_code?: number } | null>(null)
const confirmMode = ref(false)
const distillConfirm = ref(false)
const distillSubmitting = ref(false)
const distillActionBusy = ref(false)
const distillRun = ref<DistillRun | null>(null)
const distillItems = ref<Array<Record<string, any>>>([])
const distillHistory = ref<DistillRun[]>([])
const artifactViewerOpen = ref(false)
const forceReanalyze = ref(false)
const stagePolicy = ref<'preview' | 'full'>('full')
const previewSelected = reactive<Record<string, boolean>>({})
const distillPollTimer = ref<ReturnType<typeof setTimeout> | null>(null)
const selection = ref<WorkSelection>(emptySelection())
const cursorStack = ref<(string | null)[]>([null])
const pageIndex = ref(0)
const pollTimer = ref<ReturnType<typeof setTimeout> | null>(null)

const draftKeyword = ref('')
const dateRange = ref<[string, string] | null>(null)
const filters = reactive<WorkFilter>({ keyword: '', kind: 'all', history: 'all' })
const downloadPath = ref('')

const selectedTotal = computed(() => selectedCount(selection.value))
const selectedOnPage = computed(() => works.value.items.filter(item => isWorkSelected(selection.value, item)).length)
const selectedVideoCount = computed(() => selection.value.mode === 'all_filtered' ? selectedTotal.value : works.value.items.filter(item => isSelected(item) && item.kind === 'video').length)
const selectedImageCount = computed(() => selection.value.mode === 'all_filtered' ? 0 : works.value.items.filter(item => isSelected(item) && item.kind === 'image').length)
const estimatedDistillCalls = computed(() => {
  const videos = selectedVideoCount.value
  if (!selectedTotal.value) return 0
  const groups = Math.max(1, Math.ceil(selectedTotal.value / 8))
  return videos * 3 + groups + (groups > 1 ? 1 : 0)
})
const distillProgress = computed(() => {
  if (!distillRun.value?.total_count) return 0
  return Math.min(100, Math.round(((distillRun.value.completed_count + distillRun.value.degraded_count + distillRun.value.skipped_count + distillRun.value.failed_count + (distillRun.value.cancelled_count || 0)) / distillRun.value.total_count) * 100))
})
const distillOutputHint = computed(() => `下载目录/distill/${props.profile.nickname}-${props.profile.sec_user_id.slice(0, 8)}/`)
const allFilteredSelected = computed(() => isAllFilteredSelection(selection.value, filters, works.value.matched_count))
const scanProgress = computed(() => {
  if (!scan.value?.total_hint) return Math.min(92, scan.value ? scan.value.pages_scanned * 7 : 0)
  return Math.min(99, Math.round((scan.value.found_count / scan.value.total_hint) * 100))
})
const scanStepLabel = computed(() => !scan.value ? '准备中' : ({ pending: '排队中', scanning: '解析中', completed: '解析完成', partial: '部分完成', failed: '解析失败', cancelled: '已取消', expired: '已过期', idle: '等待中' }[scan.value.status]))
const terminalTitle = computed(() => scan.value?.status === 'cancelled' ? '解析已取消' : scan.value?.status === 'expired' ? '扫描结果已过期' : '解析失败')
const terminalMessage = computed(() => scan.value?.error?.message || (scan.value?.status === 'cancelled' ? `已保留 ${scan.value.found_count} 条临时结果，重新解析会生成新的扫描。` : '请重新尝试解析作品目录。'))
const partialScanMessage = computed(() => {
  const error = scan.value?.error
  if (!error) return scan.value?.warning || '部分页面读取失败，你仍可选择已经解析的作品。'
  return [
    error.message,
    error.status_code ? `上游状态码：HTTP ${error.status_code}` : '',
    error.action ? `建议：${error.action}` : '',
    scan.value?.warning || '',
  ].filter(Boolean).join(' ')
})
const selectionDescription = computed(() => selection.value.mode === 'explicit'
  ? `已选择 ${formatCount(selection.value.ids.size)} 个作品`
  : `已选择全部 ${formatCount(selection.value.matchedCount)} 个筛选结果，排除 ${selection.value.excluded.size} 个${selection.value.included.size ? `，另加 ${selection.value.included.size} 个` : ''}`)

const stepClass = (step: 'scan' | 'select' | 'confirm') => {
  if (step === 'scan') return scan.value && ['pending', 'scanning'].includes(scan.value.status) ? 'is-active' : scan.value ? 'is-complete' : ''
  if (step === 'select') return confirmMode.value || submitted.value ? 'is-complete' : scan.value?.status === 'completed' || scan.value?.status === 'partial' ? 'is-active' : ''
  return submitted.value ? 'is-complete' : confirmMode.value ? 'is-active' : ''
}

const persistScan = () => {
  if (!scan.value) return
  sessionStorage.setItem(USER_SCAN_STORAGE_KEY, JSON.stringify({ scan_id: scan.value.scan_id, scan_token: scan.value.scan_token, sec_user_id: props.profile.sec_user_id }))
}

const loadPage = async (cursor: string | null = cursorStack.value[pageIndex.value]) => {
  if (!service.value || !scan.value) return
  loadingWorks.value = true
  try { works.value = await service.value.listWorks(scan.value.scan_id, { ...filters }, cursor, pageSize) }
  catch (error: any) { ElMessage.error(error.message || '读取作品列表失败') }
  finally { loadingWorks.value = false }
}

const pollScan = async () => {
  if (!service.value || !scan.value || !['pending', 'scanning'].includes(scan.value.status)) return
  try {
    scan.value = await service.value.getScan(scan.value.scan_id)
    persistScan()
    await loadPage(null)
    if (['pending', 'scanning'].includes(scan.value.status)) pollTimer.value = setTimeout(pollScan, 500)
  } catch (error: any) {
    scan.value = { ...scan.value, status: 'failed', error: { message: error.message || '解析状态读取失败', retryable: true } }
  }
}

const startFreshScan = async () => {
  if (!service.value) return
  starting.value = true
  startupError.value = null
  clearSelection()
  try {
    scan.value = await service.value.startScan(props.userUrl)
    persistScan()
    await loadPage(null)
    if (['pending', 'scanning'].includes(scan.value.status)) pollTimer.value = setTimeout(pollScan, 400)
  } catch (error: any) {
    const detail = error.response?.data?.detail || {}
    startupError.value = {
      code: error.code,
      message: error.message || '启动解析失败',
      action: error.action || detail.action,
      stage_label: detail.stage_label,
      status_code: detail.status_code,
    }
    ElMessage.error(startupError.value.message)
  }
  finally { starting.value = false }
}

const restoreOrStart = async () => {
  try {
    service.value = await getUserPostScanService()
  } catch (error: any) {
    startupError.value = { code: error.code, message: error.message || '初始化作品解析服务失败', action: error.action }
    return
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem(USER_SCAN_STORAGE_KEY) || 'null')
    if (saved?.scan_id && saved?.sec_user_id === props.profile.sec_user_id) {
      scan.value = await service.value.getScan(saved.scan_id)
      await loadPage(null)
      if (['pending', 'scanning'].includes(scan.value.status)) pollTimer.value = setTimeout(pollScan, 400)
      return
    }
  } catch { sessionStorage.removeItem(USER_SCAN_STORAGE_KEY) }
  await startFreshScan()
}

const cancelScan = async () => {
  if (!service.value || !scan.value) return
  cancelling.value = true
  try { scan.value = await service.value.cancelScan(scan.value.scan_id); persistScan() }
  catch (error: any) { ElMessage.error(error.message || '取消解析失败') }
  finally { cancelling.value = false }
}

const applyFilters = async () => {
  filters.keyword = draftKeyword.value.trim()
  filters.date_from = dateRange.value?.[0]
  filters.date_to = dateRange.value?.[1]
  cursorStack.value = [null]
  pageIndex.value = 0
  await loadPage(null)
}

const resetFilters = async () => { draftKeyword.value = ''; dateRange.value = null; filters.keyword = ''; filters.date_from = undefined; filters.date_to = undefined; filters.kind = 'all'; filters.history = 'all'; await applyFilters() }
const isSelected = (work: UserWork) => isWorkSelected(selection.value, work)
const toggle = (work: UserWork) => { selection.value = toggleWork(selection.value, work) }
const clearSelection = () => { selection.value = emptySelection(); confirmMode.value = false }
const selectCurrentPage = () => { for (const work of works.value.items) if (!isSelected(work)) selection.value = toggleWork(selection.value, work) }
const selectAllFiltered = () => { selection.value = allFilteredSelection(filters, works.value.matched_count) }
const toggleAllFiltered = () => { if (allFilteredSelected.value) clearSelection(); else selectAllFiltered() }
const nextPage = async () => { if (!works.value.next_cursor) return; cursorStack.value = [...cursorStack.value.slice(0, pageIndex.value + 1), works.value.next_cursor]; pageIndex.value += 1; await loadPage(works.value.next_cursor) }
const previousPage = async () => { if (pageIndex.value === 0) return; pageIndex.value -= 1; await loadPage(cursorStack.value[pageIndex.value]) }

const submitDownload = async () => {
  if (!service.value || !scan.value || selectedTotal.value === 0) return
  submitting.value = true
  try {
    const response = await service.value.createDownload({ scan_id: scan.value.scan_id, selection: toScanSelection(selection.value), options: { download_path: downloadPath.value || undefined } })
    submitted.value = true
    submittedMessage.value = response.message
    emit('submitted', response.task_id)
    sessionStorage.removeItem(USER_SCAN_STORAGE_KEY)
  } catch (error: any) { ElMessage.error(error.message || '创建下载任务失败') }
  finally { submitting.value = false }
}

const submitDistill = async () => {
  if (!service.value || !scan.value || selectedTotal.value === 0) return
  distillSubmitting.value = true
  try {
    const diagnostics = await service.value.getAiDiagnostics()
    const missing: string[] = []
    if (!diagnostics.ffmpeg?.ok) missing.push('ffmpeg')
    if (!diagnostics.transcript?.api_key_configured || !diagnostics.transcript?.dashscope_api_key_configured) missing.push('口播模型配置')
    if (stagePolicy.value === 'full' && selectedVideoCount.value > 0 && !diagnostics.summary?.gateway?.api_key_configured && !diagnostics.summary?.api_key_configured && !diagnostics.summary?.fallback?.api_key_configured) missing.push('视频总结模型配置')
    if (missing.length) {
      ElMessage.error(`开始蒸馏前检查未通过：缺少${missing.join('、')}。请完成配置后重试。`)
      return
    }
    const result = await service.value.createDistill({ scanId: scan.value.scan_id, selection: toScanSelection(selection.value), forceReanalyze: forceReanalyze.value, downloadPath: downloadPath.value || undefined, stagePolicy: stagePolicy.value })
    distillRun.value = result.run
    localStorage.setItem('douyin-distill-run-id', result.run_id)
    distillConfirm.value = false
    submitted.value = false
    pollDistill()
  } catch (error: any) { ElMessage.error(error.message || '创建蒸馏运行失败') }
  finally { distillSubmitting.value = false }
}

const pollDistill = async () => {
  if (!service.value || !distillRun.value) return
  try {
    const detail = await service.value.getDistill(distillRun.value.run_id, distillRun.value.options?.stage_policy === 'preview')
    distillRun.value = detail.run
    distillItems.value = detail.items || []
    if (!['completed', 'partial', 'failed', 'cancelled', 'preview_ready'].includes(detail.run.status)) distillPollTimer.value = setTimeout(pollDistill, 1500)
  } catch (error: any) { ElMessage.error(error.message || '读取蒸馏进度失败') }
}

const cancelDistill = async () => {
  if (!service.value || !distillRun.value) return
  distillActionBusy.value = true
  try { distillRun.value = await service.value.cancelDistill(distillRun.value.run_id); await pollDistill() }
  catch (error: any) { ElMessage.error(error.message || '取消蒸馏失败') }
  finally { distillActionBusy.value = false }
}

const resumeDistill = async (awemeId?: string) => {
  if (!service.value || !distillRun.value) return
  distillActionBusy.value = true
  try { distillRun.value = await service.value.resumeDistill(distillRun.value.run_id, awemeId ? [awemeId] : []); pollDistill() }
  catch (error: any) { ElMessage.error(error.message || '继续蒸馏失败') }
  finally { distillActionBusy.value = false }
}

const deepAnalyzeSelected = async () => {
  if (!service.value || !distillRun.value) return
  const ids = Object.keys(previewSelected).filter(id => previewSelected[id])
  if (!ids.length) return
  distillActionBusy.value = true
  try { distillRun.value = await service.value.resumeDistill(distillRun.value.run_id, ids, 'full'); pollDistill() }
  catch (error: any) { ElMessage.error(error.message || '启动深看失败') }
  finally { distillActionBusy.value = false }
}

const artifactViewerUrl = computed(() => distillRun.value ? `/api/distill-runs/${distillRun.value.run_id}/view` : '')
const openDistillArtifact = () => { if (distillRun.value?.output_path) artifactViewerOpen.value = true }
const downloadDistillZip = () => { if (distillRun.value) window.open(`/api/distill-runs/${distillRun.value.run_id}/markdown?format=zip`, '_blank', 'noopener') }

const openHistory = (history: DistillRun) => {
  distillRun.value = history
  localStorage.setItem('douyin-distill-run-id', history.run_id)
  pollDistill()
}
const distillStatusLabel = (status: string) => ({ queued: '排队中', running: '处理中', preview_ready: '预筛完成', paused: '已暂停', synthesizing: '归纳中', writing: '写入文件', completed: '已完成', partial: '部分完成', cancelling: '取消中', cancelled: '已取消', failed: '失败' } as Record<string, string>)[status] || status
const distillItemStatusLabel = (item: Record<string, any>) => {
  if (item.item_status === 'failed') return `失败：${item.error?.reason || item.degrade_reason || '请重试'}`
  if (item.item_status === 'degraded') return `降级纳入：${item.degrade_reason || '部分分析成功'}`
  return ({ queued: '排队中', downloading: '下载中', transcribing: '提取口播中', summarizing: '理解画面中', transcribed: '已提取文案，待选择', ready: '分析完成', written: '已写入文件', skipped: '跳过', low_signal: '低信息量，未纳入归纳', summary_invalid: '画面理解无效，未纳入归纳', cancelled: '已取消' } as Record<string, string>)[item.item_status] || item.item_status || '等待中'
}

const formatCount = (value: number) => new Intl.NumberFormat('zh-CN').format(value)
const formatDate = (value: string) => new Date(value).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
const formatElapsed = (ms: number) => `${Math.floor(ms / 60_000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
const formatDuration = (seconds?: number | null) => seconds == null ? '视频' : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`

watch(dateRange, () => { if (!dateRange.value) { filters.date_from = undefined; filters.date_to = undefined } })
onMounted(async () => {
  try {
    await restoreOrStart()
    if (service.value) {
      const history = await service.value.listDistill(10, 0)
      distillHistory.value = history.items || []
      const savedRunId = localStorage.getItem('douyin-distill-run-id')
      const saved = savedRunId ? distillHistory.value.find(item => item.run_id === savedRunId) : null
      if (saved && ['queued', 'running', 'paused', 'synthesizing', 'writing', 'cancelling'].includes(saved.status)) openHistory(saved)
    }
  } finally { booting.value = false }
})
onBeforeUnmount(() => { if (pollTimer.value) clearTimeout(pollTimer.value); if (distillPollTimer.value) clearTimeout(distillPollTimer.value) })
</script>

<style scoped>
.scan-workbench { margin-top: var(--space-5); padding: 28px; border: 1px solid var(--color-line); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-lift); }
.scan-header { display: flex; justify-content: space-between; gap: 24px; }.scan-header h2 { margin: 8px 0; color: var(--color-ink); font: 700 30px/1 var(--font-display); }.scan-header p:last-child { margin: 0; color: var(--color-text-muted); font-size: 13px; }.scan-kicker { margin: 0; color: var(--color-cyan-deep); font: 700 11px var(--font-mono); letter-spacing: .12em; }.scan-close { min-height: 44px; }
.scan-steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin: 26px 0; padding: 0; overflow: hidden; border: 1px solid var(--color-line); border-radius: 12px; list-style: none; background: var(--color-line); }.scan-steps li { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; min-height: 70px; padding: 13px 15px; background: var(--color-surface-muted); }.scan-steps li span { grid-row: 1/3; color: var(--color-text-muted); font: 700 11px var(--font-mono); }.scan-steps strong { color: var(--color-ink-soft); font-size: 13px; }.scan-steps small { color: var(--color-text-muted); font-size: 11px; }.scan-steps .is-active { background: var(--color-ink); }.scan-steps .is-active strong { color: #fff; }.scan-steps .is-active span, .scan-steps .is-active small { color: var(--color-cyan); }.scan-steps .is-complete { background: #e6f4ef; }.scan-steps .is-complete span { color: var(--color-success); }
.scan-loading, .scan-progress-panel, .scan-state-panel, .confirm-panel, .submitted-panel { min-height: 280px; padding: 24px; border: 1px solid var(--color-line); border-radius: var(--radius-md); background: #faf8f3; }.scan-progress-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }.scan-progress-heading > div { display: flex; align-items: center; gap: 9px; }.scan-progress-heading > span { font: 700 14px var(--font-mono); }.live-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--color-cyan); box-shadow: 0 0 0 6px rgba(25,196,189,.14); animation: pulse 1.4s infinite; }.scan-metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 22px 0; }.scan-metrics > div { display: grid; padding: 15px; border-left: 3px solid var(--color-cyan); background: var(--color-surface); }.scan-metrics span { color: var(--color-text-muted); font-size: 11px; }.scan-metrics strong { margin: 4px 0; color: var(--color-ink); font: 700 26px var(--font-display); }.scan-metrics small { color: var(--color-text-muted); font-size: 10px; }.scan-time-hint { color: var(--color-text-muted); font-size: 12px; }.scan-preview-strip { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 16px 0; }.scan-preview-strip div { min-width: 0; display: flex; align-items: center; gap: 8px; padding: 10px; border: 1px solid var(--color-line); border-radius: 8px; background: var(--color-surface); }.scan-preview-strip div > span { width: 28px; height: 28px; display: grid; flex: 0 0 28px; place-items: center; color: var(--color-cyan-deep); border-radius: 7px; background: #dff4f0; }.scan-preview-strip p { overflow: hidden; margin: 0; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.cancel-scan { min-height: 44px; }
.scan-state-panel { display: flex; align-items: center; gap: 18px; }.scan-state-panel .state-icon { width: 48px; height: 48px; display: grid; flex: 0 0 48px; place-items: center; color: var(--color-danger); border-radius: 50%; background: #fae8e4; font-size: 24px; }.scan-state-panel h3 { margin: 0 0 6px; }.scan-state-panel p { margin: 0; color: var(--color-text-muted); }.scan-state-panel .state-action { display: block; margin-top: 6px; color: var(--color-text-muted); font-size: 12px; }.scan-state-panel .el-button { margin-left: auto; }.partial-alert { margin-bottom: 16px; }
.scan-toolbar { display: grid; grid-template-columns: minmax(180px, 1fr) minmax(250px, 1.1fr) 140px 140px auto; gap: 8px; padding: 14px; border: 1px solid var(--color-line); border-radius: 12px; background: var(--color-surface-muted); }.scan-toolbar :deep(.el-input__wrapper), .scan-toolbar :deep(.el-select__wrapper), .scan-toolbar :deep(.el-range-editor) { min-height: 44px; }.filter-submit { min-height: 44px; }
.selection-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 14px 0; }.result-counts, .selection-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }.result-counts span { padding: 7px 9px; color: var(--color-text-muted); border: 1px solid var(--color-line); border-radius: 999px; font-size: 11px; }.result-counts strong { color: var(--color-ink); }.selection-actions .el-button { min-height: 44px; margin-left: 0; }.selection-banner { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; padding: 11px 13px; color: var(--color-success); border: 1px solid rgba(22,133,111,.25); border-radius: 10px; background: #e9f5f0; font-size: 13px; }.selection-banner small { margin-left: auto; color: var(--color-text-muted); }
.work-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }.work-card { min-width: 0; overflow: hidden; border: 1px solid var(--color-line); border-radius: 12px; background: var(--color-surface); cursor: pointer; transition: transform var(--motion-fast), border-color var(--motion-fast), box-shadow var(--motion-fast); }.work-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-soft); }.work-card:focus-visible { outline: 3px solid rgba(25,196,189,.45); outline-offset: 3px; }.work-card.is-selected { border-color: var(--color-cyan-deep); box-shadow: 0 0 0 2px rgba(25,196,189,.2); }.work-cover { position: relative; aspect-ratio: 16/9; overflow: hidden; background: var(--color-ink); }.work-cover.kind-image { background: #26384a; }.work-cover img { width: 100%; height: 100%; object-fit: cover; }.cover-placeholder { width: 100%; height: 100%; display: grid; place-content: center; justify-items: center; gap: 6px; color: #73869a; }.cover-placeholder .el-icon { font-size: 27px; }.cover-placeholder span { font: 9px var(--font-mono); letter-spacing: .1em; }.work-check { position: absolute; top: 9px; left: 9px; width: 24px; height: 24px; display: grid; place-items: center; color: var(--color-ink); border: 2px solid rgba(255,255,255,.8); border-radius: 6px; background: rgba(16,28,44,.45); }.is-selected .work-check { border-color: var(--color-cyan); background: var(--color-cyan); }.work-kind { position: absolute; right: 8px; bottom: 8px; padding: 4px 6px; color: #fff; border-radius: 5px; background: rgba(16,28,44,.78); font: 10px var(--font-mono); }.work-copy { padding: 11px; }.work-copy h3 { min-height: 38px; display: -webkit-box; overflow: hidden; margin: 0 0 10px; color: var(--color-ink); font-size: 13px; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }.work-copy > div { display: flex; align-items: center; justify-content: space-between; gap: 6px; color: var(--color-text-muted); font-size: 10px; }.history-badge { color: var(--color-success); font-weight: 700; }.work-skeleton { padding: 10px; border: 1px solid var(--color-line); border-radius: 12px; }.skeleton-cover { width: 100%; height: 120px; margin-bottom: 10px; }
.cursor-pagination { display: flex; align-items: center; justify-content: center; gap: 14px; margin: 18px 0 88px; }.cursor-pagination .el-button { min-height: 44px; }.cursor-pagination span { color: var(--color-text-muted); font: 11px var(--font-mono); }.download-summary { position: sticky; z-index: 5; bottom: 12px; display: grid; grid-template-columns: 1fr 1.4fr auto; align-items: center; gap: 22px; padding: 15px 18px; color: #fff; border: 1px solid #314359; border-radius: 14px; background: rgba(16,28,44,.96); box-shadow: var(--shadow-lift); backdrop-filter: blur(12px); }.download-summary div { display: grid; }.download-summary span { color: #aab9c8; font-size: 10px; }.download-summary strong { overflow: hidden; margin: 3px 0; font-size: 16px; text-overflow: ellipsis; white-space: nowrap; }.download-summary small { color: #91a2b4; font-size: 10px; }.download-summary .el-button { min-height: 46px; }.summary-actions { display: flex; flex-wrap: wrap; gap: 8px; }.summary-actions .el-button { margin-left: 0; }
.distill-confirm-panel { border-color: rgba(22,133,111,.35); }.distill-path { margin-top: 18px; padding: 10px; border-radius: 8px; background: var(--color-surface-muted); font-family: var(--font-mono); font-size: 11px; word-break: break-all; }.distill-progress-panel { min-height: 280px; padding: 24px; border: 1px solid rgba(22,133,111,.35); border-radius: var(--radius-md); background: #f2faf6; }.distill-progress-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }.distill-progress-heading > div { display: flex; align-items: center; gap: 9px; }.distill-metrics { display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0; }.distill-metrics span { padding: 7px 10px; border-radius: 999px; background: #e0f2e9; color: var(--color-ink-soft); font-size: 12px; }.distill-progress-panel footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 22px; }.distill-progress-panel footer .el-button { min-height: 44px; }
.distill-artifact-hint { margin: 12px 0 0; color: var(--color-text-muted); font-size: 12px; }
.artifact-viewer { display: block; width: 100%; height: calc(100vh - 120px); border: 0; background: #fff; }
.confirm-panel > header { display: flex; align-items: start; gap: 20px; }.back-button { min-height: 44px; display: inline-flex; align-items: center; gap: 6px; padding: 0 12px; color: var(--color-ink); border: 1px solid var(--color-line); border-radius: 8px; background: transparent; cursor: pointer; }.confirm-panel header span { color: var(--color-cyan-deep); font: 700 10px var(--font-mono); }.confirm-panel h3 { margin: 5px 0; font: 700 24px var(--font-display); }.confirm-panel header p { margin: 0; color: var(--color-text-muted); font-size: 12px; }.confirm-grid { display: grid; grid-template-columns: .8fr 1.2fr; gap: 14px; margin: 22px 0 14px; }.confirm-grid section { padding: 18px; border: 1px solid var(--color-line); border-radius: 12px; background: var(--color-surface); }.confirm-grid h4 { margin: 0 0 14px; }.confirm-count { display: block; color: var(--color-coral); font: 700 48px var(--font-display); }.confirm-grid p { color: var(--color-text-muted); font-size: 12px; }.confirm-grid dl { margin: 14px 0 0; }.confirm-grid dl div { display: flex; justify-content: space-between; gap: 14px; padding: 8px 0; border-top: 1px solid var(--color-line); font-size: 11px; }.confirm-grid dt { color: var(--color-text-muted); }.confirm-grid dd { margin: 0; }.confirm-grid label { display: block; margin: 12px 0 7px; font-size: 12px; font-weight: 700; }.confirm-panel footer { display: flex; justify-content: flex-end; gap: 9px; margin-top: 18px; }.confirm-panel footer .el-button { min-height: 46px; }.submitted-panel { display: grid; place-content: center; justify-items: center; text-align: center; }.submitted-panel > span { color: var(--color-success); font-size: 54px; }.submitted-panel h3 { margin: 12px 0 7px; }.submitted-panel p { color: var(--color-text-muted); }
@keyframes pulse { 50% { box-shadow: 0 0 0 2px rgba(25,196,189,.08); } }
@media (max-width: 1100px) { .scan-toolbar { grid-template-columns: 1fr 1fr 1fr; }.filter-keyword { grid-column: span 2; }.work-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 800px) { .scan-workbench { padding: 20px; }.scan-steps { grid-template-columns: repeat(2, 1fr); }.scan-toolbar { grid-template-columns: 1fr 1fr; }.filter-keyword { grid-column: span 2; }.scan-toolbar :deep(.el-date-editor) { width: 100%; grid-column: span 2; }.selection-toolbar { align-items: flex-start; flex-direction: column; }.selection-actions .el-button { min-height: 44px; }.work-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.download-summary { grid-template-columns: 1fr auto; }.download-summary > div:nth-child(2) { display: none; }.confirm-grid { grid-template-columns: 1fr; } }
@media (max-width: 520px) { .scan-workbench { margin-inline: -1px; padding: 16px 14px; border-radius: 14px; }.scan-header { align-items: flex-start; }.scan-header h2 { font-size: 24px; }.scan-close { width: 44px; min-width: 44px; flex: 0 0 44px; padding: 0; font-size: 0; }.scan-close .el-icon { margin: 0; font-size: 16px; }.scan-steps li { min-height: 62px; padding: 11px; }.scan-metrics { grid-template-columns: 1fr; }.scan-preview-strip { grid-template-columns: 1fr 1fr; }.scan-state-panel { align-items: flex-start; flex-wrap: wrap; }.scan-state-panel .el-button { width: 100%; margin-left: 0; }.scan-toolbar { grid-template-columns: 1fr; }.filter-keyword, .scan-toolbar :deep(.el-date-editor) { grid-column: auto; }.result-counts { display: grid; grid-template-columns: repeat(3, 1fr); width: 100%; }.result-counts span { text-align: center; }.selection-actions { display: grid; grid-template-columns: 1fr 1fr; width: 100%; }.selection-actions .el-button { width: 100%; }.work-grid { grid-template-columns: 1fr; }.selection-banner { align-items: flex-start; flex-wrap: wrap; }.selection-banner small { width: 100%; margin-left: 24px; }.download-summary { bottom: 8px; grid-template-columns: 1fr; gap: 10px; }.download-summary .el-button { width: 100%; }.cursor-pagination { margin-bottom: 150px; }.confirm-panel, .scan-progress-panel, .scan-state-panel { padding: 16px; }.confirm-panel > header { flex-direction: column; }.confirm-panel footer { flex-direction: column-reverse; }.confirm-panel footer .el-button { width: 100%; margin-left: 0; } }
</style>
