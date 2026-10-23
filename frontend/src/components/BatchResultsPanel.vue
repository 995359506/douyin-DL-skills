<template>
  <section id="batch-results" class="batch-results" aria-labelledby="batch-results-title">
    <header><div><p class="kicker">BATCH / RESULTS</p><h2 id="batch-results-title">批次结果与调用额度</h2><p class="help">下载与分析分别统计。已有内容复用不产生新请求；历史缺少的用量和费用保持未知。</p></div><el-button :loading="loading" @click="loadList">刷新</el-button></header>
    <div class="controls"><el-select v-model="selected" placeholder="选择一个批次" aria-label="选择批次" @change="changeBatch"><el-option v-for="batch in batches" :key="key(batch)" :value="key(batch)" :label="`${kindLabel(batch.kind)} · ${batch.author || '未命名'} · ${batch.created_at.slice(0, 16).replace('T', ' ')}`" /></el-select><el-button v-if="report" @click="expanded = !expanded">{{ expanded ? '收起明细' : '展开明细' }}</el-button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <div class="stage-grid"><div v-for="stage in stages" :key="stage.key"><h3>{{ stage.label }}</h3><p><span v-for="(count, state) in report.counts[stage.key]" :key="state">{{ stateLabel(String(state)) }} <strong>{{ count }}</strong></span><span v-if="!Object.keys(report.counts[stage.key]).length">暂无记录</span></p></div></div>
      <div class="usage"><p><strong>实际请求 {{ report.usage.request_count ?? '未知' }} 次</strong><span>逻辑执行 {{ report.usage.logical_execution_count ?? '未知' }} 次</span><span>重试 {{ report.usage.retry_request_count ?? '未知' }} 次</span><span>备用通道 {{ report.usage.fallback_request_count ?? '未知' }} 次</span><span>在途 {{ report.usage.in_flight_count ?? '未知' }} 次</span></p><p>费用：{{ costText }}<span>用量未返回 {{ report.usage.usage_unknown_count ?? '未知' }} 次</span></p><p class="help">{{ report.eta.message }}<template v-if="report.usage.network_duration_seconds_range"> 单次网络请求历史区间：{{ report.usage.network_duration_seconds_range.map((n: number) => `${Math.round(n)}秒`).join('–') }}。</template></p></div>
      <p v-for="(estimate, stage) in etaEstimates" :key="stage" class="help">{{ stage === 'summary' ? '总结' : '口播' }}剩余处理时间：{{ estimate.remaining_seconds_range!.map((n: number) => `${Math.ceil(n / 60)}分钟`).join('–') }}（{{ estimate.sample_count }} 次历史同类执行样本；暂停和下载冷却另计）</p>
      <ModelUsageDetails :kind="selected.split('/')[0]" :batch-id="selected.split('/').slice(1).join('/')" />
      <form class="budget" @submit.prevent="saveLimit"><label for="batch-call-limit">本批次累计请求次数上限</label><el-input-number id="batch-call-limit" v-model="limit" :min="0" :max="1000000" :disabled="unlimited" aria-label="本批次累计请求次数上限" /><el-checkbox v-model="unlimited">不设上限</el-checkbox><el-button native-type="submit" :loading="saving">保存上限</el-button><el-button type="primary" plain :loading="resuming" @click="resume">继续未完成分析</el-button><p class="help">分段、重试、备用通道、合并与归纳均占次数。达到上限后停止启动新请求，在途请求仍可能完成并计费；这里控制次数，不承诺金额上限。</p></form>
      <div v-if="expanded" class="detail">
        <div class="controls"><el-select v-model="filterStatus" aria-label="处理状态筛选" @change="resetPage"><el-option value="" label="全部处理状态" /><el-option v-for="s in ['completed','reused','shared','failed','partial','paused','pending','not_requested']" :key="s" :value="s" :label="stateLabel(s)" /></el-select><el-select v-model="author" aria-label="批次作者筛选" @change="resetPage"><el-option value="" label="全部作者" /><el-option v-for="name in report.authors" :key="name" :value="name" :label="name || '未知作者'" /></el-select></div>
        <p v-if="!report.items.length" class="help">没有符合筛选条件的内容。</p>
        <article v-for="item in report.items" :key="item.aweme_id || item.task_id" class="result-row"><img v-if="item.cover" :src="item.cover" alt="视频封面" loading="lazy" referrerpolicy="no-referrer" /><div><strong>{{ item.title }}</strong><p>{{ item.author || '未知作者' }}</p><div class="item-stages"><span v-for="stage in stages" :key="stage.key">{{ stage.label }}：{{ stateLabel(item[stage.key]) }}</span></div><p v-for="(action, index) in item.actions" :key="index" class="error">{{ action.reason }} · {{ action.action }}</p></div><div class="row-actions"><el-button :disabled="!item.aweme_id" @click="read(item)">阅读与播放</el-button><el-button v-if="item.summary === 'failed' || item.transcript === 'failed'" @click="retry(item)">重试失败分析</el-button></div></article>
        <el-pagination v-if="report.total > 30" v-model:current-page="page" :page-size="30" :total="report.total" layout="prev, pager, next" @current-change="loadReport" />
      </div>
    </template>
    <p v-else-if="!loading && !error" class="help">选择已有批次查看逐条结果。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import ModelUsageDetails from '@/components/ModelUsageDetails.vue'

