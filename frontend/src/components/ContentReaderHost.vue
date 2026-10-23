<template>
  <el-dialog v-model="visible" class="content-reader-dialog" title="来源阅读与视频核对" width="min(1280px, 96vw)" top="4vh" destroy-on-close @close="stopVideo">
    <div v-loading="loading" class="content-reader">
      <el-alert v-if="error" type="error" :title="error" :closable="false" />
      <template v-else-if="video || blocks.length">
        <header class="reader-heading"><div><p>{{ video?.author || blocks[0]?.author || '未知作者' }}</p><h2>{{ video?.title || blocks[0]?.title || '来源内容' }}</h2></div><a v-if="video?.source_url" :href="video.source_url" target="_blank" rel="noreferrer">查看抖音原链接 ↗</a></header>
        <div v-if="video?.aweme_id" class="reading-controls">
          <label>阅读标记 <select v-model="reading.state" @change="saveReading"><option value="pending">待看</option><option value="read">已看</option></select></label>
          <label><input v-model="reading.rewatch" type="checkbox" @change="saveReading" /> 值得重看</label>
          <span>与下载、分析状态分别记录</span>
        </div>
        <div class="reader-layout">
          <aside class="reader-media">
            <video v-if="video?.media_url && !mediaError" ref="player" :src="video.media_url" controls playsinline preload="metadata" @loadedmetadata="onMetadata" @timeupdate="onTimeUpdate" @error="mediaError = true" />
            <div v-else class="reader-no-media"><strong>本地视频暂不可播放</strong><p>{{ mediaError ? '文件无法读取，可保留文字继续阅读。' : '文件缺失或尚未下载，保留的文字仍可查看、收藏。' }}</p></div>
            <p class="reader-media-note">打开内容不会自动播放。点击有效时间点后跳转并播放；模型估计时间以“约”标识。</p>
            <div v-if="timeline.length" class="timeline-tools"><strong>视频时间线</strong><label><input v-model="follow" type="checkbox" /> 跟随播放</label></div>
            <p v-else class="reader-media-note">当前资料没有可靠的时间线。文字仍可阅读、收藏与核对；历史视频不会自动重新分析。</p>
            <div class="reader-timeline" @wheel.passive="follow = false" @touchmove.passive="follow = false">
              <button v-for="block in timeline" :key="block.source_id" :ref="el => setTimelineElement(block.source_id, el)" type="button" :class="{ active: activeTimeId === block.source_id }" :disabled="!seekable(block)" :title="seekable(block) ? '跳转并播放' : '时间未经可靠换算、超出片长或视频不可用'" @click="seek(block)"><span>{{ displayTime(block) }}</span><span>{{ block.text }}</span></button>
            </div>
          </aside>
          <div class="reader-texts" @mouseup="captureSelection" @keyup="captureSelection">
            <p class="reader-guidance">选择文字后收藏；手机可点击每段下方“摘录此段”，再调整收藏范围。原始转写未经人工核实。</p>
            <article v-for="block in blocks" :id="`source-${block.source_id}`" :key="block.source_id" class="source-block" :class="{ 'is-focused': focusedId === block.source_id }" :data-source="block.source_id">
              <header><span>{{ sourceLabel(block.source_type) }}</span><button v-if="block.time_seconds != null" type="button" :disabled="!seekable(block)" @click="seek(block)">{{ displayTime(block) }}</button></header>
              <p v-if="block.source_missing" class="reader-guidance">来源已删除，当前显示收藏时保留的文本快照。</p><p v-else-if="block.source_updated" class="reader-guidance">来源已更新，当前定位的是原版本快照。</p>
              <p class="source-text" :data-source-text="block.source_id"><span v-for="(part, index) in textParts(block.text, block.source_id === focusedId ? focusedSpans : block.spans)" :key="index"><mark v-if="part.hit">{{ part.text }}</mark><template v-else>{{ part.text }}</template></span></p>
              <footer><el-button size="small" @click="collect(block)">摘录此段</el-button><el-button size="small" @click="topicPayload = { source_id: block.source_id, revision: block.revision }; openTopicPicker()">加入专题</el-button><el-button size="small" @click="copy(block.text)">复制</el-button></footer>
            </article>
            <el-empty v-if="!blocks.length" description="暂无已保存的口播或总结，完成分析后即可阅读" />
          </div>
        </div>
      </template>
    </div>
    <div v-if="selection && visible" class="selection-collect"><span>已选择 {{ selection.end - selection.start }} 字</span><el-button type="primary" @click="collect(selection.source, selection.start, selection.end)">收藏选中文字</el-button></div>
  </el-dialog>

  <el-dialog v-model="collectVisible" title="收藏为句子素材" width="min(620px, 94vw)" append-to-body>
    <p class="content-muted">{{ sourceLabel(collectSource?.source_type || 'note') }} · 收藏的是此刻文本快照，来源更新不会覆盖。</p>
    <label class="content-field">素材标题<input v-model="materialDraft.title" maxlength="200" /></label>
    <label class="content-field">来源摘录（调整本次摘录范围）<textarea v-model="materialDraft.excerpt" rows="6" /></label>
    <label class="content-field">标签（逗号分隔）<input v-model="materialDraft.tags" /></label>
    <label class="content-field">我的理解与备注<textarea v-model="materialDraft.notes" rows="3" /></label>
    <label class="content-field">个人改写（独立保存）<textarea v-model="materialDraft.rewrite" rows="3" /></label>
    <el-alert v-if="duplicateId" title="已经收藏过相同来源的这段内容" description="可以更新已有素材的备注，或明确另存一份。原始来源摘录保持不变。" type="info" :closable="false" />
    <template #footer><el-button @click="collectVisible = false">取消</el-button><el-button v-if="duplicateId" :loading="saving" @click="saveMaterial('update')">更新已有备注</el-button><el-button type="primary" :loading="saving" @click="saveMaterial(duplicateId ? 'duplicate' : 'create')">{{ duplicateId ? '另存一份' : '保存素材' }}</el-button></template>
  </el-dialog>

  <el-dialog v-model="topicVisible" title="加入专题" width="min(480px, 94vw)" append-to-body>
    <p class="content-muted">同一资料可以加入多个专题；移除专题引用不会删除原资料。加入视频后仍需明确选择文字参与生成。</p>
    <label class="content-field">选择专题<select v-model="selectedTopic"><option value="">请选择</option><option v-for="topic in topics" :key="topic.id" :value="topic.id">{{ topic.name }}</option></select></label>
    <label class="content-field">或新建专题<input v-model="newTopicName" placeholder="专题名称" /></label>
    <template #footer><el-button @click="topicVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!selectedTopic && !newTopicName.trim()" @click="saveTopicMember">确认加入</el-button></template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { contentApi, sourceLabel, type ContentSource, type ContentVideo, type ReaderTarget, type Topic } from '@/api/content'
