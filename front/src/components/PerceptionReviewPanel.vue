<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  cancelPerceptionOperation, createPerceptionOperation, getPerception, getPerceptionHistory, getPerceptionOperation,
  type PerceptionFieldGroup, type PerceptionOperation, type PerceptionRevision, type PerceptionStageName, type PerceptionStatus, type PerceptionUsage,
} from '../api/perception'
import { perceptionDraftKey, readPerceptionDraft, savePerceptionDraft } from '../perceptionDrafts'

const props = defineProps<{ jobId?: string | null; projectId?: string; taskKey?: string; shotId: string; suggestion?: { reason?: string; field_group?: string } }>()
const emit = defineEmits<{ updated: [jobId: string] }>()
const status = ref<PerceptionStatus | null>(null)
const operation = ref<PerceptionOperation | null>(null)
const revisions = ref<PerceptionRevision[]>([])
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const fieldGroup = ref<PerceptionFieldGroup>('action')
const reason = ref('')
const stageNames: PerceptionStageName[] = ['static', 'motion']
const stageLabels = { static: '静态内容', motion: '动作变化' }
const stageStates = { succeeded: '已完成', partial: '部分字段可用', failed: '调用失败', deferred: '尚未执行' }
const operationStates = { queued: '等待执行', running: '正在分析', applied: '已更新事实', unchanged: '事实未改变', rejected: '证据不足，保留原值', failed: '执行失败', cancelled: '已取消', interrupted: '执行已中断', conflict: '版本已变化，未覆盖' }
const fieldLabels = { action: '动作与起止状态', subject: '人物与物体', scene: '场景', spatial: '空间关系', expression: '可见表情变化' }
const isActive = (item: PerceptionOperation | null) => !!item && ['queued', 'running'].includes(item.status)
const busy = computed(() => submitting.value || isActive(operation.value))
const canWrite = computed(() => !!props.jobId && !!status.value?.record_available && !status.value.read_only && !loading.value && !busy.value)
const operations = computed(() => operation.value ? [operation.value, ...(status.value?.operations || []).filter(item => item.id !== operation.value?.id)] : status.value?.operations || [])
let generation = 0
let controller = new AbortController()
let timer = 0
let loadedDraftKey: string | null = null
function keepDraft() {
  if (loadedDraftKey) savePerceptionDraft(loadedDraftKey, { fieldGroup: fieldGroup.value, reason: reason.value })
}

function stopRequests() {
  generation += 1
  controller.abort()
  controller = new AbortController()
  window.clearTimeout(timer)
  timer = 0
}
function current(scope: number) { return scope === generation && !controller.signal.aborted }
function message(value: unknown) { return value instanceof Error ? value.message : '读取失败，请稍后刷新记录' }
function stageError(value?: Record<string, unknown> | null) {
  return value ? String(value.message || value.error || value.detail || value.code || '请求未完成') : ''
}
function cost(usage: PerceptionUsage[] = []) {
  const known = usage.filter(item => typeof item.estimated_list_price_cny === 'number')
  if (!known.length) return '费用估算未提供'
  const total = known.reduce((sum, item) => sum + item.estimated_list_price_cny!, 0)
  return `估算 ¥${total.toFixed(4)}${known.length !== usage.length || usage.some(item => item.cost_estimate_complete === false) ? '（不完整）' : ''}`
}
function valueText(value: unknown) { return Array.isArray(value) ? value.join('、') : String(value ?? '未知') }
function useSuggestion() {
  if (!props.suggestion || busy.value) return
  reason.value = props.suggestion.reason || ''
  if (props.suggestion.field_group && ['action', 'subject', 'scene', 'spatial', 'expression'].includes(props.suggestion.field_group)) fieldGroup.value = props.suggestion.field_group as PerceptionFieldGroup
}
function patches(revision: PerceptionRevision) { return revision.changes.revisions?.flatMap(item => item.patches || []) || [] }

