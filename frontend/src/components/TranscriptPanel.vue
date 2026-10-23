<template>
  <el-dialog v-model="open" title="提取口播文案" width="680px" destroy-on-close @closed="handleClosed">
    <div v-if="loading" class="transcript-state" aria-live="polite">
      <el-icon class="is-loading"><Loading /></el-icon>
      <strong>正在读取口播提取状态</strong>
      <p>只读取本机已下载的视频，不会重新下载媒体。</p>
    </div>

    <div v-else-if="!started && !job" class="transcript-confirm">
      <el-alert type="warning" title="口播提取会调用语音模型" description="将从本机视频提取音频并发送到已配置的转写服务，可能产生费用或遇到限流；不会重新下载媒体。" show-icon :closable="false" />
      <el-button type="primary" @click="start()">开始提取</el-button>
    </div>

    <div v-else-if="loadError" class="transcript-error" aria-live="assertive">
      <el-alert type="error" title="无法启动口播提取" :description="loadError" show-icon :closable="false" />
      <p v-if="loadAction">建议：{{ loadAction }}</p>
      <el-button type="primary" :loading="loading" @click="start()">重试</el-button>
    </div>

    <template v-else-if="job && ['queued', 'running'].includes(job.status)">
      <div class="transcript-progress" aria-live="polite">
        <div class="progress-line"><strong>{{ stageLabel }}</strong><span>{{ job.progress }}%</span></div>
        <el-progress :percentage="job.progress" :show-text="false" :stroke-width="10" />
        <p>处理阶段：{{ stageLabel }}。可以关闭此窗口，后台任务不会中断；稍后再次点击“提取口播”即可查看进度或结果。</p>
      </div>
      <div class="transcript-actions"><el-button type="warning" @click="cancel">取消提取</el-button></div>
    </template>

    <div v-else-if="job?.status === 'completed'" class="transcript-result">
      <el-alert type="success" title="口播文案已生成" description="原始转写未经人工核实；整理稿经过 AI 断句和编辑，不能直接视为视频原话。" show-icon :closable="false" />
      <el-tabs v-model="activeTab" class="transcript-tabs">
        <el-tab-pane label="整理文案" name="polished"><pre>{{ job.polished_text || '暂无整理文案' }}</pre></el-tab-pane>
        <el-tab-pane label="原始转写" name="original"><pre>{{ job.original_transcript || '暂无原始转写' }}</pre></el-tab-pane>
      </el-tabs>
      <div class="transcript-actions">
        <el-button type="primary" @click="readContent">阅读、收藏与视频核对</el-button>
        <el-button @mousedown.prevent @click="collectSelection">收藏选中文字</el-button>
        <el-button @click="copyText"><el-icon><CopyDocument /></el-icon>复制当前内容</el-button>
        <el-button @click="exportFile('txt')"><el-icon><Download /></el-icon>导出 TXT</el-button>
        <el-button @click="exportFile('md')"><el-icon><Document /></el-icon>导出 Markdown</el-button>
        <el-button @click="exportFile('json')"><el-icon><Document /></el-icon>导出 JSON</el-button>
        <el-button type="primary" plain :loading="loading" @click="restart"><el-icon><RefreshRight /></el-icon>重新提取</el-button>
      </div>
    </div>

    <div v-else-if="job?.status === 'failed'" class="transcript-error" aria-live="assertive">
      <el-alert type="error" title="口播提取失败" :description="job.error?.reason || '处理失败'" show-icon :closable="false" />
      <p v-if="job.error?.action">建议：{{ job.error.action }}</p>
      <el-button type="primary" :loading="loading" @click="restart">重试提取</el-button>
    </div>

    <div v-else-if="job?.status === 'cancelled'" class="transcript-error">
      <el-alert type="warning" title="口播提取已取消" description="未生成新的文案结果。" show-icon :closable="false" />
      <el-button type="primary" :loading="loading" @click="restart">重新提取</el-button>
    </div>
    <div v-else-if="job?.status === 'paused'" class="transcript-error"><el-alert type="warning" title="口播提取已暂停" :description="job.error?.reason || '已达到本批次模型请求上限，已完成的转写会保留。'" show-icon :closable="false" /><p>{{ job.error?.action || '请在批次结果中调整调用上限后继续。' }}</p><el-button @click="showBatchResults">查看批次结果</el-button></div>

    <div v-else class="transcript-state"><p>尚未开始提取。</p><el-button type="primary" :loading="loading" @click="start()">开始提取</el-button></div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Document, Download, Loading, RefreshRight } from '@element-plus/icons-vue'
import { douyinApi } from '@/api'
import { openContentReader, collectContent } from '@/api/content'

interface TranscriptJob {
  job_id: string
  task_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'paused'
  stage: string
  progress: number
  original_transcript: string
  polished_text: string
  error?: { reason?: string; action?: string } | null
}

