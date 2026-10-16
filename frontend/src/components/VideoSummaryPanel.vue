<template>
  <el-dialog v-model="open" title="视频总结" width="760px" destroy-on-close @closed="handleClosed">
    <div v-if="!started && !loading" class="summary-confirm">
      <el-alert
        type="warning"
        title="将发送完整视频进行分析"
        description="视频会通过 HTTPS 发送到当前配置的视频总结模型服务；首选网关失败时会依次尝试其他通道，不会抽帧，也不会重新下载媒体。"
        show-icon
        :closable="false"
      />
      <el-button type="primary" @click="start()">开始视频总结</el-button>
    </div>

    <div v-else-if="loading" class="summary-state" aria-live="polite">
      <el-icon class="is-loading"><Loading /></el-icon>
      <strong>正在准备视频总结</strong>
      <p>本机正在读取已下载的视频，不会创建新的下载任务。</p>
    </div>

    <div v-else-if="loadError" class="summary-error" aria-live="assertive">
      <el-alert type="error" title="无法启动视频总结" :description="loadError" show-icon :closable="false" />
      <p v-if="loadAction">建议：{{ loadAction }}</p>
      <el-button type="primary" @click="start(true)">重试</el-button>
    </div>

    <template v-else-if="job && ['queued', 'running'].includes(job.status)">
      <div class="summary-progress" aria-live="polite">
        <div class="progress-line"><strong>{{ stageLabel }}</strong><span>{{ job.progress }}%</span></div>
        <el-progress :percentage="job.progress" :show-text="false" :stroke-width="10" />
        <p>完整视频正在发送给 Gemini 进行画面和音频分析。可以关闭此窗口，后台任务不会中断；稍后再次点击“视频总结”即可查看进度或结果。</p>
      </div>
      <div class="summary-actions"><el-button type="warning" @click="cancel">取消总结</el-button></div>
    </template>

    <div v-else-if="job?.status === 'completed' && job.summary" class="summary-result">
      <el-alert type="success" title="视频总结已生成" :description="processingDescription" show-icon :closable="false" />
      <el-tabs v-model="activeTab" class="summary-tabs">
        <el-tab-pane label="概览" name="overview">
          <h3>{{ job.summary.title || '视频总结' }}</h3>
          <p class="summary-one-sentence">{{ job.summary.one_sentence }}</p>
          <p class="summary-overview">{{ job.summary.overview }}</p>
        </el-tab-pane>
        <el-tab-pane label="关键要点" name="points">
          <ul class="summary-list"><li v-for="(item, index) in job.summary.key_points" :key="`point-${index}`">{{ item }}</li></ul>
          <p v-if="!job.summary.key_points.length" class="empty-summary">暂无关键要点</p>
        </el-tab-pane>
        <el-tab-pane label="时间线" name="timeline">
          <ul class="summary-list timeline-list"><li v-for="(item, index) in job.summary.timeline" :key="`time-${index}`"><strong>{{ timelineTime(item) }}</strong><span>{{ timelineContent(item) }}</span></li></ul>
          <p v-if="!job.summary.timeline.length" class="empty-summary">暂无时间线</p>
        </el-tab-pane>
        <el-tab-pane label="画面与口播" name="details">
          <h4>画面内容</h4><ul class="summary-list"><li v-for="(item, index) in job.summary.visual_content" :key="`visual-${index}`">{{ item }}</li></ul>
          <h4>口播内容</h4><ul class="summary-list"><li v-for="(item, index) in job.summary.spoken_content" :key="`spoken-${index}`">{{ item }}</li></ul>
        </el-tab-pane>
        <el-tab-pane label="叙事与人物" name="story">
          <template v-if="hasNarrative || (job.summary.people_and_objects?.length || 0) || (job.summary.emotional_arc?.length || 0)">
            <div v-if="hasNarrative">
              <h4>叙事结构</h4>
              <dl class="summary-definition-list">
                <template v-for="(label, key) in narrativeLabels" :key="key">
                  <dt>{{ label }}</dt><dd>{{ job.summary.narrative_structure?.[key as keyof NonNullable<SummaryData['narrative_structure']>] || '未明确' }}</dd>
                </template>
              </dl>
            </div>
            <h4>人物与物品</h4>
            <ul class="summary-list"><li v-for="(item, index) in job.summary.people_and_objects || []" :key="`people-${index}`">{{ personText(item) }}</li></ul>
            <h4>情绪变化</h4>
            <ul class="summary-list"><li v-for="(item, index) in job.summary.emotional_arc || []" :key="`emotion-${index}`">{{ item }}</li></ul>
          </template>
          <p v-else class="empty-summary">暂无叙事与人物分析</p>
        </el-tab-pane>
        <el-tab-pane label="创作分析" name="creation">
          <h4>表达与剪辑</h4>
          <ul class="summary-list"><li v-for="(item, index) in job.summary.editing_and_expression || []" :key="`editing-${index}`">{{ item }}</li></ul>
          <h4>受众与意图</h4>
          <dl class="summary-definition-list"><dt>可能受众</dt><dd>{{ job.summary.audience_and_intent?.likely_audience || '未明确' }}</dd><dt>传播意图</dt><dd>{{ job.summary.audience_and_intent?.communication_intent || '未明确' }}</dd><dt>整体语气</dt><dd>{{ job.summary.audience_and_intent?.tone || '未明确' }}</dd></dl>
          <h4>可复用要素</h4>
          <ul class="summary-list"><li v-for="(item, index) in job.summary.reusable_elements || []" :key="`reuse-${index}`">{{ item }}</li></ul>
          <p v-if="!(job.summary.editing_and_expression?.length || 0) && !(job.summary.reusable_elements?.length || 0)" class="empty-summary">暂无创作分析</p>
        </el-tab-pane>
      </el-tabs>
      <div class="summary-actions">
        <el-button @click="copyText"><el-icon><CopyDocument /></el-icon>复制总结</el-button>
        <el-button @click="exportFile('md')"><el-icon><Document /></el-icon>导出 Markdown</el-button>
        <el-button @click="exportFile('json')"><el-icon><Download /></el-icon>导出 JSON</el-button>
        <el-button type="primary" plain @click="restart"><el-icon><RefreshRight /></el-icon>重新总结</el-button>
      </div>
    </div>

    <div v-else-if="job?.status === 'failed'" class="summary-error" aria-live="assertive">
      <el-alert type="error" title="视频总结失败" :description="job.error?.reason || '处理失败'" show-icon :closable="false" />
      <p v-if="job.provider_attempts?.length">已尝试：{{ job.provider_attempts.map(item => `${item.provider === 'gateway' ? '首选网关' : item.provider === 'primary' ? '原主接口' : '保底接口'}（${item.model}）：${item.status ? `HTTP ${item.status}` : '连接失败'}`).join(' → ') }}</p>
      <p v-if="job.error?.action">建议：{{ job.error.action }}</p>
      <el-button type="primary" @click="restart">重试总结</el-button>
    </div>

    <div v-else-if="job?.status === 'cancelled'" class="summary-error">
      <el-alert type="warning" title="视频总结已取消" description="未生成新的总结结果。" show-icon :closable="false" />
      <el-button type="primary" @click="restart">重新总结</el-button>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Document, Download, Loading, RefreshRight } from '@element-plus/icons-vue'