async function readRecords(scope: number, jobId: string, shotId: string, selectOperation = true) {
  const signal = controller.signal
  const [saved, history] = await Promise.allSettled([getPerception(jobId, shotId, signal), getPerceptionHistory(jobId, shotId, signal)])
  if (!current(scope)) return
  if (saved.status === 'fulfilled') {
    status.value = saved.value
    if (selectOperation) operation.value = saved.value.operations.find(item => isActive(item)) || saved.value.operations[0] || null
  } else error.value = message(saved.reason)
  if (history.status === 'fulfilled') revisions.value = history.value.revisions
}
function schedulePoll(scope: number, jobId: string, shotId: string) {
  window.clearTimeout(timer)
  if (!current(scope) || !isActive(operation.value) || operation.value?.cancel_requested) return
  timer = window.setTimeout(() => { void poll(scope, jobId, shotId) }, 1500)
}
async function finish(scope: number, jobId: string, shotId: string) {
  const completed = operation.value
  await readRecords(scope, jobId, shotId, false)
  if (current(scope) && completed?.result.revision !== undefined) emit('updated', jobId)
}
async function poll(scope: number, jobId: string, shotId: string) {
  const id = operation.value?.id
  if (!id || !current(scope)) return
  try {
    const result = await getPerceptionOperation(jobId, shotId, id, controller.signal)
    if (!current(scope)) return
    operation.value = result
    if (isActive(result)) schedulePoll(scope, jobId, shotId)
    else await finish(scope, jobId, shotId)
  } catch (caught) { if (current(scope)) error.value = message(caught) }
}
async function start(mode: 'recover' | 'review', stage?: PerceptionStageName) {
  const jobId = props.jobId
  if (!jobId || !canWrite.value || !status.value || (mode === 'review' && !reason.value.trim())
      || (mode === 'recover' && (!stage || !status.value.recoverable_stages.includes(stage)))) return
  const scope = generation, shotId = props.shotId
  submitting.value = true
  error.value = ''
  const base = { idempotency_key: crypto.randomUUID(), expected_revision: status.value.revision }
  try {
    const result = await createPerceptionOperation(jobId, shotId, mode === 'recover'
      ? { ...base, mode, stages: [stage!] }
      : { ...base, mode, field_group: fieldGroup.value, reason: reason.value.trim() }, controller.signal)
    if (!current(scope)) return
    operation.value = result
    if (isActive(result)) schedulePoll(scope, jobId, shotId)
    else await finish(scope, jobId, shotId)
  } catch (caught) { if (current(scope)) error.value = message(caught) }
  finally { if (current(scope)) submitting.value = false }
}
async function cancel() {
  const jobId = props.jobId, shotId = props.shotId, id = operation.value?.id
  if (!jobId || !id || !isActive(operation.value) || operation.value?.cancel_requested) return
  stopRequests()
  const scope = generation
  submitting.value = true
  error.value = ''
  try {
    const result = await cancelPerceptionOperation(jobId, shotId, id, controller.signal)
    if (current(scope)) operation.value = result
  } catch (caught) { if (current(scope)) error.value = message(caught) }
  finally { if (current(scope)) submitting.value = false }
}
async function load() {
  keepDraft()
  loadedDraftKey = null
  stopRequests()
  const scope = generation, jobId = props.jobId, shotId = props.shotId
  status.value = null
  operation.value = null
  revisions.value = []
  reason.value = ''
  fieldGroup.value = 'action'
  if (props.suggestion?.reason) reason.value = props.suggestion.reason
  if (props.suggestion?.field_group && ['action', 'subject', 'scene', 'spatial', 'expression'].includes(props.suggestion.field_group)) fieldGroup.value = props.suggestion.field_group as PerceptionFieldGroup
  if (props.projectId !== undefined && jobId) {
    const key = perceptionDraftKey(props.projectId, props.taskKey || jobId, jobId, shotId)
    const draft = readPerceptionDraft(key)
    if (draft) { reason.value = draft.reason; fieldGroup.value = draft.fieldGroup }
    loadedDraftKey = key
  }
  error.value = ''
  submitting.value = false
  loading.value = !!jobId
  if (!jobId) return
  await readRecords(scope, jobId, shotId)
  if (!current(scope)) return
  loading.value = false
  schedulePoll(scope, jobId, shotId)
}
watch([reason, fieldGroup], keepDraft, { flush: 'sync' })
watch(() => [props.projectId, props.taskKey, props.jobId, props.shotId], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { keepDraft(); stopRequests() })
</script>

