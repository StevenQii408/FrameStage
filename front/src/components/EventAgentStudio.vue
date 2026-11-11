<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Copy, X } from 'lucide-vue-next'
import { cancelEventAgentRun, createEventAgentRun, getEventAgentResult, getEventAgentRun, listEventAgentRuns, type EventAgentResult, type EventAgentRun } from '../api/eventAgent'
import { apiUrl } from '../api/client'

const props = defineProps<{ jobId?: string | null; jobStatus: string; inputType: 'video' | 'script'; jobLabel?: string; sourceShotIds?: string[] }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement | null>(null)
const runs = ref<EventAgentRun[]>([])
const run = ref<EventAgentRun | null>(null)
const result = ref<EventAgentResult | null>(null)
const loading = ref(false)
const mutation = ref<'start' | 'cancel' | ''>('')
const error = ref('')
const message = ref('')
const eligible = computed(() => !!props.jobId && props.inputType === 'video' && props.jobStatus === 'completed')
const active = (value: EventAgentRun | null) => !!value && ['queued', 'running'].includes(value.status)
const running = computed(() => active(run.value))
const hasActiveRun = computed(() => runs.value.some(active))
const canStart = computed(() => eligible.value && !loading.value && !mutation.value && !hasActiveRun.value)
const statuses: Record<string, string> = { queued: '排队中', running: '分析中', ready: '已完成', completed: '已完成', degraded: '部分完成', cancelled: '已取消', interrupted: '已中断', failed: '失败' }
const progress = computed(() => typeof run.value?.progress === 'number' ? Math.min(100, Math.max(0, run.value.progress)) : null)
const usage = computed(() => run.value?.usage_summary || result.value?.usage_summary)
const audits = computed(() => result.value?.audit || run.value?.audit || run.value?.checkpoints || [])
const sourceIds = computed(() => [...new Set(props.sourceShotIds || result.value?.windows.flatMap(window => window.source_shot_ids) || [])])
const coverage = computed(() => {
  const ownership = new Map(result.value?.source_coverage.map(item => [item.source_shot_id, item.event_id]) || [])
  return sourceIds.value.map(id => {
    const eventId = ownership.get(id)
    const event = result.value?.events.find(item => item.id === eventId)
    return { id, eventId, event, decision: event?.presentation.find(item => item.source_shot_id === id) }
  })
})
const coveredCount = computed(() => coverage.value.filter(item => item.eventId).length)
const presentationLabels = { preserve: '保留', integrate: '整合', omit: '省略' }
const transitionLabels = { initial: '开始', continuation: '连续', cut: '切镜' }
const toolLabels: Record<string, string> = { inspect_boundary: '检查切镜边界', sample_interval_frames: '区间补帧', compare_frames: '对比画面', crop_frame: '查看局部', get_neighbor_context: '读取相邻上下文' }
function recorded(value?: number | null) { return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未记录' }
function seconds(value: number) { return `${value.toFixed(2)}s` }
function evidenceUrl(frameId: string) { return apiUrl(`/jobs/${encodeURIComponent(props.jobId || '')}/frames/${encodeURIComponent(frameId)}`) }

let generation = 0
let disposed = false
let timer: ReturnType<typeof setTimeout> | null = null
let controller = new AbortController()
let previousFocus: HTMLElement | null = null
function stopPolling() { if (timer !== null) clearTimeout(timer); timer = null }
function invalidate() { ++generation; stopPolling(); controller.abort(); controller = new AbortController() }
function current(token: number, jobId: string, runId?: string) {
  return !disposed && token === generation && props.jobId === jobId && (!runId || run.value?.run_id === runId)
}
function showError(cause: unknown, token: number, jobId: string) {
  if (current(token, jobId) && !(cause instanceof Error && cause.name === 'AbortError')) error.value = cause instanceof Error ? cause.message : String(cause)
}
function updateRun(data: EventAgentRun) {
  run.value = data
  runs.value = [data, ...runs.value.filter(item => item.run_id !== data.run_id)]
  if (!active(data)) stopPolling()
}
async function readResult(data: EventAgentRun, token: number, jobId: string) {
  if (!data.has_result || active(data)) return
  const cached = await getEventAgentResult(jobId, data.run_id, controller.signal)
  if (current(token, jobId, data.run_id) && cached.job_id === jobId && cached.run_id === data.run_id) result.value = cached
}
function schedulePoll(token: number, jobId: string, runId: string) {
  stopPolling()
  if (!current(token, jobId, runId) || !running.value) return
  timer = setTimeout(() => { timer = null; void poll(token, jobId, runId) }, 2000)
}
async function poll(token: number, jobId: string, runId: string) {
  if (!current(token, jobId, runId)) return
  try {
    const data = await getEventAgentRun(jobId, runId, controller.signal)
    if (!current(token, jobId, runId) || data.job_id !== jobId || data.run_id !== runId) return
    error.value = ''; updateRun(data); await readResult(data, token, jobId)
  } catch (cause) { showError(cause, token, jobId) }
  finally { schedulePoll(token, jobId, runId) }
}
async function selectRun(runId: string) {
  const jobId = props.jobId
  if (!jobId || mutation.value) return
  invalidate(); const token = generation
  result.value = null; error.value = ''; message.value = ''; loading.value = true
  run.value = runs.value.find(item => item.run_id === runId) || null
  try {
    const data = await getEventAgentRun(jobId, runId, controller.signal)
    if (!current(token, jobId) || data.job_id !== jobId || data.run_id !== runId) return
    updateRun(data); await readResult(data, token, jobId); schedulePoll(token, jobId, runId)
  } catch (cause) { showError(cause, token, jobId) }
  finally { if (current(token, jobId)) loading.value = false }
}
async function loadRuns() {
  invalidate(); const token = generation; const jobId = props.jobId
  run.value = null; result.value = null; runs.value = []; error.value = ''; message.value = ''; mutation.value = ''
  loading.value = !!jobId
  if (!jobId) return
  try {
    const data = await listEventAgentRuns(jobId, controller.signal)
    if (!current(token, jobId) || data.job_id !== jobId) return
    runs.value = [...data.runs].sort((a, b) => b.created_at.localeCompare(a.created_at))
    const initial = runs.value.find(active) || runs.value[0]
    if (initial) await selectRun(initial.run_id)
  } catch (cause) { showError(cause, token, jobId) }
  finally { if (current(token, jobId)) loading.value = false }
}
async function start() {
  const jobId = props.jobId
  if (!jobId || !canStart.value) return
  invalidate(); const token = generation
  mutation.value = 'start'; error.value = ''; message.value = ''; result.value = null
  try {
    const data = await createEventAgentRun(jobId, controller.signal)
    if (!current(token, jobId) || data.job_id !== jobId) return
    updateRun(data); await readResult(data, token, jobId); schedulePoll(token, jobId, data.run_id)
  } catch (cause) { showError(cause, token, jobId) }
  finally { if (current(token, jobId)) mutation.value = '' }
}
async function cancel() {
  const jobId = props.jobId; const selected = run.value
  if (!jobId || !selected || !active(selected) || mutation.value) return
  invalidate(); const token = generation
  mutation.value = 'cancel'; error.value = ''; message.value = ''
  try {
    const data = await cancelEventAgentRun(jobId, selected.run_id, controller.signal)
    if (!current(token, jobId, selected.run_id) || data.job_id !== jobId || data.run_id !== selected.run_id) return
    updateRun(data); await readResult(data, token, jobId); schedulePoll(token, jobId, data.run_id)
  } catch (cause) { showError(cause, token, jobId); schedulePoll(token, jobId, selected.run_id) }
  finally { if (current(token, jobId)) mutation.value = '' }
}
async function copy(text: string) {
  const token = generation
  try {
    await navigator.clipboard.writeText(text)
    if (token === generation && !disposed) message.value = '已复制完整提示词'
  } catch { if (token === generation && !disposed) error.value = '复制失败，请展开提示词后手动复制' }
}
function close() { invalidate(); emit('close') }
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>('button:not([disabled]), select:not([disabled]), summary, [tabindex="0"]')]
  const first = focusable[0], last = focusable[focusable.length - 1]
  if (!first) { event.preventDefault(); panel.value.focus(); return }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}