import { douyinApi } from '@/api'

interface SummaryData {
  title: string
  one_sentence: string
  overview: string
  key_points: string[]
  timeline: Array<{ time?: string; content?: string; visual?: string; action?: string; audio?: string; significance?: string } | string>
  visual_content: string[]
  spoken_content: string[]
  people_and_objects?: Array<{ name?: string; role_or_relation?: string; appearance_or_action?: string } | string>
  narrative_structure?: Partial<Record<'hook' | 'setup' | 'development' | 'turning_point' | 'climax' | 'ending', string>>
  emotional_arc?: string[]
  editing_and_expression?: string[]
  audience_and_intent?: { likely_audience?: string; communication_intent?: string; tone?: string }
  reusable_elements?: string[]
}

interface SummaryJob {
  job_id: string
  task_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  stage: string
  progress: number
  input_bytes: number
  summary?: SummaryData | null
  error?: { reason?: string; action?: string } | null
  prepared_bytes?: number | null
  processing_mode?: 'direct' | 'transcoded' | 'chunked' | null
  segment_count?: number | null
  model?: string | null
  provider_attempts?: Array<{ provider: string; model: string; status?: number | null }>
}

const props = defineProps<{ modelValue: boolean; taskId: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const open = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const job = ref<SummaryJob | null>(null)
const loading = ref(false)
const loadError = ref('')
const loadAction = ref('')
const started = ref(false)
const activeTab = ref('overview')
let pollTimer: ReturnType<typeof setTimeout> | null = null

const stageLabel = computed(() => ({ queued: '排队中', checking_video: '检查视频', compressing_video: '压缩完整视频', ready_to_upload: '准备上传', analyzing_video: 'Gemini 分析视频', retrying_as_segments: '切换分段分析', compressing_segments: '准备分段视频', analyzing_segments: '分析视频片段', combining_segments: '合并片段结果', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[job.value?.stage || 'queued'] || '处理中')
const formatBytes = (value?: number | null) => {
  if (!value) return '未知大小'
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}
const processingDescription = computed(() => {
  const current = job.value
  if (!current?.processing_mode) return '总结由完整视频的画面与音频共同生成。'
  const mode = current.processing_mode === 'direct' ? '完整视频直传' : current.processing_mode === 'transcoded' ? '完整视频本地压缩后直传' : `连续分段分析（${current.segment_count || 0} 段）`
  return `${mode} · 模型 ${current.model || '当前配置'} · 原始 ${formatBytes(current.input_bytes)}${current.prepared_bytes ? ` · 发送 ${formatBytes(current.prepared_bytes)}` : ''}`
})
const timelineTime = (item: SummaryData['timeline'][number]) => typeof item === 'string' ? '-' : item.time || '-'
const timelineContent = (item: SummaryData['timeline'][number]) => {
  if (typeof item === 'string') return item
  return [item.content, item.visual && `画面：${item.visual}`, item.action && `动作：${item.action}`, item.audio && `声音：${item.audio}`, item.significance && `作用：${item.significance}`].filter(Boolean).join('；')
}
const personText = (item: NonNullable<SummaryData['people_and_objects']>[number]) => typeof item === 'string' ? item : [item.name, item.role_or_relation, item.appearance_or_action].filter(Boolean).join('：')
const narrativeLabels: Record<string, string> = { hook: '开场钩子', setup: '背景铺垫', development: '发展', turning_point: '转折/冲突', climax: '高潮', ending: '结尾' }
const hasNarrative = computed(() => Object.values(job.value?.summary?.narrative_structure || {}).some(Boolean))
const unwrap = (response: any) => {
  if (!response?.success) throw new Error(response?.message || '请求失败')
  return response.data
}
const stopPolling = () => { if (pollTimer) clearTimeout(pollTimer); pollTimer = null }
const handleClosed = () => {
  stopPolling()
  if (job.value && ['queued', 'running'].includes(job.value.status)) ElMessage.info('视频总结仍在后台运行，稍后再次打开即可查看')
}
const loadExisting = async () => {
  loading.value = true
  loadError.value = ''
  loadAction.value = ''
  stopPolling()
  try {
    const existing = unwrap(await douyinApi.getVideoSummary(props.taskId))
    if (existing) {
      job.value = existing
      started.value = true
      if (['queued', 'running'].includes(existing.status)) pollTimer = setTimeout(poll, 500)
    }
  } catch (error: any) {
    loadError.value = error.message || '读取视频总结状态失败'
    loadAction.value = error.action || ''
  } finally { loading.value = false }
}
const poll = async () => {
  if (!open.value || !job.value || !['queued', 'running'].includes(job.value.status)) return
  try {
    job.value = unwrap(await douyinApi.getVideoSummary(props.taskId))
    if (job.value && ['queued', 'running'].includes(job.value.status)) pollTimer = setTimeout(poll, 1500)
  } catch (error: any) { ElMessage.error(error.message || '读取视频总结状态失败'); stopPolling() }
}
const start = async (force = false) => {
  loading.value = true; started.value = true; loadError.value = ''; stopPolling()
  try {
    job.value = unwrap(await douyinApi.createVideoSummary(props.taskId, force))
    if (job.value && ['queued', 'running'].includes(job.value.status)) pollTimer = setTimeout(poll, 600)
  } catch (error: any) { loadError.value = error.message || '启动视频总结失败'; loadAction.value = error.action || ''; ElMessage.error(loadError.value) }
  finally { loading.value = false }
}
const restart = () => start(true)
const cancel = async () => {
  if (!job.value?.job_id) return
  loading.value = true
  try { job.value = unwrap(await douyinApi.cancelVideoSummary(job.value.job_id)); stopPolling() }
  catch (error: any) { ElMessage.error(error.message || '取消视频总结失败') }
  finally { loading.value = false }
}
const copyText = async () => {
  const summary = job.value?.summary
  if (!summary) return
  const text = [summary.title, summary.one_sentence, summary.overview, ...summary.key_points].filter(Boolean).join('\n\n')
  try { await navigator.clipboard.writeText(text); ElMessage.success('总结已复制') }
  catch { ElMessage.error('复制失败') }
}
const exportFile = (format: 'md' | 'json') => {
  if (job.value?.job_id) window.open(`/api/summaries/${encodeURIComponent(job.value.job_id)}/export?format=${format}`, '_blank', 'noopener')
}
watch(() => props.modelValue, value => { if (value) { job.value = null; started.value = false; loadError.value = ''; loadAction.value = ''; activeTab.value = 'overview'; void loadExisting() } else stopPolling() })
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.summary-confirm,.summary-state { min-height: 180px; display: grid; place-content: center; justify-items: center; gap: 14px; text-align: center; }.summary-state { color: var(--color-text-muted); }.summary-state .el-icon { font-size: 28px; color: var(--color-accent); }.summary-state strong,.summary-result h3 { color: var(--color-ink); }.summary-state p,.summary-error p { margin: 0; color: var(--color-text-muted); font-size: 13px; }.summary-progress { padding: 18px; border: 1px solid var(--color-line); border-radius: 0; background: var(--color-surface); }.progress-line { display: flex; justify-content: space-between; margin-bottom: 12px; }.progress-line span { font: 700 13px var(--font-mono); }.summary-progress p { margin: 12px 0 0; color: var(--color-text-muted); font-size: 12px; }.summary-tabs { margin-top: 16px; }.summary-one-sentence { margin: 8px 0 14px; color: var(--color-ink-soft); font-weight: 700; }.summary-overview { white-space: pre-wrap; line-height: 1.8; }.summary-list { margin: 0; padding-left: 20px; color: var(--color-ink-soft); line-height: 1.8; }.summary-list + .summary-list { margin-top: 14px; } .timeline-list li { margin-bottom: 10px; }.timeline-list li span { display: block; margin-top: 2px; }.summary-definition-list { display: grid; grid-template-columns: 110px 1fr; gap: 8px 14px; margin: 0; line-height: 1.7; }.summary-definition-list dt { color: var(--color-text-muted); font-size: 12px; }.summary-definition-list dd { margin: 0; color: var(--color-ink-soft); white-space: pre-wrap; }.summary-result h4 { margin: 16px 0 6px; color: var(--color-ink); }.empty-summary { color: var(--color-text-muted); }.summary-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; margin-top: 16px; }.summary-actions .el-button { min-height: 40px; }.summary-error { display: grid; gap: 14px; }.summary-error .el-button { justify-self: end; }
@media (max-width: 560px) { .summary-actions { display: grid; grid-template-columns: 1fr 1fr; }.summary-actions .el-button,.summary-error .el-button { width: 100%; margin: 0; } }
</style>