<template>
  <section class="inspector-section perception-review" aria-label="视觉分析记录" data-testid="perception-panel">
    <div class="section-title">视觉分析记录 <span v-if="status">REV {{ status.revision }}</span></div>
    <button v-if="suggestion" type="button" class="outline-button" :disabled="busy" @click="useSuggestion">使用 Agent 的复核建议填写表单</button>
    <p v-if="loading" class="review-note">读取已保存记录…</p>
    <p v-else-if="!jobId || !status?.record_available" class="review-note" data-testid="perception-no-record">此镜头没有可恢复的分析记录。</p>
    <template v-if="status?.record_available">
      <div v-for="stage in stageNames" :key="stage" class="stage-record" :data-testid="`perception-stage-${stage}`">
        <div class="stage-line"><strong>{{ stageLabels[stage] }}</strong><span>{{ status.stages[stage] ? stageStates[status.stages[stage]!.state] : '未记录' }}</span></div>
        <p v-if="status.stages[stage]" class="review-note">{{ status.stages[stage]!.input_frames.length }} 张证据帧 · {{ cost(status.stages[stage]!.usage) }}</p>
        <p v-if="status.stages[stage]?.error" class="review-error">{{ stageError(status.stages[stage]!.error) }}</p>
        <button v-if="status.recoverable_stages.includes(stage)" type="button" class="outline-button" :data-testid="`perception-recover-${stage}`" :disabled="!canWrite" @click="start('recover', stage)">仅恢复{{ stageLabels[stage] }}（云端调用）</button>
      </div>
      <p v-if="status.read_only" class="review-note">当前任务尚未停止，暂不能恢复或复核。</p>
      <p class="review-note">恢复或复核会调用云端 API，可能产生费用。</p>
    </template>
    <div class="review-form">
      <label>复核内容<select v-model="fieldGroup" data-testid="perception-field-group" :disabled="!canWrite"><option v-for="(label, key) in fieldLabels" :key="key" :value="key">{{ label }}</option></select></label>
      <label>指出需要核对的问题<textarea v-model="reason" data-testid="perception-reason" :disabled="!canWrite" maxlength="1000" rows="2" placeholder="例如：杯子仍在手中，动作摘要却写成已放下" /></label>
      <button type="button" class="outline-button" data-testid="perception-review-start" :disabled="!canWrite || !reason.trim()" @click="start('review')">复核所选事实（云端调用）</button>
    </div>
    <div v-if="operation" class="operation-state" data-testid="perception-operation">
      <strong>{{ operationStates[operation.status] }}</strong>
      <p class="review-note">{{ cost(operation.usage) }}</p>
      <p v-if="operation.error || operation.result.error || operation.result.warning" class="review-error">{{ operation.error || operation.result.error || operation.result.warning }}</p>
      <p v-if="operation.cancel_requested" class="review-note">取消已提交，已发出调用的费用仍可能产生。</p>
      <button v-if="isActive(operation)" type="button" class="outline-button" data-testid="perception-cancel" :disabled="submitting || operation.cancel_requested" @click="cancel">取消本次操作</button>
    </div>
    <p v-if="error" class="review-error" role="alert">{{ error }}</p>
    <button v-if="jobId" type="button" class="outline-button" data-testid="perception-refresh" :disabled="submitting || loading" @click="load">刷新记录（不调用模型）</button>
    <details v-if="operations.length || revisions.length" class="revision-history" data-testid="perception-history">
      <summary>修订与调用记录（{{ revisions.length }} 条审计）</summary>
      <div v-for="revision in revisions" :key="revision.version" class="revision-row">
        <strong>审计 {{ revision.version }} · {{ revision.changes.changed ? '事实已更新' : '事实未变，记录已更新' }}</strong>
        <p class="review-note">{{ revision.changes.field_group ? fieldLabels[revision.changes.field_group] : (revision.changes.stages || []).map(stage => stageLabels[stage]).join('、') }}</p>
        <p v-for="patch in patches(revision)" :key="patch.field" class="revision-difference"><span>此前：{{ valueText(patch.old.value) }}</span><span>现在：{{ valueText(patch.new.value) }}</span></p>
        <p v-for="(audit, index) in revision.changes.revisions || []" :key="index" class="review-note">{{ audit.rationale || audit.error || audit.reason }}</p>
      </div>
      <div v-for="item in operations" :key="item.id" class="revision-row"><strong>{{ item.mode === 'recover' ? '阶段恢复' : '事实复核' }} · {{ operationStates[item.status] }}</strong><p class="review-note">{{ cost(item.usage) }}</p><p v-if="item.request.reason" class="review-note">{{ item.request.reason }}</p></div>
    </details>
  </section>
</template>

<style scoped>
.perception-review{font-size:12px}.review-note{color:var(--text-muted,#8b8b92);font-size:11px;line-height:1.6;margin:6px 0}.stage-record{padding:10px 0;border-bottom:1px solid var(--border,#333338)}.stage-line{display:flex;justify-content:space-between;gap:12px}.stage-line span{color:var(--text-muted,#8b8b92)}.review-error{color:var(--warning,#bd8263);font-size:11px;line-height:1.6;overflow-wrap:anywhere}.review-form{display:grid;gap:10px;margin-top:12px}.review-form label{display:grid;gap:6px;color:var(--text-muted,#8b8b92)}.review-form select,.review-form textarea{width:100%;box-sizing:border-box;border:1px solid var(--border,#333338);background:transparent;color:inherit;border-radius:6px;padding:7px;font:inherit}.review-form textarea{resize:vertical}.perception-review .outline-button{width:100%;margin:8px 0 0}.perception-review button:disabled{opacity:.45;cursor:default}.operation-state{margin-top:14px}.revision-history{margin-top:14px}.revision-history summary{cursor:pointer}.revision-row{padding:10px 0;border-bottom:1px solid var(--border,#333338)}.revision-difference{display:grid;gap:5px;line-height:1.6;overflow-wrap:anywhere}
</style>