import { textParts, canSeek, timeLabel } from '@/utils/contentText'

const visible = ref(false), loading = ref(false), error = ref(''), video = ref<ContentVideo | null>(null)
const blocks = ref<ContentSource[]>([]), focusedId = ref(''), focusedSpans = ref<number[][]>([])
const player = ref<HTMLVideoElement | null>(null), mediaError = ref(false), duration = ref<number | null>(null)
const pendingSeek = ref<number | null>(null)
const reading = reactive({ state: 'pending', rewatch: false }), follow = ref(false), activeTimeId = ref('')
const timelineElements = new Map<string, Element>()
const timeline = computed(() => blocks.value.filter(b => b.time_seconds != null).slice().sort((a, b) => (a.time_seconds || 0) - (b.time_seconds || 0)))
const selection = ref<{ source: ContentSource; start: number; end: number } | null>(null)
const collectVisible = ref(false), collectSource = ref<ContentSource | null>(null), duplicateId = ref(''), saving = ref(false)
const materialDraft = reactive({ title: '', excerpt: '', tags: '', notes: '', rewrite: '' })
const topicVisible = ref(false), topics = ref<Topic[]>([]), selectedTopic = ref(''), newTopicName = ref(''), topicPayload = ref<Record<string, any>>({})
let readerSequence = 0
function stopVideo() { if (player.value) { player.value.pause(); player.value.removeAttribute('src'); player.value.load() }; selection.value = null }
async function load(target: ReaderTarget) {
  stopVideo(); const sequence = ++readerSequence; visible.value = true; loading.value = true; error.value = ''; video.value = null; blocks.value = []; mediaError.value = false; duration.value = null; pendingSeek.value = null; follow.value = false
  try {
    let source: ContentSource | null = null
    if (target.source_id) source = await contentApi.source(target.source_id, target.revision)
    const awemeId = target.aweme_id || source?.aweme_id
    let current: ContentVideo | null = null
    try { current = awemeId ? await contentApi.video(awemeId) : target.task_id ? await contentApi.task(target.task_id) : null }
    catch (e: any) { if (!source || e.response?.status !== 404) throw e }
    if (sequence !== readerSequence) return
    video.value = current; duration.value = current?.duration || null
    blocks.value = current?.sources || current?.blocks || []
    if (source) { blocks.value = blocks.value.filter(b => b.source_id !== source!.source_id); blocks.value.unshift(source) }
    if (source?.time_seconds != null && canSeek(source.time_seconds, source.time_basis, duration.value)) pendingSeek.value = source.time_seconds
    focusedId.value = source?.source_id || target.source_id || ''; focusedSpans.value = target.spans || source?.spans || []
    Object.assign(reading, current?.reading_state || current?.reading || { state: 'pending', rewatch: false })
    await nextTick(); if (focusedId.value) document.getElementById(`source-${focusedId.value}`)?.scrollIntoView({ block: 'center' })
  } catch (e: any) { if (sequence === readerSequence) error.value = e.message || '读取来源失败' }
  finally { if (sequence === readerSequence) loading.value = false }
}
function onReader(event: Event) { void load((event as CustomEvent<ReaderTarget>).detail) }
function onTopic(event: Event) { topicPayload.value = (event as CustomEvent).detail; void openTopicPicker() }
async function onCollect(event: Event) {
  const target = (event as CustomEvent<{ task_id: string; source_type?: string; text?: string }>).detail
  try {
    const current = await contentApi.task(target.task_id)
    const candidates = (current.sources || current.blocks || []).filter(source => !target.source_type || source.source_type === target.source_type)
    const selected = target.text?.trim()
    const source = selected ? candidates.find(item => item.text.includes(selected)) : candidates[0]
    if (!source) { ElMessage.info('请在“阅读与收藏”中选择来源段落'); void load({ task_id: target.task_id }); return }
    const start = selected ? source.text.indexOf(selected) : 0
    collect(source, start, selected ? start + selected.length : source.text.length)
  } catch (e: any) { ElMessage.error(e.message) }
}
function onMetadata() {
  duration.value = player.value?.duration && Number.isFinite(player.value.duration) ? player.value.duration : duration.value
  if (player.value && pendingSeek.value != null && canSeek(pendingSeek.value, undefined, duration.value)) { player.value.currentTime = pendingSeek.value; player.value.pause() }
  pendingSeek.value = null
}
function seekable(block: ContentSource) { return !!video.value?.media_url && !mediaError.value && canSeek(block.time_seconds, block.time_basis, duration.value) }
function displayTime(block: ContentSource) { const approximate = !/aligned|verified|exact/.test(block.time_basis || ''); return `${approximate ? '约 ' : ''}${timeLabel(block.time_seconds)}` }
async function seek(block: ContentSource) {
  if (!player.value || !seekable(block) || block.time_seconds == null) return
  player.value.currentTime = block.time_seconds
  try { await player.value.play() } catch { ElMessage.info('请点击播放器播放按钮继续') }
}
function setTimelineElement(id: string, element: unknown) { if (element instanceof Element) timelineElements.set(id, element); else timelineElements.delete(id) }
function onTimeUpdate() {
  const current = player.value?.currentTime || 0
  const entry = timeline.value.filter(b => seekable(b) && (b.time_seconds || 0) <= current).pop()
  if (entry && entry.source_id !== activeTimeId.value) { activeTimeId.value = entry.source_id; if (follow.value) timelineElements.get(entry.source_id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) }
}
async function saveReading() { if (video.value?.aweme_id) try { await contentApi.reading(video.value.aweme_id, { ...reading }) } catch (e: any) { ElMessage.error(e.message) } }
function captureSelection() {
  const selected = window.getSelection(); if (!selected?.rangeCount || selected.isCollapsed) { selection.value = null; return }
  const range = selected.getRangeAt(0), parent = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer as Element : range.commonAncestorContainer.parentElement
  const element = parent?.closest<HTMLElement>('[data-source-text]'); if (!element || !element.contains(range.startContainer) || !element.contains(range.endContainer)) { selection.value = null; return }
  const source = blocks.value.find(b => b.source_id === element.dataset.sourceText); if (!source) return
  const prefix = range.cloneRange(); prefix.selectNodeContents(element); prefix.setEnd(range.startContainer, range.startOffset)
  const start = prefix.toString().length; selection.value = { source, start, end: start + range.toString().length }
}
function collect(source: ContentSource, start = 0, end = source.text.length) {
  collectSource.value = source; duplicateId.value = ''; Object.assign(materialDraft, { title: source.title || '未命名素材', excerpt: source.text.slice(start, end), tags: '', notes: '', rewrite: '' }); collectVisible.value = true
}
async function saveMaterial(mode: 'create' | 'update' | 'duplicate') {
  const source = collectSource.value; if (!source || !materialDraft.excerpt.trim()) return
  const start = source.text.indexOf(materialDraft.excerpt)
  if (start < 0) { ElMessage.warning('来源摘录需是原文中的连续片段，改写请填写“个人改写”'); return }
  saving.value = true
  const fields = { title: materialDraft.title, tags: materialDraft.tags.split(/[,，]/).map(t => t.trim()).filter(Boolean), notes: materialDraft.notes, rewrite: materialDraft.rewrite }
  try {
    if (mode === 'update') await contentApi.updateMaterial(duplicateId.value, fields)
    else {
      const result: any = await contentApi.createMaterial({ ...fields, source_id: source.source_id, revision: source.revision, start, end: start + materialDraft.excerpt.length, allow_duplicate: mode === 'duplicate', request_id: crypto.randomUUID() })
      if (result.duplicate || result.existing_id) { duplicateId.value = result.existing_id || result.id || result.material?.id; return }
    }
    collectVisible.value = false; ElMessage.success('素材已保存，来源快照将独立保留'); window.dispatchEvent(new Event('content-changed'))
  } catch (e: any) { const detail = e.response?.data?.detail; if (e.response?.status === 409 && (detail?.existing_id || detail?.material_id)) duplicateId.value = detail.existing_id || detail.material_id; else ElMessage.error(e.message) }
  finally { saving.value = false }
}
async function openTopicPicker() { try { topics.value = (await contentApi.topics()).items; selectedTopic.value = ''; newTopicName.value = ''; topicVisible.value = true } catch (e: any) { ElMessage.error(e.message) } }
async function saveTopicMember() {
  saving.value = true
  try { let id = selectedTopic.value; if (newTopicName.value.trim()) id = (await contentApi.createTopic({ name: newTopicName.value.trim(), goal: '', description: '' })).id; await contentApi.addMember(id, topicPayload.value); topicVisible.value = false; ElMessage.success('已加入专题'); window.dispatchEvent(new Event('content-changed')) }
  catch (e: any) { ElMessage.error(e.message) } finally { saving.value = false }
}
async function copy(text: string) { try { await navigator.clipboard.writeText(text); ElMessage.success('已复制') } catch { ElMessage.error('复制失败') } }
onMounted(() => { window.addEventListener('content-reader', onReader); window.addEventListener('content-topic', onTopic); window.addEventListener('content-collect', onCollect) })
onBeforeUnmount(() => { readerSequence++; stopVideo(); window.removeEventListener('content-reader', onReader); window.removeEventListener('content-topic', onTopic); window.removeEventListener('content-collect', onCollect) })
</script>

