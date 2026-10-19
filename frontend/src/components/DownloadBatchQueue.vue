<template>
  <section v-if="!unavailable && (batches.length || loadError)" class="download-batch-queue" aria-labelledby="download-batch-title">
    <header class="queue-header">
      <div>
        <p class="queue-kicker">BACKGROUND / LOCAL</p>
        <h2 id="download-batch-title">后台下载队列</h2>
        <p class="queue-help">本地已有视频在冷却期间也会提前跳过；下载串行，每条结束后默认等 30 秒。网页关闭不影响后台；电脑和本地服务需保持运行。</p>
      </div>
      <el-button :loading="loading" :disabled="!!actionBatch" @click="loadBatches">刷新队列</el-button>
    </header>

    <p v-if="loadError" class="queue-error" role="alert">{{ loadError }}<span>当前进度可能已过期，请刷新后再操作。</span></p>
    <div class="queue-list">
      <article v-for="batch in activeBatches" :key="batch.batch_id" class="queue-card" :class="`is-${batch.status}`">
        <div class="queue-card-heading"><h3>{{ batch.author || '主页批次' }}</h3><span class="queue-status">{{ downloadBatchStatusLabel(batch.status) }}</span></div>
        <div class="queue-metrics"><strong>已完成 {{ batch.completed }} / {{ batch.total }}</strong><span>{{ downloadBatchSkipLabel(batch) }}</span><span>失败 {{ batch.failed }}</span><span>待处理 {{ batch.pending }}</span><span v-if="batch.cancelled">已停止 {{ batch.cancelled }}</span></div>
        <progress :value="downloadBatchProgressValue(batch)" :max="Math.max(1, batch.total)" :aria-label="`${batch.author || '主页批次'}处理进度`"></progress>
        <div class="queue-card-footer">
          <div><p class="queue-countdown">{{ downloadBatchNextLabel(batch, now, fetchedAt) }}</p><p v-if="batch.current_aweme_id" class="queue-current">当前视频 {{ batch.current_aweme_id }}</p></div>
          <div class="queue-controls">
            <el-button v-if="batch.status === 'paused'" :disabled="loading || !!loadError || !!actionBatch" :loading="actionBatch === batch.batch_id && actionName === 'resume'" @click="controlBatch(batch, 'resume')">继续</el-button>
            <el-button v-else :disabled="loading || !!loadError || !!actionBatch" :loading="actionBatch === batch.batch_id && actionName === 'pause'" @click="controlBatch(batch, 'pause')">暂停</el-button>
            <el-button type="danger" plain :disabled="loading || !!loadError || !!actionBatch" :loading="actionBatch === batch.batch_id && actionName === 'stop'" @click="controlBatch(batch, 'stop')">停止后续</el-button>
          </div>
        </div>
        <p v-if="batch.error?.reason || batch.error?.action" class="queue-stage-error" role="status"><span v-if="batch.error?.reason">{{ batch.error.reason }}</span><span v-if="batch.error?.action">建议：{{ batch.error.action }}</span></p>
      </article>
    </div>

    <details v-if="finishedBatches.length" class="queue-history">
      <summary>最近已结束的批次（{{ finishedBatches.length }}）</summary>
      <p v-for="batch in finishedBatches" :key="batch.batch_id"><strong>{{ batch.author || '主页批次' }}</strong><span>{{ downloadBatchStatusLabel(batch.status) }} · 完成 {{ batch.completed }}/{{ batch.total }} · {{ downloadBatchSkipLabel(batch) }} · 失败 {{ batch.failed }}<template v-if="batch.cancelled"> · 已停止 {{ batch.cancelled }}</template></span></p>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import api from '@/api'
import { downloadBatchNextLabel, downloadBatchProgressValue, downloadBatchSkipLabel, downloadBatchStatusLabel, type DownloadBatch } from '@/utils/downloadBatchQueue'

const batches = ref<DownloadBatch[]>([])
const loading = ref(false)
const unavailable = ref(false)
const loadError = ref('')
const actionBatch = ref('')
const actionName = ref('')
const now = ref(Date.now())
const fetchedAt = ref(now.value)
const activeBatches = computed(() => batches.value.filter(batch => !['completed', 'cancelled'].includes(batch.status)))
const finishedBatches = computed(() => batches.value.filter(batch => ['completed', 'cancelled'].includes(batch.status)).slice(0, 3))
let pollTimer: ReturnType<typeof setInterval> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let disposed = false