watch(() => [props.jobId, props.inputType, props.jobStatus], () => { void loadRuns() }, { immediate: true })
onMounted(async () => { previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null; await nextTick(); if (!disposed) panel.value?.focus() })
onBeforeUnmount(() => { disposed = true; invalidate(); if (previousFocus?.isConnected) previousFocus.focus() })
</script>

<template>
  <div class="event-agent-backdrop" @click.self="close">
    <section ref="panel" class="event-agent-studio" role="dialog" aria-modal="true" aria-labelledby="event-agent-title" tabindex="-1" @keydown="onKeydown">
      <header class="event-agent-header">
        <div><h2 id="event-agent-title">事件分镜（实验）</h2><p>{{ jobLabel || jobId || '未选择视频任务' }}</p></div>
        <button type="button" aria-label="关闭事件分镜" data-testid="event-agent-close" @click="close"><X :size="18" /></button>
      </header>
      <p class="event-agent-notice">点击“分析事件分镜”会使用已配置的云端 API，并可能产生费用。实验结果单独保存，原任务的镜头、提示词和画布位置保持不变。</p>
      <div class="event-agent-actions">
        <button type="button" data-testid="event-agent-start" :disabled="!canStart" @click="start">{{ mutation === 'start' ? '正在提交…' : '分析事件分镜' }}</button>
        <button v-if="running" type="button" data-testid="event-agent-cancel" :disabled="!!mutation || run?.cancel_requested" @click="cancel">{{ mutation === 'cancel' || run?.cancel_requested ? '取消请求已提交' : '取消本次分析' }}</button>
        <button type="button" data-testid="event-agent-refresh" :disabled="loading || !!mutation" @click="loadRuns">刷新运行记录</button>
        <span v-if="!eligible">请选择已完成的视频任务后开始分析。</span>
        <span v-else-if="hasActiveRun && !running">该任务已有进行中的实验，请在运行记录中选择查看。</span>
        <span v-else-if="loading">正在读取保存的运行记录…</span>
      </div>
      <label v-if="runs.length" class="event-agent-history">运行记录
        <select data-testid="event-agent-run-select" :value="run?.run_id || ''" :disabled="!!mutation" @change="selectRun(($event.target as HTMLSelectElement).value)">
          <option v-for="item in runs" :key="item.run_id" :value="item.run_id">{{ item.created_at }} · {{ statuses[item.status] || item.status }} · {{ item.run_id.slice(0, 8) }}</option>
        </select>
      </label>
      <p v-if="!loading && !run && !error" class="event-agent-empty">还没有事件分镜实验。打开面板只读取记录，点击分析后才会开始云端工作。</p>
      <p v-if="error" class="event-agent-error" role="alert">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <section v-if="run" class="event-agent-run" data-testid="event-agent-status" aria-live="polite">
        <div><strong>{{ statuses[run.status] || run.status }}</strong><span>阶段：{{ run.stage }}</span><span v-if="progress !== null">{{ progress }}%</span></div>
        <progress v-if="progress !== null" :value="progress" max="100" aria-label="事件分镜进度" />
        <p v-if="run.provider || run.model">{{ run.provider }} · {{ run.model }}</p>
        <dl class="event-agent-metrics" data-testid="event-agent-usage">
          <div><dt>完成窗口</dt><dd>{{ recorded(run.window_completed ?? run.checkpoint_count ?? usage?.windows) }}<span v-if="typeof run.window_total === 'number'"> / {{ run.window_total }}</span></dd></div>
          <div><dt>模型调用</dt><dd>{{ recorded(usage?.model_calls) }}</dd></div>
          <div><dt>工具调用</dt><dd>{{ recorded(usage?.tool_calls) }}</dd></div>
          <div><dt>附加取证帧</dt><dd>{{ recorded(usage?.extra_frames) }}</dd></div>
          <div><dt>费用估算</dt><dd>{{ typeof usage?.estimated_list_price_cny === 'number' ? `¥${usage.estimated_list_price_cny.toFixed(4)}${usage.cost_estimate_complete ? '' : '（不完整）'}` : '未提供' }}</dd></div>
        </dl>
        <p v-if="run.error" class="event-agent-error">{{ run.error }}</p>
        <p v-if="run.status === 'degraded'" class="event-agent-warning">本次实验部分完成，请检查警告与来源覆盖后使用。</p>
        <p v-if="run.status === 'interrupted'">本次实验已中断。保存的记录可供检查，重新分析需要再次点击分析按钮。</p>
        <ul v-if="run.warnings?.length" class="event-agent-warning"><li v-for="(warning, index) in run.warnings" :key="index">{{ warning }}</li></ul>
        <details v-if="audits.length" data-testid="event-agent-window-audit">
          <summary>查看窗口与取证记录（{{ audits.length }}）</summary>
          <article v-for="audit in audits" :key="audit.window_id" class="event-agent-window">
            <strong>{{ audit.window_id }} · {{ audit.status === 'ready' ? '完成' : audit.status === 'fallback' ? '证据不足，保留原镜头' : '已取消' }}</strong>
            <p>归属镜头：{{ audit.source_shot_ids.join('、') }}<span v-if="audit.context_shot_ids?.length"> · 只读上下文：{{ audit.context_shot_ids.join('、') }}</span></p>
            <p>模型 {{ recorded(audit.usage?.model_calls) }} 次 · 工具 {{ recorded(audit.usage?.tool_calls) }} 次</p>
            <ul v-if="audit.tool_results?.length"><li v-for="(tool, index) in audit.tool_results" :key="index">{{ toolLabels[tool.name] || tool.name }} · {{ tool.status }}<span v-if="tool.error"> · {{ tool.error }}</span></li></ul>
            <ul v-if="audit.validation_errors?.length || audit.warnings?.length" class="event-agent-warning"><li v-for="(warning, index) in [...audit.validation_errors, ...audit.warnings]" :key="index">{{ warning }}</li></ul>
          </article>
        </details>
      </section>
      <section v-if="result" class="event-agent-results" data-testid="event-agent-result">
        <h3>实验结果 · {{ result.events.length }} 个事件 / {{ result.generation_clips.length }} 个生成片段</h3>
        <p>来源归属覆盖 {{ coveredCount }} / {{ sourceIds.length }} 个原片镜头。归属与摄影呈现分别记录；省略镜头仍需保留归属和原因。</p>
        <ul v-if="result.warnings.length" class="event-agent-warning"><li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li></ul>
        <details class="event-agent-coverage" data-testid="event-agent-coverage">
          <summary>检查来源归属与呈现决策</summary>
          <div class="event-agent-table"><table><thead><tr><th>来源镜头</th><th>归属事件</th><th>呈现</th><th>原因</th></tr></thead><tbody>
            <tr v-for="item in coverage" :key="item.id"><td>{{ item.id }}</td><td>{{ item.event?.title || item.eventId || '未覆盖' }}</td><td>{{ item.decision ? presentationLabels[item.decision.disposition] : '未记录' }}</td><td>{{ item.decision?.reason || '未记录' }}</td></tr>
          </tbody></table></div>
        </details>
        <article v-for="clip in result.generation_clips" :key="clip.id" class="event-agent-clip" data-testid="event-agent-clip">
          <header><h3>{{ clip.title }}</h3><span class="event-agent-mode" data-testid="event-agent-mode">{{ clip.generation_mode === 'continuous' ? '连续镜头' : '内部切镜' }}</span></header>
          <p>原片 {{ seconds(clip.source_start_time) }}–{{ seconds(clip.source_end_time) }} · 生成时长建议 {{ seconds(clip.generation_duration_seconds) }}</p>
          <p class="event-agent-source-ids">来源镜头：{{ clip.source_shot_ids.join('、') }}</p>
          <ul v-if="clip.warnings.length" class="event-agent-warning"><li v-for="(warning, index) in clip.warnings" :key="index">{{ warning }}</li></ul>
          <details class="event-agent-facts"><summary>查看事件事实与呈现原因</summary>
            <template v-for="event in result.events.filter(item => item.id === clip.event_id)" :key="event.id">
              <p>{{ event.goal }}</p><p>初始状态：{{ event.initial_state }}<br />结束状态：{{ event.final_state }}</p>
              <p v-if="event.status === 'fallback'" class="event-agent-warning">该事件使用保守保留方案，请检查取证记录。</p>
              <ul><li v-for="fact in event.causal_facts" :key="fact.id">{{ fact.text }}<span v-if="fact.critical">（关键事实）</span><ul><li v-for="(evidence, index) in fact.evidence" :key="index"><a :href="evidenceUrl(evidence.crop_artifact_id || evidence.frame_id)" target="_blank" rel="noopener noreferrer">{{ evidence.frame_id }} · {{ seconds(evidence.timestamp) }}</a> · {{ evidence.description }}</li></ul></li></ul>
            </template>
            <ul><li v-for="decision in clip.presentation" :key="decision.source_shot_id">{{ decision.source_shot_id }} · {{ presentationLabels[decision.disposition] }}：{{ decision.reason }}</li></ul>
            <div class="event-agent-table"><table><thead><tr><th>生成时间</th><th>衔接</th><th>动作</th><th>来源镜头</th></tr></thead><tbody><tr v-for="stage in clip.timeline" :key="stage.id"><td>{{ seconds(stage.generation_start_seconds) }}–{{ seconds(stage.generation_end_seconds) }}</td><td>{{ transitionLabels[stage.transition] }}</td><td>{{ stage.action }}</td><td>{{ stage.source_shot_ids.join('、') }}</td></tr></tbody></table></div>
          </details>
          <div v-for="(prompt, index) in [clip.first_frame_prompt, clip.seedance_prompt]" :key="prompt.id" class="event-agent-prompt" data-testid="event-agent-prompt">
            <div><strong>{{ index === 0 ? '首帧提示词' : 'Seedance 视频提示词' }}</strong><button type="button" data-testid="event-agent-copy" :aria-label="index === 0 ? '复制首帧提示词' : '复制 Seedance 视频提示词'" @click="copy(prompt.positive_prompt)"><Copy :size="14" />复制</button></div>
            <details><summary>展开完整提示词</summary><pre>{{ prompt.positive_prompt }}</pre><template v-if="prompt.negative_prompt"><strong>负面约束</strong><pre>{{ prompt.negative_prompt }}</pre></template></details>
          </div>
        </article>
      </section>
    </section>
  </div>
