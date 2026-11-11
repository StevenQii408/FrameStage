<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { X, Copy, Sparkles } from 'lucide-vue-next'
import { analyzeProjectStyle, analyzeStyleImage, applyDirectStyle, editDirectStyle, getProjectDirectStyle, getStyleAnalysis, styleDiagnosticsUrl, styleEventsUrl, styleImageUrl, type StyleAnalysis, type StyleDiagnosticDecision, type StyleRecipeSource, type StyleRecommendation } from '../api/styleDirect'

const props = defineProps<{ jobId?: string; imageBlob?: Blob | null; imageAssetId?: string; targets?: { id: string; label: string }[] }>()
const emit = defineEmits<{ close: []; applied: [jobId: string] }>()
const analysis = ref<StyleAnalysis | null>(null)
const panel = ref<HTMLElement | null>(null)
const busy = ref(false)
const error = ref('')
const message = ref('')
const text = ref('')
const selectedRecipe = ref<StyleRecipeSource | undefined>()
const target = ref(props.jobId || '')
const now = ref(Date.now())
const labels: Record<string, string> = { queued: '排队中', observing: '提取视觉证据', acquiring: '读取原图局部证据', verifying: '核验新增证据', retrieving: '召回风格候选', interpreting: '转译专业画风', correcting: '纠正输出结构一次', rendering: '校验与渲染', completed: '画风已生成', partial: '已保留可用特征', failed: '分析未完成' }
const profile = computed(() => analysis.value?.result.profile)
const diagnostics = computed(() => analysis.value?.result.diagnostics)
const recommendations = computed(() => {
  const seen = new Set<string>()
  return (analysis.value?.recommendations || analysis.value?.result.recommendations || []).filter(item => {
    if (!item.recipe_id || !item.library_prompt?.trim() || !item.library_source_hash || seen.has(item.recipe_id)) return false
    seen.add(item.recipe_id)
    return true
  }).slice(0, 3)
})
const derivedRecommendations = computed(() => analysis.value?.recommendations_derived || analysis.value?.result.recommendations_derived)
const draftChanged = computed(() => !!selectedRecipe.value || text.value !== profile.value?.rendered_prompt)
const qualityState = computed(() => analysis.value?.result.quality_state || diagnostics.value?.quality?.state)
const qualityLabels: Record<string, string> = { valid: '结构有效', available: '观察已保留', usable: '特征可用', completed: '转译完成', resolved: '视觉特征已确定', partial: '部分确定', unknown: '未知', failed: '未完成', supported: '有证据支持' }
const selectedAnchorId = computed(() => diagnostics.value?.quality?.selected_reference_id || profile.value?.reference?.id || diagnostics.value?.quality?.selected_anchor_id || profile.value?.anchor?.id)
const groupLabels: Record<string, string> = { tone: '色彩', color: '色彩', light: '照明', lighting: '照明', texture: '纹理', medium: '媒介', form: '造型', motion: '动态', color_tone: '色彩', lighting_shading: '照明', texture_material: '纹理', medium_rendering: '媒介', form_language: '造型', motion_signature: '动态' }
const retrievalLabels: Record<string, string> = { 'signature-combination': '完整特征组合', 'partial-signature-review': '部分特征组，仅供复核', 'medium-only-review': '仅媒介符合，仅供复核' }
const stageStatuses: Record<string, string> = { completed: '完成', failed: '失败', running: '进行中', partial: '部分完成', skipped: '跳过', pending: '待执行', success: '完成', error: '失败' }
function recorded(value: number | null | undefined) { return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未记录' }
function seconds(value: number | null | undefined) { return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)} 秒` : '未记录' }
function groups(values: string[]) { return values.map(id => groupLabels[id] || id).join('、') || '无' }
function decisionText(decision: StyleDiagnosticDecision) {
  if (decision.reason) return decision.reason
  if (decision.repair) return ({ 'wrapped-array': '已修复数组包装', 'visible-clauses-preserved': '已保留有证据的可见描述' } as Record<string, string>)[decision.repair] || decision.repair
  if (decision.verified_feature_groups) return `特征组引用已核验：${Object.entries(decision.verified_feature_groups).map(([id, refs]) => `${groupLabels[id] || id}（${refs.join('、')}）`).join('；')}`
  return '该审计项未记录说明'
}
function candidateState(id: string, eligible: boolean) {
  if (id === profile.value?.reference?.id) return '已选择 · 原图比较支持的视觉参考（非来源鉴定）'
  if (id === selectedAnchorId.value) return eligible ? '已选择 · 文字特征支持' : '已选择 · 语义引用支持（非来源鉴定）'
  return eligible ? '文字组合成立 · 未选为锚点' : '文字组合未齐 · 可进行语义比较'
}
const running = computed(() => !!analysis.value && !['completed', 'partial', 'failed'].includes(analysis.value.status))
const elapsed = computed(() => analysis.value ? Math.max(0, Math.round((now.value - Date.parse(analysis.value.created_at)) / 1000)) : 0)
const storageKey = computed(() => props.imageAssetId ? `frameforge-style-analysis:${props.imageAssetId}` : '')
let stream: EventSource | null = null
let timer: ReturnType<typeof setInterval> | null = null
let generation = 0
let polling = false
let refreshPending = false
function stop() { stream?.close(); stream = null; if (timer) clearInterval(timer); timer = null; polling = false }
function setAnalysis(data: StyleAnalysis, resetDraft = false) {
  const preserveDraft = !resetDraft && analysis.value?.analysis_id === data.analysis_id && draftChanged.value
  analysis.value = data
  if (!preserveDraft) { text.value = data.result.profile?.rendered_prompt || ''; selectedRecipe.value = undefined }
  if (storageKey.value) { try { localStorage.setItem(storageKey.value, data.analysis_id) } catch { /* optional persistence */ } }
  if (!running.value) stop()
}
async function refresh(id: string, token: number) {
  if (refreshPending) return
  refreshPending = true
  try { const data = await getStyleAnalysis(id); if (token === generation) setAnalysis(data) }
  catch (cause) { if (token === generation) error.value = String(cause) }
  finally { refreshPending = false }
}
function follow(data: StyleAnalysis) {
  stop(); setAnalysis(data, true)
  if (!running.value) return
  const token = generation
  stream = new EventSource(styleEventsUrl(data.analysis_id))
  stream.addEventListener('style.direct.stage', () => { void refresh(data.analysis_id, token) })
  stream.addEventListener('style.direct.finished', () => { void refresh(data.analysis_id, token) })
  stream.onerror = () => { polling = true }
  timer = setInterval(() => { now.value = Date.now(); if (polling) void refresh(data.analysis_id, token) }, 2000)
}
async function start() {
  const token = ++generation
  stop(); busy.value = true; error.value = ''; message.value = ''
  try {
    const data = props.imageBlob ? await analyzeStyleImage(props.imageBlob, !!analysis.value) : props.jobId ? await analyzeProjectStyle(props.jobId) : null
    if (!data) throw new Error('请先导入参考图片')
    if (token === generation) follow(data)
  } catch (cause) { if (token === generation) error.value = String(cause) }
  finally { if (token === generation) busy.value = false }
}
async function save() {
  if (!profile.value || !analysis.value) return
  const token = generation
  busy.value = true; error.value = ''
  try { const data = await saveDraft(analysis.value.analysis_id, profile.value.version); if (token === generation) { setAnalysis(data, true); message.value = '已保存新版本，无需重新分析' } }
  catch (cause) { if (token === generation) error.value = String(cause) }
  finally { if (token === generation) busy.value = false }
}
function saveDraft(id: string, version: number) {
  return selectedRecipe.value ? editDirectStyle(id, text.value, version, { ...selectedRecipe.value }) : editDirectStyle(id, text.value, version)
}
function chooseRecipe(recipe: StyleRecommendation) {
  text.value = recipe.library_prompt
  selectedRecipe.value = { kind: 'library-recipe', recipe_id: recipe.recipe_id, source_hash: recipe.library_source_hash }
  error.value = ''; message.value = '配方已填入草稿；保存或应用后才生效。'
}
function restoreSaved() { text.value = profile.value?.rendered_prompt || ''; selectedRecipe.value = undefined; message.value = '已恢复已保存的画风'; error.value = '' }
watch(text, value => {
  if (selectedRecipe.value && value !== recommendations.value.find(recipe => recipe.recipe_id === selectedRecipe.value?.recipe_id)?.library_prompt) selectedRecipe.value = undefined
})
async function apply() {
  if (!profile.value || !analysis.value || !target.value) return
  const token = generation, job = target.value, id = analysis.value.analysis_id
  busy.value = true; error.value = ''
  try {
    if (draftChanged.value) {
      const data = await saveDraft(id, profile.value.version)
      if (token !== generation) return
      setAnalysis(data, true)
    }
    if (token !== generation) return
    await applyDirectStyle(job, id, profile.value!.version)
    if (token !== generation) return
    message.value = '已应用画风并保存原 Prompt 版本；主体、动作和镜头范围不变'
    emit('applied', job)
  } catch (cause) { if (token === generation) error.value = String(cause) }
  finally { if (token === generation) busy.value = false }
}
async function copy() { try { await navigator.clipboard.writeText(text.value); message.value = '已复制' } catch { error.value = '复制失败，请手动选中文字复制' } }
watch(() => [props.jobId, props.imageAssetId], async () => {
  const token = ++generation
  stop(); busy.value = false; analysis.value = null; text.value = ''; selectedRecipe.value = undefined; error.value = ''; message.value = ''; target.value = props.jobId || ''
  try {
    let id = ''
    if (storageKey.value) { try { id = localStorage.getItem(storageKey.value) || '' } catch { /* optional */ } }
    if (id) { const data = await getStyleAnalysis(id); if (token === generation) follow(data) }
    else if (props.jobId && !props.imageBlob) {
      const data = await getProjectDirectStyle(props.jobId)
      if (token === generation && data.analysis) follow(data.analysis)
    }
  } catch (cause) { if (token === generation) error.value = String(cause) }
}, { immediate: true })
onBeforeUnmount(() => { generation++; stop() })
onMounted(() => panel.value?.focus())
</script>

<template>
  <div class="direct-style-backdrop" @click.self="emit('close')" @keydown.esc="emit('close')">
    <section ref="panel" class="direct-style-studio" role="dialog" aria-modal="true" aria-label="高密度画风工作台" tabindex="-1">
      <header><div><small>QWEN DIRECT · EVIDENCE FIRST</small><h2>{{ imageBlob ? '参考图画风' : '项目画风' }}</h2></div><button aria-label="关闭画风工作台" @click="emit('close')"><X :size="19" /></button></header>
      <p class="direct-style-help">系统先给出画风观察，详细证据另存；不会要求你挑选专业风格。你也可以选择证据支持的库内配方作为创作方向，保存或应用后才生效。视觉参考不代表实际出品方。单图不判断动画节奏。</p>
      <div class="direct-style-actions"><button :disabled="busy || running || (!imageBlob && !jobId)" @click="start"><Sparkles :size="16" />{{ analysis ? '重新分析画风' : '分析画风' }}</button><span>会发送参考画面至云端 Flash 并产生 API 费用；正常 2 次，最多 4 次请求。</span></div>
      <div v-if="analysis" class="direct-style-status" role="status"><b>{{ labels[analysis.stage] || analysis.stage }}</b><span v-if="running">{{ analysis.stage === 'queued' ? '排队已等待' : '本次已等待（含排队）' }} {{ elapsed }} 秒</span><span v-else-if="analysis.result.budget">{{ analysis.result.budget.model_requests }} 次请求 · {{ analysis.result.budget.crop_count }} 个裁剪 · {{ analysis.result.budget.elapsed_seconds }} 秒</span></div>
      <p v-if="error || analysis?.error" class="direct-style-error">{{ error || analysis?.error }}</p><p v-if="message" role="status">{{ message }}</p>
      <p v-if="qualityState" role="status" data-testid="style-quality-state">视觉观察：{{ qualityLabels[qualityState.observation] || qualityState.observation }} · 专业转译：{{ qualityLabels[qualityState.interpretation] || qualityState.interpretation }} · {{ qualityLabels[qualityState.style] || qualityState.style }} · {{ qualityState.reference != null ? `具体视觉参考：${qualityLabels[qualityState.reference]}` : `具体风格锚点：${qualityLabels[qualityState.anchor] || qualityState.anchor}` }}</p>
      <p v-if="qualityState?.descriptor" data-testid="style-family-state">描述：{{ qualityLabels[qualityState.descriptor] || qualityState.descriptor }} · 风格家族：{{ qualityLabels[qualityState.family || 'unknown'] }} · 工作室或艺术家出处未作鉴定。</p>
      <details v-if="analysis" class="direct-style-diagnostics" data-testid="style-diagnostics">
        <summary>分析诊断：耗时、候选与校验</summary>
        <template v-if="diagnostics && diagnostics.status !== 'legacy-unavailable'">
          <p class="direct-style-help">候选顺序不是视觉相似度排名。文字组合未齐仍可根据原图进行语义比较；逐字证据引用也不等于人工确认了风格准确率。</p>
          <dl class="direct-style-diagnostic-metrics">
            <div><dt>总耗时</dt><dd>{{ seconds(diagnostics.timing?.total_seconds) }}</dd></div>
            <div><dt>模型调用耗时</dt><dd>{{ seconds(diagnostics.timing?.model_seconds) }}</dd></div>
            <div><dt>候选召回耗时</dt><dd>{{ seconds(diagnostics.timing?.retrieval_seconds) }}</dd></div>
            <div><dt>校验耗时</dt><dd>{{ seconds(diagnostics.timing?.validation_seconds) }}</dd></div>
            <div><dt>渲染耗时</dt><dd>{{ seconds(diagnostics.timing?.render_seconds) }}</dd></div>
          </dl>
          <p>模型请求 {{ recorded(diagnostics.calls?.model_requests) }} / {{ recorded(diagnostics.calls?.max_model_requests) }} 次 · 结构纠正 {{ diagnostics.calls?.correction_used == null ? '未记录' : diagnostics.calls.correction_used ? '1 次' : '0 次' }} · 补证据 {{ recorded(diagnostics.calls?.supplement_rounds) }} 轮</p>
          <div v-if="diagnostics.stages?.length" class="direct-style-diagnostic-table">
            <table><caption>各阶段耗时</caption><thead><tr><th scope="col">阶段</th><th scope="col">耗时</th><th scope="col">模型请求</th><th scope="col">状态</th></tr></thead><tbody><tr v-for="(stage, index) in diagnostics.stages" :key="`${stage.name}-${index}`"><th scope="row">{{ stage.label || labels[stage.name] || stage.name }}</th><td>{{ seconds(stage.elapsed_seconds) }}</td><td>{{ recorded(stage.model_requests) }}</td><td>{{ stageStatuses[stage.status] || stage.status }}</td></tr></tbody></table>
          </div>
          <p v-else>本次未记录阶段耗时。</p>
          <h4>候选与选择依据</h4>
          <ul v-if="diagnostics.candidate_snapshot?.length" class="direct-style-diagnostic-candidates">
            <li v-for="candidate in diagnostics.candidate_snapshot" :key="candidate.id"><div><b>{{ candidate.label }}</b><span>{{ candidateState(candidate.id, candidate.selection_eligible) }}</span></div><small>{{ candidate.id }}</small><p>召回依据：{{ retrievalLabels[candidate.retrieval_basis] || candidate.retrieval_basis }} · 成立组：{{ groups(candidate.matched_group_ids) }} · 缺失组：{{ groups(candidate.missing_group_ids) }}</p></li>
          </ul>
          <p v-else>本次未记录候选快照。</p>
          <h4 v-if="diagnostics.decisions?.length">选择、拒绝与修复记录</h4>
          <ul v-if="diagnostics.decisions?.length" class="direct-style-diagnostic-decisions"><li v-for="(decision, index) in diagnostics.decisions" :key="index"><b>{{ decision.field === 'anchor' ? '风格锚点' : decision.field || '校验' }}{{ decision.index == null ? '' : ` #${decision.index + 1}` }}</b> · {{ decisionText(decision) }}</li></ul>
          <p>输入 Token：{{ recorded(diagnostics.usage?.prompt_tokens) }} · 输出 Token：{{ recorded(diagnostics.usage?.completion_tokens) }}</p>
          <p v-if="diagnostics.usage?.estimated_list_price_cny != null">目录价估算 ¥{{ diagnostics.usage.estimated_list_price_cny.toFixed(5) }}{{ diagnostics.usage.complete ? '（用量完整，非最终账单）' : '（记录不完整，非最终账单）' }}</p>
          <p v-else>目录价估算：未记录<span v-if="diagnostics.usage?.known_cost_subtotal_cny != null">；已记录费用小计 ¥{{ diagnostics.usage.known_cost_subtotal_cny.toFixed(5) }}，其余用量未知</span>。</p>
          <p class="direct-style-help">{{ diagnostics.quality?.note || '未记录人工准确率；结构、范围和引用检查仅代表已执行的规则校验。' }}</p>
        </template>
        <p v-else class="direct-style-help">{{ diagnostics?.note || '历史分析未记录诊断；阶段耗时、候选和重试次数保持未知。' }}</p>
        <a :href="styleDiagnosticsUrl(analysis.analysis_id)" target="_blank" rel="noopener noreferrer">导出诊断 Markdown</a>
      </details>
      <template v-if="profile">
        <p v-if="analysis?.status === 'failed'" class="direct-style-error" role="status" data-testid="style-incomplete">画风分析未完成，未确定具体风格。已识别的主体、动作与摄影信息不受影响；不会用其他风格替代。</p>
        <p v-else-if="qualityState?.interpretation === 'partial' || qualityState?.interpretation === 'failed'" class="direct-style-help" role="status" data-testid="style-incomplete">画风专业转译未完成，以下仅为已核验的基础视觉特征，不代表已确定具体风格。</p>
        <h3>{{ profile.descriptive_name }} <small>{{ profile.application_status === 'user-edited' ? '用户编辑' : '系统判断' }} · v{{ profile.version }}</small></h3>
        <section class="direct-style-observations" data-testid="style-observations"><h4>参考画面中的观察</h4><p v-for="claim in analysis!.result.evidence?.claims" :key="claim.id"><b>{{ claim.id }}</b> · {{ claim.observation }}</p><p v-if="!analysis!.result.evidence?.claims?.length">本次未记录逐项观察。</p><small>这些是分析记录；下面的配方是可选创作方向，不新增对参考图的事实判断。</small></section>
        <section class="direct-style-recipes" data-testid="style-recommendations"><h4>可选画风配方 <small>最多 3 个</small></h4><p class="direct-style-help">高 / 中表示当前观察对配方的支持程度，不是准确率或制作来源认定。点击只填入草稿。</p><p v-if="derivedRecommendations" class="direct-style-help" data-testid="style-derived-recommendations">这些建议由历史观察只读整理，无需重新分析。</p><p v-if="!recommendations.length" data-testid="style-no-recommendations">当前观察还没有足够证据支持库内配方；可以继续编辑已有画风。</p>
          <article v-for="recipe in recommendations" :key="recipe.recipe_id" class="direct-style-recipe" :class="{ selected: selectedRecipe?.recipe_id === recipe.recipe_id }" data-testid="style-recipe-card"><div class="direct-style-recipe-heading"><h4>{{ recipe.label }}</h4><span>{{ recipe.fit_level === 'high' ? '高' : '中' }} · 证据支持</span></div><p>{{ recipe.reason }}</p><ul v-if="recipe.controls?.length"><li v-for="(control, index) in recipe.controls" :key="`${control.claim_id}-${index}`"><b>{{ control.claim_id }}</b> · {{ control.quote }}</li></ul><p v-if="recipe.overlap?.length"><b>重合特征：</b>{{ recipe.overlap.join('；') }}</p><p v-if="recipe.differences?.length"><b>差异与取舍：</b>{{ recipe.differences.join('；') }}</p><details><summary>查看完整库内 Prompt</summary><pre data-testid="style-library-prompt">{{ recipe.library_prompt }}</pre></details><details v-if="recipe.detailed_description"><summary>详细画风描述</summary><p>{{ recipe.detailed_description }}</p></details><button :disabled="running || busy" data-testid="style-choose-recipe" @click="chooseRecipe(recipe)">{{ selectedRecipe?.recipe_id === recipe.recipe_id ? '已填入草稿' : '选择此配方，填入草稿' }}</button></article>
        </section>
        <p v-if="profile.anchor" class="direct-style-help">生成风格锚点（非来源鉴定）：{{ profile.anchor.reason }}</p>
        <p v-if="profile.reference" class="direct-style-help" data-testid="style-reference">视觉参考（非出品方认定）：{{ profile.reference.reason }}</p>
        <textarea v-model="text" class="direct-style-editor" aria-label="高密度画风 Prompt" :disabled="running || busy" />
        <p v-if="selectedRecipe" class="direct-style-help" data-testid="style-recipe-draft">草稿来源：库内配方 {{ recommendations.find(item => item.recipe_id === selectedRecipe!.recipe_id)?.label || selectedRecipe.recipe_id }}。可继续编辑；尚未应用到项目。</p>
        <div class="direct-style-actions"><button :disabled="!text" @click="copy"><Copy :size="15" />复制</button><button :disabled="busy || running || !draftChanged" @click="restoreSaved">恢复已保存画风</button><button :disabled="busy || running || !draftChanged || !text" @click="save">保存修改</button></div>
        <details><summary>详细画风描述</summary><p>{{ profile.detailed_prompt }}</p><p v-for="(value, scene) in profile.scene_prompts" :key="scene">{{ scene }} · {{ value }}</p><p v-for="(value, alias) in profile.local_prompts" :key="alias" data-testid="style-local-effect">{{ alias }} · {{ value }}（不继承到全局画风）</p></details>
        <details><summary>视觉证据与不确定信息</summary><div class="direct-style-evidence"><figure v-for="asset in analysis!.result.assets" :key="asset.alias"><img :src="styleImageUrl(analysis!.analysis_id, asset.alias)" :alt="`证据 ${asset.alias}`" /><figcaption>{{ asset.alias }}{{ asset.parent_alias ? ' · 原图裁剪' : '' }}</figcaption></figure></div><p v-for="claim in analysis!.result.evidence?.claims" :key="claim.id"><b>{{ claim.images.join(' / ') }}</b> · {{ claim.observation }}</p><p v-for="uncertainty in profile.uncertainties" :key="uncertainty">{{ uncertainty }}</p></details>
        <div class="direct-style-apply"><label>应用到已完成项目<select v-model="target" :disabled="busy || running"><option value="">请选择目标项目</option><option v-for="item in targets" :key="item.id" :value="item.id">{{ item.label }}</option></select></label><button :disabled="busy || !target || running || !text" @click="apply">更新首帧与 Seedance 画风</button><small>会更新所选项目的风格并保留旧 Prompt 版本；其他任务和节点图片不变。</small></div>
        <small v-if="analysis!.result.estimated_list_price_cny != null">目录价估算 ¥{{ analysis!.result.estimated_list_price_cny.toFixed(5) }}{{ analysis!.result.cost_estimate_complete ? '' : '（用量不完整，非最终账单）' }}</small>
      </template>
    </section>
  </div>
