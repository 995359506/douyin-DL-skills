import api from './index'

export interface ContentSource {
  source_id: string; revision: string; aweme_id?: string; task_id?: string
  title: string; author: string; source_type: string; text: string
  field_path?: string; block_id?: string; time_seconds?: number | null; time_basis?: string
  spans?: number[][]; source_url?: string; source_updated?: boolean; source_missing?: boolean
}
export interface ContentVideo {
  aweme_id: string; title: string; author: string; cover?: string; task_id?: string
  media_url?: string; media_available?: boolean; duration?: number | null; source_url?: string
  sources?: ContentSource[]; blocks?: ContentSource[]; matches?: ContentSource[]
  reading?: { state: string; rewatch: boolean }; reading_state?: { state: string; rewatch: boolean }
  collected_at?: string
}
export interface Material {
  id: string; title: string; excerpt: string; text?: string; source_type: string; author?: string
  aweme_id?: string; source_id?: string; revision?: string; source_url?: string
  tags: string[]; notes: string; rewrite: string; created_at?: string; time_seconds?: number | null
  source_updated?: boolean; source_missing?: boolean
}
export interface TopicMember {
  id: string; title: string; text: string; source_type?: string; source_id?: string; aweme_id?: string
  material_id?: string; revision?: string; group_name?: string; position?: number; notes?: string
}
export interface TopicVersion {
  id: string; status: string; action?: string; created_at?: string; output_markdown: string
  edited_markdown?: string; inputs?: Array<{ citation_id: string; title: string; text: string; source_type: string; source_url?: string; source_id?: string; revision?: string; aweme_id?: string }>
  error?: string; warnings?: string[]
}
export interface Topic {
  id: string; name: string; goal: string; description: string; settings?: Record<string, string>
  members?: TopicMember[]; versions?: TopicVersion[]
}
async function data<T>(request: Promise<any>): Promise<T> {
  const response = await request
  if (!response.success) throw new Error(response.message || '内容请求失败')
  return response.data
}
const id = encodeURIComponent
export const contentApi = {
  search: (params: Record<string, any>) => data<{ items: ContentVideo[]; total: number; authors?: string[] }>(api.get('/content/search', { params })),
  video: (awemeId: string) => data<ContentVideo>(api.get(`/content/videos/${id(awemeId)}`)),
  task: (taskId: string) => data<ContentVideo>(api.get(`/content/tasks/${id(taskId)}`)),
  source: (sourceId: string, revision?: string) => data<ContentSource>(api.get(`/content/sources/${id(sourceId)}`, { params: { revision } })),
  reading: (awemeId: string, value: { state: string; rewatch: boolean }) => data<any>(api.put(`/content/videos/${id(awemeId)}/reading`, value)),
  materials: (params: Record<string, any> = {}) => data<{ items: Material[]; total: number; authors?: string[]; tags?: string[] }>(api.get('/content/materials', { params })),
  createMaterial: (value: Record<string, any>) => data<Material>(api.post('/content/materials', value)),
  updateMaterial: (materialId: string, value: Record<string, any>) => data<Material>(api.patch(`/content/materials/${id(materialId)}`, value)),
  deleteMaterial: (materialId: string) => data<any>(api.delete(`/content/materials/${id(materialId)}`)),
  topics: () => data<{ items: Topic[]; total: number }>(api.get('/content/topics')),
  topic: (topicId: string) => data<Topic>(api.get(`/content/topics/${id(topicId)}`)),
  createTopic: (value: Record<string, any>) => data<Topic>(api.post('/content/topics', value)),
  updateTopic: (topicId: string, value: Record<string, any>) => data<Topic>(api.patch(`/content/topics/${id(topicId)}`, value)),
  deleteTopic: (topicId: string) => data<any>(api.delete(`/content/topics/${id(topicId)}`)),
  addMember: (topicId: string, value: Record<string, any>) => data<TopicMember>(api.post(`/content/topics/${id(topicId)}/members`, value)),
  updateMember: (topicId: string, memberId: string, value: Record<string, any>) => data<TopicMember>(api.patch(`/content/topics/${id(topicId)}/members/${id(memberId)}`, value)),
  removeMember: (topicId: string, memberId: string) => data<any>(api.delete(`/content/topics/${id(topicId)}/members/${id(memberId)}`)),
  preview: (topicId: string, value: Record<string, any>) => data<any>(api.post(`/content/topics/${id(topicId)}/preview`, value)),
  generate: (topicId: string, value: Record<string, any>) => data<TopicVersion>(api.post(`/content/topics/${id(topicId)}/generations`, value, { timeout: 600000 })),
  editVersion: (topicId: string, versionId: string, edited_markdown: string) => data<TopicVersion>(api.patch(`/content/topics/${id(topicId)}/generations/${id(versionId)}`, { edited_markdown })),
  topicLimit: (topicId: string, request_limit: number | null) => data<any>(api.post(`/batch-results/topic/${id(topicId)}/limit`, { request_limit })),
  resumeTopic: (topicId: string) => data<any>(api.post(`/batch-results/topic/${id(topicId)}/resume`, {}, { timeout: 600000 })),
  exportUrl: (topicId: string, versionId?: string) => `/api/content/topics/${id(topicId)}/export${versionId ? `?version_id=${id(versionId)}` : ''}`,
}

export const sourceLabels: Record<string, string> = {
  original: '原始转写 · 未核实', polished: '整理稿 · AI 整理', summary: 'AI 视频总结',
  distill: '博主蒸馏 · AI 归纳', synthesis: '博主蒸馏 · AI 归纳', note: '个人笔记',
  original_transcript: '原始转写 · 未核实', polished_text: '整理稿 · AI 整理',
  metadata: '视频信息',
}
export function sourceLabel(type: string) { return sourceLabels[type] || type || '来源资料' }
export interface ReaderTarget { aweme_id?: string; task_id?: string; source_id?: string; revision?: string; spans?: number[][] }
export function openContentReader(target: ReaderTarget) { window.dispatchEvent(new CustomEvent('content-reader', { detail: target })) }
export function addToTopic(target: Record<string, any>) { window.dispatchEvent(new CustomEvent('content-topic', { detail: target })) }
export function collectContent(target: { task_id: string; source_type?: string; text?: string }) { window.dispatchEvent(new CustomEvent('content-collect', { detail: target })) }