<style scoped>
.reader-heading { display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--color-line);padding-bottom:18px; }.reader-heading p{margin:0;color:var(--color-text-muted);font-size:12px}.reader-heading h2{margin:6px 0 0;font-size:22px;line-height:1.4}.reader-heading a{white-space:nowrap;color:var(--color-accent);font-size:12px}.reading-controls{display:flex;flex-wrap:wrap;align-items:center;gap:20px;padding:14px 0;font-size:12px}.reading-controls span{color:var(--color-text-muted)}.reader-layout{display:grid;grid-template-columns:minmax(280px,.9fr) minmax(0,1.2fr);gap:24px}.reader-media video{width:100%;max-height:55vh;display:block;background:#000;object-fit:contain}.reader-no-media{padding:44px 24px;background:var(--color-surface);border:1px solid var(--color-line);text-align:center}.reader-no-media p,.reader-media-note,.reader-guidance{font-size:12px;color:var(--color-text-muted);line-height:1.7}.reader-texts{max-height:70vh;overflow:auto;padding:0 12px 20px 0;scroll-behavior:smooth}.reader-guidance{margin-top:0}.source-block{padding:16px 18px;border:1px solid var(--color-line);margin-bottom:14px}.source-block.is-focused{border-left:3px solid var(--color-accent)}.source-block header{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:var(--color-text-muted)}.source-block header button{border:0;background:transparent;color:var(--color-accent)}.source-block header button:disabled{color:var(--color-text-muted)}.source-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.9;font-size:14px}.source-block footer{display:flex;flex-wrap:wrap;gap:6px}.source-block footer .el-button{margin:0;min-height:34px}.source-text mark{background:var(--el-color-primary-light-9);color:var(--color-ink)}.timeline-tools{display:flex;justify-content:space-between;font-size:12px;margin:20px 0 8px}.reader-timeline{max-height:26vh;overflow:auto}.reader-timeline button{display:grid;grid-template-columns:70px 1fr;gap:10px;width:100%;padding:12px;border:0;border-bottom:1px solid var(--color-line);background:var(--color-paper);text-align:left;font-size:12px;cursor:pointer}.reader-timeline button.active{border-left:3px solid var(--color-accent);background:var(--color-surface-muted)}.reader-timeline button:disabled{opacity:.65;cursor:default}.selection-collect{position:sticky;bottom:0;display:flex;align-items:center;justify-content:flex-end;gap:16px;padding:12px;background:var(--color-paper);border-top:1px solid var(--color-line)}.content-field{display:grid;gap:6px;margin:14px 0;font-size:12px}.content-field input,.content-field textarea,.content-field select,.reading-controls select{padding:9px;border:1px solid var(--color-line-strong);background:var(--color-paper);color:var(--color-ink);font:inherit}.content-muted{color:var(--color-text-muted);font-size:12px;line-height:1.7}
@media(max-width:760px){.reader-layout{grid-template-columns:1fr}.reader-media video{max-height:38vh}.reader-texts{max-height:none;padding-right:0}.reader-heading{display:block}.reader-heading a{display:inline-block;margin-top:10px}.reader-timeline{max-height:160px}}
</style>