</template>

<style scoped>
.direct-style-observations,.direct-style-recipes{margin:18px 0;padding:16px;border:1px solid #7194aa44;border-radius:12px}.direct-style-observations h4,.direct-style-recipes h4{margin:0 0 10px}.direct-style-observations p{line-height:1.65}.direct-style-recipe{margin-top:12px;padding:15px;border:1px solid #7194aa55;border-radius:10px;background:#7194aa08}.direct-style-recipe.selected{border-color:#81c8ec;background:#81c8ec12}.direct-style-recipe-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.direct-style-recipe-heading h4{margin:0}.direct-style-recipe-heading span{flex-shrink:0;color:#9cd4ed;font-size:12px}.direct-style-recipe p,.direct-style-recipe li{line-height:1.65}.direct-style-recipe pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.75;margin:12px 0;padding:12px;border-radius:8px;background:#0002}.direct-style-recipe button{margin-top:10px}.direct-style-recipe details{margin:10px 0}
.direct-style-backdrop{position:fixed;inset:0;z-index:150;background:rgba(3,7,12,.64);display:grid;place-items:center;padding:24px}.direct-style-studio{width:min(820px,100%);max-height:90vh;overflow:auto;background:var(--surface,#202126);color:var(--text,#e8eaf0);border:1px solid #7194aa55;border-radius:20px;padding:24px;box-shadow:0 20px 90px #0008}.direct-style-studio header,.direct-style-actions,.direct-style-status{display:flex;align-items:center;justify-content:space-between;gap:12px}.direct-style-studio h2{margin:6px 0 12px}.direct-style-studio small,.direct-style-help{color:#92a7b5;line-height:1.7}.direct-style-studio button{display:inline-flex;align-items:center;gap:7px;border:1px solid #7194aa66;border-radius:10px;padding:10px 14px;background:#7194aa18;color:inherit;cursor:pointer}.direct-style-studio button:disabled{opacity:.4;cursor:default}.direct-style-actions{justify-content:flex-start;flex-wrap:wrap;margin:14px 0}.direct-style-actions span{font-size:12px;color:#92a7b5;flex:1;min-width:180px}.direct-style-status{padding:12px;border-radius:10px;background:#6393b41a;font-size:13px}.direct-style-editor{width:100%;min-height:190px;resize:vertical;background:#0c121840;border:1px solid #7194aa55;border-radius:12px;color:inherit;padding:14px;font:inherit;line-height:1.9;box-sizing:border-box}.direct-style-studio details{margin:16px 0;line-height:1.8}.direct-style-evidence{display:flex;gap:12px;flex-wrap:wrap}.direct-style-evidence figure{margin:8px 0;width:110px}.direct-style-evidence img{width:100%;height:110px;object-fit:contain}.direct-style-error{color:#e8b57e}.direct-style-apply{border-top:1px solid #7194aa44;padding-top:16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}.direct-style-apply select{margin-left:8px;background:#171d24;color:inherit;padding:8px;border-radius:8px}.direct-style-apply small{width:100%}
</style>

<style scoped>
.direct-style-diagnostics{padding:12px 14px;border:1px solid #7194aa44;border-radius:12px;font-size:13px}.direct-style-diagnostics summary{cursor:pointer}.direct-style-diagnostics h4{margin:16px 0 8px}.direct-style-diagnostic-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:12px;margin:16px 0}.direct-style-diagnostic-metrics dt{color:#92a7b5}.direct-style-diagnostic-metrics dd{margin:2px 0;font-variant-numeric:tabular-nums}.direct-style-diagnostic-table{overflow-x:auto}.direct-style-diagnostic-table table{width:100%;border-collapse:collapse;text-align:left}.direct-style-diagnostic-table caption{text-align:left;font-weight:600;margin-bottom:8px}.direct-style-diagnostic-table th,.direct-style-diagnostic-table td{border-bottom:1px solid #7194aa33;padding:6px 10px 6px 0}.direct-style-diagnostic-candidates,.direct-style-diagnostic-decisions{padding-left:20px}.direct-style-diagnostic-candidates li{margin:10px 0}.direct-style-diagnostic-candidates li div{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between}.direct-style-diagnostic-candidates p{margin:4px 0;overflow-wrap:anywhere}.direct-style-diagnostic-decisions li{margin:6px 0;overflow-wrap:anywhere}.direct-style-diagnostics a{color:inherit;text-decoration:underline;text-underline-offset:3px}
</style>

<style scoped>
</style>