interface Batch { kind: string; batch_id: string; author: string; created_at: string; status: string; total: number }
interface Item { task_id: string; aweme_id: string; title: string; author: string; cover?: string; download: string; summary: string; transcript: string; actions: Array<{ stage: string; reason: string; action: string }> }
interface Report { batch: Batch; counts: Record<string, Record<string, number>>; items: Item[]; authors: string[]; total: number; usage: Record<string, any>; eta: { message: string; analysis?: Record<string, { remaining_seconds_range: number[] | null; sample_count: number }> } }
const batches = ref<Batch[]>([]), selected = ref(''), report = ref<Report | null>(null)
const loading = ref(false), saving = ref(false), resuming = ref(false), expanded = ref(false), error = ref('')
const unlimited = ref(true), limit = ref(100), filterStatus = ref(''), author = ref(''), page = ref(1)
let reportSequence = 0
const stages: Array<{ key: 'download' | 'transcript' | 'summary'; label: string }> = [{ key: 'download', label: '下载' }, { key: 'transcript', label: '口播' }, { key: 'summary', label: '总结' }]
const key = (batch: Batch) => `${batch.kind}/${batch.batch_id}`
const kindLabel = (kind: string) => ({ download: '下载批次', summary: '作者总结', transcript: '作者口播', distill: '博主蒸馏', single: '单视频分析', topic: '专题生成' }[kind] || kind)
const stateLabel = (state: string) => ({ completed: '完成', reused: '复用', shared: '共享处理中结果', failed: '失败', partial: '部分完成', paused: '已暂停', cancelled: '已取消', pending: '待处理', running: '处理中', not_requested: '未请求', skipped: '跳过' }[state] || state)
const costText = computed(() => { const usage = report.value?.usage; if (!usage || usage.cost_status === 'unknown') return '未知（缺少价格或上游用量）'; const values = Object.entries(usage.estimated_costs || {}).map(([currency, amount]) => `${Number(amount).toFixed(4)} ${currency}`); return `${values.join(' / ') || '未知'}（按配置单价估算${usage.cost_status === 'incomplete' ? '，不完整' : ''}）` })
const etaEstimates = computed(() => Object.fromEntries(Object.entries(report.value?.eta.analysis || {}).filter(([, estimate]) => Array.isArray(estimate.remaining_seconds_range))))
async function unwrap(path: string, method = 'get', data?: unknown) { const value: any = method === 'get' ? await api.get(path) : await api.post(path, data); if (!value.success) throw new Error(value.message || '请求失败'); return value.data }
async function loadList() { loading.value = true; error.value = ''; try { batches.value = (await unwrap('/batch-results')).batches; if (selected.value) await loadReport() } catch (e: any) { error.value = e.message || '批次读取失败' } finally { loading.value = false } }
async function loadReport() {
  const sequence = ++reportSequence, requestKey = selected.value
  if (!requestKey) return
  const requestPage = page.value, requestStatus = filterStatus.value, requestAuthor = author.value
  error.value = ''
  try {
    const data = await unwrap(`/batch-results/${requestKey}?page=${requestPage}&status=${encodeURIComponent(requestStatus)}&author=${encodeURIComponent(requestAuthor)}`)
    if (sequence !== reportSequence || selected.value !== requestKey) return
    report.value = data; unlimited.value = data.usage.request_limit == null; limit.value = data.usage.request_limit ?? 100
  } catch (e: any) { if (sequence === reportSequence && selected.value === requestKey) error.value = e.message || '结果读取失败' }
}
function changeBatch() { reportSequence++; page.value = 1; filterStatus.value = ''; author.value = ''; report.value = null; error.value = ''; unlimited.value = true; limit.value = 100; void loadReport() }
const resetPage = () => { page.value = 1; void loadReport() }
async function saveLimit() { saving.value = true; try { await unwrap(`/batch-results/${selected.value}/limit`, 'post', { request_limit: unlimited.value ? null : limit.value }); ElMessage.success('调用次数上限已保存'); await loadReport() } catch (e: any) { error.value = e.message } finally { saving.value = false } }
async function resume() { resuming.value = true; try { await unwrap(`/batch-results/${selected.value}/resume`, 'post', {}); ElMessage.success('已继续未完成分析'); await loadReport() } catch (e: any) { error.value = e.message } finally { resuming.value = false } }
function read(item: Item) { window.dispatchEvent(new CustomEvent('content-reader', { detail: { awemeId: item.aweme_id, aweme_id: item.aweme_id } })) }
async function retry(item: Item) { try { await ElMessageBox.confirm('仅重试失败的分析阶段，会调用已配置模型并受本批次额度约束；已有成功结果会保留。', '重试失败分析', { confirmButtonText: '确认重试', cancelButtonText: '返回' }); await unwrap(`/batch-results/${selected.value}/retry`, 'post', { task_id: item.task_id }); ElMessage.success('已提交失败阶段重试'); await loadReport() } catch (e: any) { if (e !== 'cancel' && e !== 'close') error.value = e.message || '重试未成功' } }
onMounted(() => { void loadList() })
onBeforeUnmount(() => { reportSequence++ })
</script>