</template>

<style scoped>
.event-agent-backdrop{position:fixed;inset:0;z-index:80;background:#070b14b8;display:grid;place-items:center;padding:24px}.event-agent-studio{width:min(100%,1040px);max-height:90dvh;overflow:auto;box-sizing:border-box;padding:24px;border:1px solid #7895b544;border-radius:18px;background:#151d29;color:#e6edf5;box-shadow:0 24px 80px #0005;outline:none;font-size:13px;line-height:1.7}.event-agent-header{display:flex;justify-content:space-between;align-items:start;gap:18px}.event-agent-header h2{font-size:20px;margin:0}.event-agent-header p{color:#a3b5c9;margin:4px 0;overflow-wrap:anywhere}.event-agent-studio button{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 12px;border:1px solid #8dabc355;border-radius:9px;background:#729bcf1a;color:inherit;cursor:pointer;font:inherit}.event-agent-studio button:disabled{opacity:.45;cursor:default}.event-agent-studio button:focus-visible{outline:2px solid #a7c9ef;outline-offset:3px}.event-agent-studio summary:focus-visible{outline:2px solid #a7c9ef;outline-offset:3px}.event-agent-studio select:focus-visible{outline:2px solid #a7c9ef;outline-offset:3px}.event-agent-notice{padding:12px 14px;background:#6393b412;border-radius:10px;color:#b7c8d9}.event-agent-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:18px 0}.event-agent-actions span{color:#a3b5c9}.event-agent-history{display:flex;align-items:center;gap:10px;margin:18px 0}.event-agent-history select{min-width:0;max-width:100%;flex:1;background:#111924;color:inherit;border:1px solid #8dabc355;border-radius:8px;padding:8px;font:inherit}.event-agent-empty{color:#a3b5c9}.event-agent-error{color:#efb0a0}.event-agent-warning{color:#e1bf82}.event-agent-run{padding:14px;border:1px solid #8dabc333;border-radius:12px}.event-agent-run>div{display:flex;gap:16px;flex-wrap:wrap}.event-agent-run progress{width:100%;height:7px;accent-color:#a8bff0}.event-agent-run p{margin:8px 0}.event-agent-results{margin-top:20px}.event-agent-studio ul{padding-left:20px}.event-agent-studio li{overflow-wrap:anywhere}@media(max-width:600px){.event-agent-backdrop{padding:10px}.event-agent-studio{padding:16px}.event-agent-history{align-items:start;flex-direction:column}.event-agent-history select{width:100%}}
</style>

<style scoped>
.event-agent-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:12px;margin:14px 0}.event-agent-metrics dt{color:#a3b5c9}.event-agent-metrics dd{margin:2px 0;font-variant-numeric:tabular-nums}.event-agent-studio summary{cursor:pointer}.event-agent-window{padding:12px 0;border-top:1px solid #8dabc333}.event-agent-results h3{font-size:15px}.event-agent-coverage{padding:12px;border:1px solid #8dabc333;border-radius:10px}.event-agent-table{overflow-x:auto}.event-agent-table table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}.event-agent-table td{border-bottom:1px solid #8dabc333;padding:8px 12px 8px 0;vertical-align:top;overflow-wrap:anywhere}.event-agent-table th{border-bottom:1px solid #8dabc333;padding:8px 12px 8px 0;vertical-align:top;overflow-wrap:anywhere}.event-agent-clip{margin-top:20px;padding:16px;border:1px solid #8dabc333;border-radius:12px}.event-agent-clip header{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.event-agent-clip h3{margin:0;flex:1}.event-agent-mode{padding:3px 8px;border:1px solid #a8bff044;border-radius:8px;background:#a8bff010;white-space:nowrap}.event-agent-source-ids{color:#a3b5c9;font-size:12px}.event-agent-facts{margin:14px 0}.event-agent-studio a{color:inherit;text-decoration:underline;text-underline-offset:3px}.event-agent-prompt{margin-top:14px;padding:12px;background:#07121c44;border-radius:10px}.event-agent-prompt>div{display:flex;align-items:center;justify-content:space-between;gap:12px}.event-agent-prompt details{margin-top:8px}.event-agent-prompt pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.9;user-select:text}
</style>