async function loadBatches() {
  if (loading.value || disposed) return
  loading.value = true
  try {
    const response = await api.get<unknown, { success: boolean; data?: { batches?: DownloadBatch[] }; message?: string }>('/download-batches', { timeout: 10000 })
    if (!response.success || !Array.isArray(response.data?.batches)) throw new Error(response.message || '队列返回格式异常')
    if (disposed) return
    batches.value = response.data.batches
    fetchedAt.value = Date.now()
    now.value = fetchedAt.value
    unavailable.value = false
    loadError.value = ''
  } catch (error: any) {
    if (disposed) return
    if (error.response?.status === 404) {
      unavailable.value = true
      batches.value = []
      loadError.value = ''
    } else {
      unavailable.value = false
      loadError.value = `无法刷新后台队列：${error.message || '请检查本地服务'}`
    }
  } finally {
    loading.value = false
  }
}

async function controlBatch(batch: DownloadBatch, action: 'pause' | 'resume' | 'stop') {
  if (actionBatch.value || loading.value || loadError.value) return
  if (action === 'stop') {
    try {
      await ElMessageBox.confirm('停止后将不再下载本批次的后续视频；已下载文件会保留，当前下载不会取消。', '停止后台队列', { confirmButtonText: '停止后续下载', cancelButtonText: '返回', type: 'warning' })
    } catch { return }
  }
  if (actionBatch.value || loading.value || disposed) return
  actionBatch.value = batch.batch_id
  actionName.value = action
  try {
    const response = await api.post<unknown, { success: boolean; message?: string }>(`/download-batches/${encodeURIComponent(batch.batch_id)}/${action}`, {}, { timeout: 15000 })
    if (!response.success) throw new Error(response.message || '操作未成功')
    await loadBatches()
  } catch (error: any) {
    loadError.value = `队列操作未确认：${error.message || '请检查本地服务'}。请刷新确认实际状态。`
  } finally {
    actionBatch.value = ''
    actionName.value = ''
  }
}

onMounted(() => {
  void loadBatches()
  pollTimer = setInterval(() => { if (!actionBatch.value) void loadBatches() }, 10000)
  clockTimer = setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  disposed = true
  clearInterval(pollTimer)
  clearInterval(clockTimer)
})
</script>

<style scoped>
.download-batch-queue { margin-top: var(--space-5); padding: 24px 28px; border: 1px solid var(--color-line); background: var(--color-surface); }
.queue-header, .queue-card-heading, .queue-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.queue-kicker { margin: 0 0 7px; color: var(--color-text-muted); font: 700 10px var(--font-mono); letter-spacing: .12em; }
.queue-header h2 { margin: 0; color: var(--color-ink); font: 700 23px var(--font-display); }
.queue-help { margin: 8px 0 0; color: var(--color-text-muted); font-size: 12px; line-height: 1.6; }
.queue-list { display: grid; gap: 12px; margin-top: 18px; }
.queue-card { padding: 18px; border: 1px solid var(--color-line); background: var(--color-surface-muted); }
.queue-card-heading h3 { overflow-wrap: anywhere; margin: 0; font-size: 16px; }
.queue-status { display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0; font-size: 12px; font-weight: 700; }
.queue-status::before { width: 7px; height: 7px; border-radius: 50%; background: var(--el-color-primary); content: ''; }
.is-paused .queue-status::before { background: var(--el-color-warning); }
.queue-metrics { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 14px 0 9px; color: var(--color-text-muted); font-size: 12px; }
.queue-metrics strong { color: var(--color-ink); font-weight: 600; }
progress { display: block; width: 100%; height: 5px; overflow: hidden; border: 0; background: var(--color-line); accent-color: var(--el-color-primary); }
progress::-webkit-progress-bar { background: var(--color-line); }
progress::-webkit-progress-value { background: var(--el-color-primary); }
.queue-card-footer { margin-top: 13px; }
.queue-countdown { margin: 0; color: var(--color-ink); font-size: 12px; font-variant-numeric: tabular-nums; }
.queue-current { margin: 5px 0 0; color: var(--color-text-muted); font: 10px var(--font-mono); overflow-wrap: anywhere; }
.queue-controls { display: flex; flex-shrink: 0; gap: 8px; }
.queue-controls .el-button { margin: 0; }
.queue-error, .queue-stage-error { display: grid; gap: 4px; margin: 14px 0 0; color: var(--el-color-danger); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.queue-error { padding: 12px; border-left: 2px solid currentColor; background: var(--el-color-danger-light-9); }
.queue-history { margin-top: 16px; color: var(--color-text-muted); font-size: 12px; }
.queue-history summary { cursor: pointer; padding: 4px 0; }
.queue-history p { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 8px 0 0; padding-top: 8px; border-top: 1px solid var(--color-line); }
.queue-history strong { color: var(--color-ink); }
.el-button { min-height: 36px; }
@media (max-width: 640px) { .download-batch-queue { padding: 20px 16px; }.queue-header { align-items: flex-start; }.queue-card { padding: 14px; }.queue-card-footer { align-items: flex-start; flex-direction: column; }.queue-header > .el-button { flex-shrink: 0; }.queue-controls { align-self: flex-end; } }
</style>
