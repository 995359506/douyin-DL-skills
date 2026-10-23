<template>
  <details class="model-usage-details" @toggle="onToggle">
    <summary>查看通道、模型与实际请求 / 配置费用估算单价</summary>
    <div class="usage-detail-body" v-loading="loading">
      <div class="detail-heading"><p>只显示调用元数据。一次逻辑分析可能包含多个真实请求，分段、重试与备用通道分别记账；复用不增加请求。</p><el-button size="small" @click="load">刷新</el-button></div>
      <p v-if="error" class="detail-error" role="alert">{{ error }}</p>
      <div class="usage-table-scroll"><table><caption>按通道与模型分组</caption><thead><tr><th>通道 / 模型</th><th>请求 / 成功</th><th>重试 / 备用</th><th>用量已知 / 未知</th><th>估算费用</th></tr></thead><tbody><tr v-for="group in stats.provider_models || []" :key="`${group.provider}/${group.model}`"><td><strong>{{ group.provider }}</strong><br />{{ group.model }}</td><td>{{ group.request_count }} / {{ group.succeeded_count }}</td><td>{{ group.retry_request_count }} / {{ group.fallback_request_count }}</td><td>{{ group.usage_known_count }} / {{ group.usage_unknown_count }}</td><td>{{ cost(group.estimated_costs, group.unknown_cost_request_count) }}</td></tr><tr v-if="!stats.provider_models?.length"><td colspan="5">没有可核实的请求记录；早期历史用量保持未知。</td></tr></tbody></table></div>
      <div class="usage-table-scroll"><table><caption>实际请求明细（仅元数据）</caption><thead><tr><th>时间 / 阶段</th><th>模型</th><th>状态 / HTTP</th><th>重试 / 备用</th><th>输入 / 输出 token</th><th>耗时 / 估算费用</th></tr></thead><tbody><tr v-for="attempt in attempts" :key="attempt.request_id"><td>{{ date(attempt.started_at) }}<br />{{ attempt.stage }}<template v-if="attempt.segment_index != null"> / 片段 {{ attempt.segment_index }}</template></td><td>{{ attempt.provider }}<br />{{ attempt.model }}</td><td>{{ statuses[attempt.status] || attempt.status }} / {{ attempt.http_status ?? '未知' }}</td><td>{{ attempt.retry_index || 0 }} / {{ attempt.is_fallback ? '是' : '否' }}</td><td>{{ attempt.input_tokens ?? '未知' }} / {{ attempt.output_tokens ?? '未知' }}</td><td>{{ attempt.elapsed_seconds == null ? '未知耗时' : `${Number(attempt.elapsed_seconds).toFixed(1)} 秒` }}<br />{{ attempt.estimated_cost == null ? '费用未知' : `${attempt.currency} ${Number(attempt.estimated_cost).toFixed(6)}（估算）` }}</td></tr><tr v-if="!attempts.length"><td colspan="6">暂无明细</td></tr></tbody></table></div>
      <el-pagination v-if="stats.request_count > 50" :current-page="page" :total="stats.request_count" :page-size="50" layout="prev,pager,next" @current-change="(value: number) => { page = value; load() }" />
      <details class="price-config"><summary>配置已知单价（仅影响后续请求）</summary><p>费用是配置单价与上游实际用量的估算，并非账单或金额硬上限。缺少单价或必要用量时保持未知，不会算作零；填 0 表示你明确确认免费。不同币种分别累计。</p>
        <div v-if="prices.length" class="saved-prices"><button v-for="price in prices" :key="`${price.provider}/${price.model}`" type="button" @click="editPrice(price)">{{ price.provider }} / {{ price.model }} · {{ price.currency }} · {{ price.version }}</button></div>
        <form class="price-form" @submit.prevent="savePrice"><label>通道标识<input v-model="priceForm.provider" placeholder="例如 gateway" required /></label><label>模型标识<input v-model="priceForm.model" placeholder="填写实际模型名" required /></label><label>费率版本<input v-model="priceForm.version" placeholder="例如 2026-10-23" required /></label><label>币种<input v-model="priceForm.currency" placeholder="例如 CNY / USD" maxlength="3" required /></label><label>计费方式<select v-model="priceForm.mode"><option value="token">按输入 / 输出 token</option><option value="request">按实际请求次数</option><option value="audio">按音频秒数</option></select></label><template v-if="priceForm.mode === 'token'"><label>输入 / 百万 token<input v-model="priceForm.input" type="number" min="0" step="any" placeholder="未知请留空" /></label><label>输出 / 百万 token<input v-model="priceForm.output" type="number" min="0" step="any" placeholder="未知请留空" /></label></template><label v-if="priceForm.mode === 'request'">每次请求<input v-model="priceForm.request" type="number" min="0" step="any" placeholder="未知请留空" /></label><label v-if="priceForm.mode === 'audio'">每秒音频<input v-model="priceForm.audio" type="number" min="0" step="any" placeholder="未知请留空" /></label><div class="price-submit"><el-button native-type="submit" type="primary" :loading="saving">保存后续估算单价</el-button><el-button @click="resetPrice">清空表单</el-button></div></form>
        <p>保存一个计费方式会清空该模型其他方式的单价，避免叠加。历史请求保留发送时的费率快照，不追溯改写。</p>
      </details>
    </div>
  </details>
</template>