const props = defineProps<{ modelValue: boolean; taskId: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const open = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const job = ref<TranscriptJob | null>(null)
const loading = ref(false)
const loadError = ref('')
const loadAction = ref('')
const started = ref(false)
const activeTab = ref('polished')
let pollTimer: ReturnType<typeof setTimeout> | null = null

const stageLabel = computed(() => ({ queued: '排队中', extracting_audio: '提取音频', transcribing: '语音转写', polishing: '整理口播文案', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[job.value?.stage || 'queued'] || '处理中')

const unwrap = (response: any) => {
  if (!response?.success) throw new Error(response?.message || '请求失败')
  return response.data
}

const stopPolling = () => { if (pollTimer) clearTimeout(pollTimer); pollTimer = null }
const handleClosed = () => {
  stopPolling()
  if (job.value && ['queued', 'running'].includes(job.value.status)) ElMessage.info('口播提取仍在后台运行，稍后再次打开即可查看')
}
const loadExisting = async () => {
  loading.value = true
  loadError.value = ''
  loadAction.value = ''
  stopPolling()
  try {
    const existing = unwrap(await douyinApi.getTranscript(props.taskId))
    if (existing) {
      job.value = existing
      started.value = true
      if (['queued', 'running'].includes(existing.status)) pollTimer = setTimeout(poll, 500)
    }
  } catch (error: any) {
    loadError.value = error.message || '读取口播提取状态失败'
    loadAction.value = error.action || ''
  } finally { loading.value = false }
}
const poll = async () => {
  if (!open.value || !job.value || !['queued', 'running'].includes(job.value.status)) return
  try {
    job.value = unwrap(await douyinApi.getTranscript(props.taskId))
    if (job.value && ['queued', 'running'].includes(job.value.status)) pollTimer = setTimeout(poll, 1200)
  } catch (error: any) { ElMessage.error(error.message || '读取口播提取状态失败'); stopPolling() }
}

const start = async (force = false) => {
  loading.value = true
  started.value = true
  loadError.value = ''
  loadAction.value = ''
  stopPolling()
  try {
    job.value = unwrap(await douyinApi.createTranscript(props.taskId, force))
    if (job.value && ['queued', 'running'].includes(job.value.status)) pollTimer = setTimeout(poll, 500)
  } catch (error: any) { loadError.value = error.message || '启动口播提取失败'; loadAction.value = error.action || ''; ElMessage.error(loadError.value) }
  finally { loading.value = false }
}

const restart = () => start(true)
const cancel = async () => {
  if (!job.value?.job_id) return
  loading.value = true
  try { job.value = unwrap(await douyinApi.cancelTranscript(job.value.job_id)); stopPolling() }
  catch (error: any) { ElMessage.error(error.message || '取消口播提取失败') }
  finally { loading.value = false }
}
const copyText = async () => {
  const text = activeTab.value === 'original' ? job.value?.original_transcript : job.value?.polished_text
  if (!text) return
  try { await navigator.clipboard.writeText(text); ElMessage.success('文案已复制') }
  catch { ElMessage.error('复制失败') }
}
const exportFile = (format: 'txt' | 'md' | 'json') => {
  if (job.value?.job_id) window.open(`/api/transcripts/${encodeURIComponent(job.value.job_id)}/export?format=${format}`, '_blank', 'noopener')
}
const readContent = () => { open.value = false; openContentReader({ task_id: props.taskId }) }
const collectSelection = () => {
  const text = window.getSelection()?.toString().trim()
  if (!text) { ElMessage.info('请先选择口播中的文字；手机可打开“阅读、收藏与视频核对”逐段摘录'); return }
  collectContent({ task_id: props.taskId, source_type: activeTab.value, text })
}
const showBatchResults = () => { open.value = false; document.getElementById('batch-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }

watch(() => props.modelValue, value => { if (value) { job.value = null; started.value = false; void loadExisting() } else stopPolling() })
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.transcript-state, .transcript-confirm { min-height: 180px; display: grid; place-content: center; justify-items: center; gap: 10px; color: var(--color-text-muted); text-align: center; }.transcript-state .el-icon { font-size: 28px; color: var(--color-accent); }.transcript-state strong { color: var(--color-ink); }.transcript-state p, .transcript-error p { margin: 0; font-size: 13px; }.transcript-confirm { gap: 16px; }.transcript-confirm :deep(.el-alert) { max-width: 560px; text-align: left; }.transcript-progress { padding: 18px; border: 1px solid var(--color-line); border-radius: 0; background: var(--color-surface); }.progress-line { display: flex; justify-content: space-between; margin-bottom: 12px; }.progress-line span { font: 700 13px var(--font-mono); }.transcript-progress p { margin: 12px 0 0; color: var(--color-text-muted); font-size: 12px; }.transcript-tabs { margin-top: 16px; }.transcript-tabs pre { min-height: 220px; max-height: 420px; overflow: auto; margin: 0; padding: 16px; white-space: pre-wrap; color: var(--color-ink); border: 1px solid var(--color-line); border-radius: 0; background: var(--color-surface); font: 14px/1.8 var(--font-body); }.transcript-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; margin-top: 16px; }.transcript-actions .el-button { min-height: 40px; }.transcript-error { display: grid; gap: 14px; }.transcript-error .el-button { justify-self: end; }
@media (max-width: 560px) { .transcript-actions { display: grid; grid-template-columns: 1fr 1fr; }.transcript-actions .el-button { width: 100%; margin: 0; }.transcript-error .el-button { justify-self: stretch; } }
</style>