<style scoped>
.batch-results{margin-top:var(--space-5);padding:24px 28px;background:var(--color-surface);border:1px solid var(--color-line)}header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}h2{margin:0;font:700 23px var(--font-display)}.kicker{margin:0 0 8px;font:700 10px var(--font-mono);letter-spacing:.12em;color:var(--color-text-muted)}.help{font-size:12px;color:var(--color-text-muted);line-height:1.7}.controls{display:flex;gap:12px;margin:18px 0}.controls .el-select{width:min(520px,100%)}.stage-grid{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--color-line)}.stage-grid>div{padding:16px;border-right:1px solid var(--color-line)}.stage-grid>div:last-child{border:0}.stage-grid h3{font-size:13px;margin:0 0 12px}.stage-grid p{display:flex;gap:12px;flex-wrap:wrap;margin:0;font-size:12px;color:var(--color-text-muted)}strong{color:var(--color-ink)}.usage{margin:18px 0;font-size:13px}.usage p{display:flex;gap:16px;flex-wrap:wrap}.budget{display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--color-line);padding-top:18px;font-size:12px}.budget p{flex-basis:100%;margin:0}.result-row{display:flex;gap:16px;padding:18px 0;border-top:1px solid var(--color-line)}.result-row>img{width:64px;height:88px;object-fit:cover}.result-row>div:nth-last-child(2){flex:1;min-width:0}.result-row strong{font-size:14px;overflow-wrap:anywhere}.result-row p{font-size:12px;color:var(--color-text-muted)}.item-stages{display:flex;gap:14px;font-size:11px;flex-wrap:wrap}.row-actions{display:flex;flex-direction:column;gap:8px;align-items:flex-end}.row-actions .el-button{margin:0}.error{font-size:12px;color:var(--el-color-danger)!important;overflow-wrap:anywhere}@media(max-width:680px){.batch-results{padding:20px 16px}.stage-grid{grid-template-columns:1fr}.stage-grid>div{border-right:0;border-bottom:1px solid var(--color-line)}.controls{flex-wrap:wrap}.result-row{flex-wrap:wrap}.row-actions{flex-direction:row;width:100%}.budget .el-input-number{width:130px}}
</style>