<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
const props = defineProps<{ kind: string; batchId: string }>()
const opened = ref(false), loading = ref(false), saving = ref(false), error = ref(''), page = ref(1)
const stats = ref<any>({}), attempts = ref<any[]>([]), prices = ref<any[]>([])
const priceForm = reactive({ provider: '', model: '', version: '', currency: '', mode: 'token', input: '', output: '', request: '', audio: '' })
const statuses: Record<string, string> = { reserved: '已预留', sent: '已发送', started: '在途', succeeded: '成功', failed: '失败', unknown: '结果未知', cancelled: '已取消' }
let sequence = 0
async function unwrap(request: Promise<any>) { const response = await request; if (!response.success) throw new Error(response.message || '读取模型用量失败'); return response.data }
async function load() {
  if (!props.kind || !props.batchId) return
  const current = ++sequence; loading.value = true; error.value = ''
  try {
    const result = await unwrap(api.get(`/model-usage/${encodeURIComponent(props.kind)}/${encodeURIComponent(props.batchId)}`, { params: { limit: 50, offset: (page.value - 1) * 50 } }))
    if (current !== sequence) return
    stats.value = result.stats || {}; attempts.value = result.attempts || []
    const rates = await unwrap(api.get('/model-prices')); if (current === sequence) prices.value = rates.prices || []
  } catch (e: any) { if (current === sequence) error.value = e.message }
  finally { if (current === sequence) loading.value = false }
}
function onToggle(event: Event) { opened.value = (event.target as HTMLDetailsElement).open; if (opened.value) void load() }
function cost(values: Record<string, number> = {}, unknown = 0) { const entries = Object.entries(values).map(([currency, amount]) => `${currency} ${Number(amount).toFixed(6)}`); return entries.length ? `${entries.join(' + ')}（估算${unknown ? '，不完整' : ''}）` : '未知' }
function date(value: string) { const time = new Date(value); return Number.isNaN(time.valueOf()) ? '时间未知' : time.toLocaleString('zh-CN') }
function resetPrice() { Object.assign(priceForm, { provider: '', model: '', version: '', currency: '', mode: 'token', input: '', output: '', request: '', audio: '' }) }
function editPrice(price: any) { Object.assign(priceForm, { provider: price.provider, model: price.model, version: price.version, currency: price.currency, mode: price.per_request != null ? 'request' : price.per_audio_second != null ? 'audio' : 'token', input: price.input_per_million ?? '', output: price.output_per_million ?? '', request: price.per_request ?? '', audio: price.per_audio_second ?? '' }) }
function number(value: string) { return value === '' ? null : Number(value) }
async function savePrice() {
  const values = { provider: priceForm.provider.trim(), model: priceForm.model.trim(), version: priceForm.version.trim(), currency: priceForm.currency.trim().toUpperCase(), input_per_million: priceForm.mode === 'token' ? number(priceForm.input) : null, output_per_million: priceForm.mode === 'token' ? number(priceForm.output) : null, per_request: priceForm.mode === 'request' ? number(priceForm.request) : null, per_audio_second: priceForm.mode === 'audio' ? number(priceForm.audio) : null }
  const rates = [values.input_per_million, values.output_per_million, values.per_request, values.per_audio_second]
  if (rates.every(value => value == null) || rates.some(value => value != null && (!Number.isFinite(value) || value < 0))) { ElMessage.warning('请填写至少一项非负的明确单价，未知项保持空白'); return }
  saving.value = true
  try { const result = await unwrap(api.put('/model-prices', values)); prices.value = result.prices || []; ElMessage.success('估算单价已保存，仅影响后续请求') }
  catch (e: any) { ElMessage.error(typeof e.message === 'string' ? e.message : '单价格式有误，请核对通道、模型与币种') }
  finally { saving.value = false }
}
watch(() => [props.kind, props.batchId], () => { page.value = 1; stats.value = {}; attempts.value = []; if (opened.value) void load() })
</script>

<style scoped>
.model-usage-details{border:1px solid var(--color-line);margin:18px 0;font-size:12px}.model-usage-details>summary,.price-config>summary{padding:12px 14px;cursor:pointer;font-weight:600;background:var(--color-surface)}.usage-detail-body{padding:16px}.detail-heading{display:flex;gap:16px;justify-content:space-between;align-items:start}.detail-heading p,.price-config p{color:var(--color-text-muted);font-size:12px;line-height:1.8;margin:0 0 14px}.detail-error{color:var(--color-ink);border-left:3px solid var(--color-line-strong);padding-left:10px}.usage-table-scroll{overflow:auto;margin:14px 0}.usage-table-scroll table{border-collapse:collapse;width:100%;min-width:650px;font-size:11px;text-align:left}.usage-table-scroll caption{text-align:left;font-weight:700;padding:8px 0}.usage-table-scroll th,.usage-table-scroll td{padding:10px 12px;border:1px solid var(--color-line);vertical-align:top;line-height:1.7}.usage-table-scroll th{background:var(--color-surface)}.price-config{margin-top:20px;border-top:1px solid var(--color-line)}.price-config p{margin:14px 0}.price-form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:18px 0}.price-form label{display:grid;gap:6px;font-size:11px}.price-form input,.price-form select{padding:9px 10px;border:1px solid var(--color-line-strong);background:var(--color-paper);color:var(--color-ink);min-width:0}.price-submit{grid-column:1/-1}.saved-prices{display:flex;gap:8px;flex-wrap:wrap}.saved-prices button{border:1px solid var(--color-line);padding:7px 9px;color:var(--color-text-muted);background:var(--color-paper);font-size:11px;cursor:pointer}.saved-prices button:hover{border-color:var(--color-accent)}@media(max-width:700px){.price-form{grid-template-columns:1fr 1fr}}@media(max-width:440px){.price-form{grid-template-columns:1fr}.usage-detail-body{padding:10px}}
</style>
